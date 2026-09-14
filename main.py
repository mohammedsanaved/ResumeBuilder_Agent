"""
main.py

End-to-end runner: paste a JD -> parse requirements via Ollama -> score
project_bank.json entries -> select top 3-4 -> generate a tailored summary
-> build the resume via resume_template.generate_resume -> (if a contact
email is found in the JD) draft a cold email.

Usage:
    python main.py
    # paste the JD text, then press Ctrl+D (Linux/Mac) or Ctrl+Z + Enter (Windows)

    python main.py --jd-file path/to/jd.txt --output-dir ./output

Setup required before running:
    1. Ollama running locally: `ollama serve`
    2. A chat model pulled:      `ollama pull llama3.1:8b`
    3. An embedding model pulled: `ollama pull nomic-embed-text`
    4. `pip install requests python-docx`
    5. `python resume_indexer.py ./resumes`   (builds project_bank.json)
    6. Copy candidate_profile.example.json -> candidate_profile.json
       and fill in your own name, contact info, skills, experience, education.
"""

import os
import re
import sys
import json
import math
import argparse

import requests

from resume_template import generate_resume
from crawler_and_ranker import crawl_resumes_for_projects, rank_projects_for_jd

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "gpt-oss:120b-cloud")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

PROJECT_BANK_PATH = "project_bank.json"
CANDIDATE_PROFILE_PATH = "candidate_profile.json"

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_chat_json(system_prompt, user_prompt, model=CHAT_MODEL):
    """Calls Ollama chat with JSON-only output enforced. Returns a parsed dict."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Some models still wrap JSON in stray text even with format=json.
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"Model did not return valid JSON:\n{content}")


def ollama_chat_text(system_prompt, user_prompt, model=CHAT_MODEL):
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


def ollama_embed(text, model=EMBED_MODEL):
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# JD parsing
# ---------------------------------------------------------------------------

JD_SYSTEM_PROMPT = """You extract structured hiring requirements from a job description.
Respond with ONLY a JSON object, no preamble, no markdown fences, matching exactly this shape:
{
  "job_role": string,
  "company_name": string,
  "required_skills": [string, ...],
  "nice_to_have_skills": [string, ...],
  "seniority": string,
  "domain": string
}
If a field isn't clearly stated in the JD, make a reasonable short guess rather than leaving it empty."""


def parse_jd(jd_text):
    parsed = ollama_chat_json(JD_SYSTEM_PROMPT, jd_text)
    email_match = EMAIL_REGEX.search(jd_text)
    parsed["contact_email"] = email_match.group(0) if email_match else None
    return parsed


# ---------------------------------------------------------------------------
# Project matching
# ---------------------------------------------------------------------------

def _project_text(project):
    return f"{project['title']} {project.get('tech', '')} " + " ".join(project.get("bullets", []))


def _ensure_embeddings(projects):
    """Adds an embedding to any project that doesn't have one yet, then writes
    the cache back so repeat runs never re-embed unchanged projects."""
    changed = False
    for p in projects:
        if "embedding" not in p:
            p["embedding"] = ollama_embed(_project_text(p))
            changed = True
    if changed:
        with open(PROJECT_BANK_PATH, "w") as f:
            json.dump({"files": {}, "projects": projects}, f, indent=2)
    return projects


def score_projects(projects, jd):
    skills_text = ", ".join(jd["required_skills"] + jd.get("nice_to_have_skills", []))
    jd_embedding = ollama_embed(skills_text)
    required_lower = [s.lower() for s in jd["required_skills"]]

    scored = []
    for p in projects:
        sim = cosine_similarity(p["embedding"], jd_embedding)
        text_lower = _project_text(p).lower()
        matched_skills = [s for s in required_lower if s in text_lower]
        keyword_score = len(matched_skills) / max(len(required_lower), 1)
        hybrid = 0.6 * sim + 0.4 * keyword_score
        scored.append({**p, "score": hybrid, "matched_skills": matched_skills})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored, required_lower


def select_top_projects(scored, required_lower, top_n=4):
    """Greedy selection with a diversity pass: start with the highest-scoring
    projects, then swap in a lower-ranked project if it covers a required
    skill that nothing selected so far covers."""
    selected = scored[:top_n]
    covered = set()
    for p in selected:
        covered.update(p["matched_skills"])

    missing_skills = set(required_lower) - covered
    if not missing_skills:
        return selected

    for candidate in scored[top_n:]:
        if not missing_skills:
            break
        new_skills = set(candidate["matched_skills"]) & missing_skills
        if new_skills:
            selected.sort(key=lambda x: x["score"])  # weakest first
            selected[0] = candidate
            covered.update(new_skills)
            missing_skills -= new_skills
            selected.sort(key=lambda x: x["score"], reverse=True)

    return selected


