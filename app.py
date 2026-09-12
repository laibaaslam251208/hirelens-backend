import os
import sqlite3
import base64
import json
import hashlib
import smtplib
from email.mime.text import MIMEText
import fitz  # PyMuPDF
from flask import Flask, request, jsonify
from flask_cors import CORS
from huggingface_hub import InferenceClient

# Environment variables setup (Render Dashboard se read honge)
HF_TOKEN = os.environ.get('HF_API_KEY')

client = InferenceClient(
    provider="groq",
    api_key=HF_TOKEN
)

vision_client = InferenceClient(api_key=HF_TOKEN)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key')
CORS(app)

interview_sessions = {}

def ask_ai(prompt, max_tokens=800, retries=3, fallback="Could you tell me more about your hands-on experience with this?"):
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens
            )
            text = response.choices[0].message.content.strip()
            if text:
                return text
        except Exception as e:
            print("ask_ai error:", e)
    return fallback

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def send_email(to_email, subject, body):
    try:
        gmail_address = os.environ.get('GMAIL_ADDRESS')
        gmail_password = os.environ.get('GMAIL_APP_PASSWORD')
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = gmail_address
        msg['To'] = to_email
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, to_email, msg.as_string())
        print(f"Email sent to {to_email}")
        return True
    except Exception as e:
        print("Email error:", e)
        return False

def init_db():
    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS recruiters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    skills TEXT NOT NULL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    name TEXT,
                    extracted_skills TEXT,
                    experience_years TEXT,
                    match_score INTEGER,
                    interview_score INTEGER,
                    status TEXT DEFAULT 'Pending',
                    decision TEXT DEFAULT NULL
                )''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def home():
    return jsonify({"message": "HireLens AI backend is running"})