# ---------------------------------------------------------------------------
# Summary & cold email generation
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """You write a concise, 3-4 sentence professional resume summary.
Mirror the language and priorities of the job description's required skills where it is
truthful to do so. Do not invent experience beyond what is given in the candidate's
projects and base summary. Return plain text only, no headings or markdown."""


# def generate_summary(jd, candidate_profile, selected_projects):
#     project_lines = "\n".join(
#         f"- {p['title']}: {'; '.join(p.get('bullets', []))}" for p in selected_projects
#     )
#     user_prompt = (
#         f"Job role: {jd['job_role']}\n"
#         f"Required skills: {', '.join(jd['required_skills'])}\n"
#         f"Candidate base summary: {candidate_profile.get('base_summary', '')}\n"
#         f"Selected projects:\n{project_lines}"
#     )
#     return ollama_chat_text(SUMMARY_SYSTEM_PROMPT, user_prompt)


# COLD_EMAIL_SYSTEM_PROMPT = """You write a short, direct cold email (under 150 words) from a
# job candidate to a hiring contact. Reference 1-2 concrete achievements from the given
# projects. No generic filler, no over-the-top enthusiasm, no fabricated details.
# Return plain text: a "Subject: ..." line first, then a blank line, then the email body."""


# def generate_cold_email(jd, candidate_profile, selected_projects):
#     project_lines = "\n".join(
#         f"- {p['title']}: {'; '.join(p.get('bullets', [])[:2])}" for p in selected_projects[:2]
#     )
#     contact = candidate_profile.get("contact", {})
#     user_prompt = (
#         f"Candidate name: {candidate_profile['candidate_name']}\n"
#         f"Applying for: {jd['job_role']} at {jd['company_name']}\n"
#         f"Key matching projects:\n{project_lines}\n"
#         f"Candidate contact: {contact.get('email', '')}, {contact.get('phone', '')}"
#     )
#     return ollama_chat_text(COLD_EMAIL_SYSTEM_PROMPT, user_prompt)
def generate_summary(jd, candidate_profile, selected_projects):
    # Convert the (prefix, text) tuples into a single readable string for the LLM
    project_lines = "\n".join(
        f"- {p['title']}: {'; '.join([f'{prefix} {text}'.strip() for prefix, text in p.get('bullets', [])])}" 
        for p in selected_projects
    )
    user_prompt = (
        f"Job role: {jd['job_role']}\n"
        f"Required skills: {', '.join(jd['required_skills'])}\n"
        f"Candidate base summary: {candidate_profile.get('base_summary', '')}\n"
        f"Selected projects:\n{project_lines}"
    )
    return ollama_chat_text(SUMMARY_SYSTEM_PROMPT, user_prompt)

COLD_EMAIL_SYSTEM_PROMPT = """You write a short, direct cold email (under 150 words) from a
job candidate to a hiring contact. Reference 1-2 concrete achievements from the given
projects. No generic filler, no over-the-top enthusiasm, no fabricated details.
Return plain text: a "Subject: ..." line first, then a blank line, then the email body."""

def generate_cold_email(jd, candidate_profile, selected_projects):
    # Apply the same tuple conversion here, limiting to the first 2 bullets
    project_lines = "\n".join(
        f"- {p['title']}: {'; '.join([f'{prefix} {text}'.strip() for prefix, text in p.get('bullets', [])[:2]])}" 
        for p in selected_projects[:2]
    )
    contact = candidate_profile.get("contact", {})
    user_prompt = (
        f"Candidate name: {candidate_profile['candidate_name']}\n"
        f"Applying for: {jd['job_role']} at {jd['company_name']}\n"
        f"Key matching projects:\n{project_lines}\n"
        f"Candidate contact: {contact.get('email', '')}, {contact.get('phone', '')}"
    )
    return ollama_chat_text(COLD_EMAIL_SYSTEM_PROMPT, user_prompt)

COVER_LETTER_SYSTEM_PROMPT = """You write a professional, tailored cover letter (3-4 paragraphs) from a job candidate to a hiring manager. 
    Focus on how the candidate's skills and specific project achievements align with the job description.
    Use a confident, professional tone. Do not invent experience. Do not use bracketed placeholders like [Company Address] unless the data is provided.
Return plain text only: start directly with "Dear Hiring Manager," or similar."""