@app.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    if not name or not email or not password:
        return jsonify({"error": "Name, email and password are required"}), 400

    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO recruiters (name, email, password_hash) VALUES (?, ?, ?)",
                  (name, email, hash_password(password)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "An account with this email already exists"}), 400
    conn.close()
    return jsonify({"message": "Account created", "name": name}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("SELECT name, password_hash FROM recruiters WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()

    if not row or row[1] != hash_password(password):
        return jsonify({"error": "Invalid email or password"}), 401

    return jsonify({"message": "Login successful", "name": row[0]})

@app.route('/submit-job', methods=['POST'])
def submit_job():
    data = request.get_json()
    title = data.get('title')
    description = data.get('description', '')
    skills = data.get('skills')
    if not title or not skills:
        return jsonify({"error": "Job title and skills are required"}), 400

    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("INSERT INTO jobs (title, description, skills) VALUES (?, ?, ?)", (title, description, skills))
    conn.commit()
    job_id = c.lastrowid
    conn.close()
    return jsonify({"message": "Job posted successfully", "job_id": job_id}), 201

@app.route('/jobs', methods=['GET'])
def get_jobs():
    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("SELECT id, title, description, skills FROM jobs")
    rows = c.fetchall()
    conn.close()
    jobs = [{"id": r[0], "title": r[1], "description": r[2], "skills": r[3]} for r in rows]
    return jsonify(jobs)

@app.route('/upload-resume', methods=['POST'])
def upload_resume():
    if 'resume' not in request.files:
        return jsonify({"error": "No resume file uploaded"}), 400
    job_id = request.form.get('job_id')
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("SELECT title, skills FROM jobs WHERE id=?", (job_id,))
    job = c.fetchone()
    conn.close()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job_title, job_skills = job

    resume_file = request.files['resume']
    filename = resume_file.filename.lower()
    file_bytes = resume_file.read()

    try:
        if filename.endswith('.pdf'):
            pdf = fitz.open(stream=file_bytes, filetype="pdf")
            page = pdf[0]
            pix = page.get_pixmap(dpi=150)
            image_bytes = pix.tobytes("png")
            pdf.close()
        else:
            image_bytes = file_bytes

        image_b64 = base64.b64encode(image_bytes).decode('utf-8')

        prompt = f"""This is a resume/CV. The candidate is applying for: {job_title}, which requires these skills: {job_skills}.
Read the resume and respond with ONLY a JSON object (no extra text) in this exact format:
{{"name": "candidate name", "skills": "comma separated skills found in resume", "experience_years": "number or range", "match_score": a number from 0 to 100 representing how well the candidate matches the required skills}}"""

        response = vision_client.chat.completions.create(
            model="meta-llama/Llama-4-Scout-17B-16E-Instruct",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
            ]}],
            max_tokens=400
        )
        result_text = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(result_text)

        candidate_name = result.get('name', 'Unknown')

        conn = sqlite3.connect('hirelens.db')
        c = conn.cursor()
        c.execute("SELECT id FROM candidates WHERE job_id=? AND LOWER(name)=LOWER(?)", (job_id, candidate_name))
        existing_candidate = c.fetchone()

        if existing_candidate:
            conn.close()
            return jsonify({"error": f"Candidate '{candidate_name}' is already uploaded for this job."}), 400

        c.execute("""INSERT INTO candidates (job_id, name, extracted_skills, experience_years, match_score)
                     VALUES (?, ?, ?, ?, ?)""",
                  (job_id, candidate_name, result.get('skills', ''),
                   result.get('experience_years', ''), result.get('match_score', 0)))
        conn.commit()
        candidate_id = c.lastrowid
        conn.close()
        return jsonify({"message": "Resume processed successfully", "candidate_id": candidate_id, "data": result}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/candidates', methods=['GET'])
def get_candidates():
    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("SELECT id, job_id, name, extracted_skills, experience_years, match_score, interview_score, status, decision FROM candidates")
    rows = c.fetchall()
    conn.close()
    candidates = [{"id": r[0], "job_id": r[1], "name": r[2], "skills": r[3],
                   "experience_years": r[4], "match_score": r[5], "interview_score": r[6],
                   "status": r[7], "decision": r[8]} for r in rows]
    return jsonify(candidates)

@app.route('/start-interview', methods=['POST'])
def start_interview():
    data = request.get_json()
    candidate_id = data.get('candidate_id')

    conn = sqlite3.connect('hirelens.db')
    c = conn.cursor()
    c.execute("SELECT name, extracted_skills, job_id, status FROM candidates WHERE id=?", (candidate_id,))
    cand = c.fetchone()
    if not cand:
        conn.close()
        return jsonify({"error": "Candidate not found"}), 404

    name, cand_skills, job_id, status = cand

    if status == 'Interviewed':
        conn.close()
        return jsonify({"error": f"Interview for {name} has already been completed."}), 400

    c.execute("SELECT title, skills FROM jobs WHERE id=?", (job_id,))
    job_title, job_skills = c.fetchone()
    conn.close()

    prompt = f"""You are interviewing {name} for a {job_title} role that requires: {job_skills}.
Their resume lists these skills: {cand_skills}.
Ask ONE short, specific technical interview question to test their practical knowledge of one of these skills. Respond with ONLY the question, no preamble, no markdown formatting."""

    question = ask_ai(prompt, max_tokens=800)

    interview_sessions[str(candidate_id)] = {
        "job_title": job_title, "job_skills": job_skills, "cand_skills": cand_skills,
        "qa": [{"question": question, "answer": None}]
    }
    return jsonify({"question": question, "question_number": 1})

@app.route('/interview-answer', methods=['POST'])
def interview_answer():
    data = request.get_json()
    candidate_id = str(data.get('candidate_id'))
    answer = data.get('answer')

    session = interview_sessions.get(candidate_id)
    if not session:
        return jsonify({"error": "No active interview session for this candidate"}), 400

    session['qa'][-1]['answer'] = answer

    if len(session['qa']) < 3:
        history = "\n".join([f"Q: {qa['question']}\nA: {qa['answer']}" for qa in session['qa']])
        prompt = f"""Interview so far for a {session['job_title']} role (required skills: {session['job_skills']}):
{history}

Ask the NEXT short, specific technical interview question, testing a different skill than before. Respond with ONLY the question, no preamble, no markdown formatting."""
        next_q = ask_ai(prompt, max_tokens=800)
        session['qa'].append({"question": next_q, "answer": None})
        return jsonify({"question": next_q, "question_number": len(session['qa']), "done": False})
    else:
        transcript = "\n".join([f"Q: {qa['question']}\nA: {qa['answer']}" for qa in session['qa']])
        eval_prompt = f"""Evaluate this candidate's interview for a {session['job_title']} role (required skills: {session['job_skills']}):
{transcript}

Respond with ONLY a JSON object, no markdown formatting: {{"score": a number 0-100 for overall interview quality, "feedback": "one short sentence of feedback"}}"""
        result_text = ask_ai(eval_prompt, max_tokens=800, fallback='{"score": 50, "feedback": "Evaluation unavailable, default score applied."}')
        result_text = result_text.replace("```json", "").replace("