def generate_cover_letter(jd, candidate_profile, selected_projects):
    project_lines = "\n".join(
        f"- {p['title']}: {'; '.join([f'{prefix} {text}'.strip() for prefix, text in p.get('bullets', [])])}" 
        for p in selected_projects
    )
    user_prompt = (
        f"Candidate name: {candidate_profile['candidate_name']}\n"
        f"Applying for: {jd['job_role']} at {jd['company_name']}\n"
        f"Required skills: {', '.join(jd['required_skills'])}\n"
        f"Candidate base summary: {candidate_profile.get('base_summary', '')}\n"
        f"Key matching projects:\n{project_lines}\n"
    )
    return ollama_chat_text(COVER_LETTER_SYSTEM_PROMPT, user_prompt)

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def run(jd_text, output_dir="."):
    if not os.path.exists(CANDIDATE_PROFILE_PATH):
        raise FileNotFoundError(
            f"{CANDIDATE_PROFILE_PATH} not found. Copy candidate_profile.example.json to "
            f"{CANDIDATE_PROFILE_PATH} and fill in your own details."
        )

    candidate_profile = load_json(CANDIDATE_PROFILE_PATH)

    # 7 steps total if an email is found (JD parsing, Crawl/Rank, Summary, Resume, Cold Email, Cover Letter)
    total_steps = 4 if not EMAIL_REGEX.search(jd_text) else 6

    print(f"\n[Step 1/{total_steps}] 🧠 Parsing job description via local LLM (this may take a moment)...")
    jd = parse_jd(jd_text)
    print(f"   ✓ Detected Role: {jd['job_role']} | Company: {jd['company_name']}")
    print(f"   ✓ Required skills: {', '.join(jd['required_skills'])}")
    if jd["contact_email"]:
        print(f"   ✓ Found contact email: {jd['contact_email']}")

    print(f"\n[Step 2/{total_steps}] 🕷️ Crawling resumes and ranking projects...")
    all_projects = crawl_resumes_for_projects("./resumes")
    selected = rank_projects_for_jd(all_projects, jd, top_n=3)
    
    print(f"   ✓ Selected top projects:")
    for p in selected:
        print(f"      - {p['title']}")

    print(f"\n[Step 3/{total_steps}] ✍️  Generating tailored summary via local LLM...")
    summary = generate_summary(jd, candidate_profile, selected)
    print("   ✓ Summary generated successfully.")

    resume_data = {
        "candidate_name": candidate_profile["candidate_name"],
        "job_role": jd["job_role"],
        "company_name": jd["company_name"],
        "tagline": candidate_profile.get("tagline", ""),
        "contact": candidate_profile.get("contact", {}),
        "summary": summary,
        "skills": [tuple(s) for s in candidate_profile.get("skills", [])],
        "experience": [
            {**exp, "bullets": [tuple(b) for b in exp.get("bullets", [])]}
            for exp in candidate_profile.get("experience", [])
        ],
        "projects": [
            {
                "title": p["title"],
                "tag": p.get("tag"),
                "tech": p.get("tech", ""),
                "bullets": p.get("bullets", []),
            }
            for p in selected
        ],
        "education": candidate_profile.get("education", {}),
    }

    print(f"\n[Step 4/{total_steps}] 📄 Building and formatting the .docx resume...")
    resume_path = generate_resume(resume_data, output_dir=output_dir)
    print(f"   ✓ Saved resume to: {resume_path}")

    if jd["contact_email"]:
        safe_role = jd["job_role"].replace(" ", "_")
        safe_company = jd["company_name"].replace(" ", "_")

        print(f"\n[Step 5/{total_steps}] 📧 Generating cold email draft via local LLM...")
        email_text = generate_cold_email(jd, candidate_profile, selected)
        email_path = os.path.join(output_dir, f"ColdEmail_{safe_role}_{safe_company}.txt")
        with open(email_path, "w", encoding="utf-8") as f:
            f.write(email_text)
        print(f"   ✓ Saved cold email to: {email_path}")

        print(f"\n[Step 6/{total_steps}] 📜 Generating cover letter via local LLM...")
        cover_letter_text = generate_cover_letter(jd, candidate_profile, selected)
        cl_path = os.path.join(output_dir, f"CoverLetter_{safe_role}_{safe_company}.txt")
        with open(cl_path, "w", encoding="utf-8") as f:
            f.write(cover_letter_text)
        print(f"   ✓ Saved cover letter to: {cl_path}")

    else:
        print(f"\n[Step 5/4] ⏭️  No contact email found in JD. Skipping cold email and cover letter generation.")

    print("\n✅ All tasks completed successfully!\n")

def main():
    parser = argparse.ArgumentParser(
        description="Generate a JD-tailored resume (and cold email) locally via Ollama."
    )
    parser.add_argument("--jd-file", help="Path to a text file containing the JD. If omitted, reads from stdin.")
    parser.add_argument("--output-dir", default=".", help="Where to save the generated resume/email.")
    args = parser.parse_args()

    if args.jd_file:
        with open(args.jd_file, "r", encoding="utf-8") as f:
            jd_text = f.read()
    else:
        print("Paste the job description, then press Ctrl+D (Linux/Mac) or Ctrl+Z then Enter (Windows):")
        jd_text = sys.stdin.read()

    run(jd_text, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
