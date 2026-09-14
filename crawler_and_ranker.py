import os
import glob
import math
import docx
import requests

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"

# ---------------------------------------------------------------------------
# 1. ROBUST CRAWLER (Fixes the missing descriptions & duplicates bug)
# ---------------------------------------------------------------------------
def crawl_resumes_for_projects(resumes_dir="./resumes"):
    """
    Crawls all .docx files in the directory, reliably extracting projects 
    and deduplicating them by title.
    """
    if not os.path.exists(resumes_dir):
        print(f"Directory {resumes_dir} not found.")
        return []

    unique_projects = {}

    for filepath in glob.glob(os.path.join(resumes_dir, "*.docx")):
        try:
            doc = docx.Document(filepath)
        except Exception as e:
            print(f"Skipping {filepath} (unreadable): {e}")
            continue

        in_projects_section = False
        curr_proj = None

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            upper_text = text.upper()

            # Detect section boundaries
            if "FEATURED" in upper_text and "PROJECT" in upper_text:
                in_projects_section = True
                continue
            
            # Stop if we hit Education or another major section
            if in_projects_section and upper_text in ["EDUCATION", "CERTIFICATIONS", "AWARDS"]:
                in_projects_section = False
                if curr_proj:
                    unique_projects[curr_proj["title"]] = curr_proj
                    curr_proj = None
                continue

            if in_projects_section:
                # Detect Project Title Line (Look for the pipe '|' separating title and tech)
                if "|" in text and not text.startswith("•"):
                    if curr_proj:
                        unique_projects[curr_proj["title"]] = curr_proj
                    
                    parts = text.split("|", 1)
                    title_raw = parts[0].strip()
                    tech_raw = parts[1].strip() if len(parts) > 1 else ""

                    # Clean up numbering (e.g., "1. ThinkStack AI" -> "ThinkStack AI")
                    if title_raw and title_raw[0].isdigit() and ". " in title_raw[:4]:
                        title_raw = title_raw.split(". ", 1)[-1].strip()

                    # Extract tags if present (e.g., "[Live on Google Play]")
                    tag = None
                    if "[" in title_raw and "]" in title_raw:
                        tag_start = title_raw.find("[")
                        tag_end = title_raw.find("]")
                        tag = title_raw[tag_start:tag_end+1]
                        title_raw = title_raw[:tag_start].strip()

                    curr_proj = {
                        "title": title_raw,
                        "tag": tag,
                        "tech": tech_raw,
                        "bullets": [],
                        "source": os.path.basename(filepath)
                    }

                # Detect Bullet Points
                elif curr_proj and text.startswith("•"):
                    clean_bullet = text.lstrip("• ").strip()
                    
                    # Safely split prefix from description using the colon ':'
                    if ":" in clean_bullet:
                        b_parts = clean_bullet.split(":", 1)
                        prefix = b_parts[0].strip() + ":"
                        desc = b_parts[1].strip()
                        curr_proj["bullets"].append((prefix, desc))
                    else:
                        curr_proj["bullets"].append(("", clean_bullet))

        # Catch the last project in the file
        if curr_proj:
            unique_projects[curr_proj["title"]] = curr_proj

    final_projects = list(unique_projects.values())
    print(f"🕷️ Crawled {len(final_projects)} unique projects from '{resumes_dir}'.")
    return final_projects


# ---------------------------------------------------------------------------
# 2. EMBEDDING & HYBRID RANKING ENGINE
# ---------------------------------------------------------------------------
def get_embedding(text):
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        print(f"Embedding error: {e}")
        return []

def cosine_similarity(a, b):
    if not a or not b: return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)

import re

def rank_projects_for_jd(projects, jd_parsed_data, top_n=3):
    """
    Ranks projects using a Hybrid Score (Keyword + Semantic)
    and enforces diversity by deduplicating core project names (e.g., "iCUBE ERP").
    """
    required_skills = [s.lower() for s in jd_parsed_data.get("required_skills", [])]
    jd_text_context = " ".join(required_skills + [jd_parsed_data.get("job_role", "")])
    jd_embedding = get_embedding(jd_text_context)

    scored_projects = []

    for proj in projects:
        # Construct searchable text for the project
        bullet_text = " ".join([f"{p} {d}" for p, d in proj["bullets"]])
        proj_search_text = f"{proj['title']} {proj['tech']} {bullet_text}".lower()

        # 1. Semantic Score
        if "embedding" not in proj:
            proj["embedding"] = get_embedding(proj_search_text)
        semantic_score = cosine_similarity(proj["embedding"], jd_embedding)

        # 2. Keyword Overlap Score
        matched_skills = [skill for skill in required_skills if skill in proj_search_text]
        keyword_score = len(matched_skills) / max(len(required_skills), 1)

        # 3. Hybrid Calculation
        hybrid_score = (0.6 * keyword_score) + (0.4 * semantic_score)

        scored_projects.append({
            "project": proj,
            "score": hybrid_score,
            "matched_skills": matched_skills
        })

    # Sort by highest hybrid score
    scored_projects.sort(key=lambda x: x["score"], reverse=True)

    # 4. Enforce Project Diversity (Base-Name Deduplication)
    final_selected = []
    seen_base_names = set()

    for sp in scored_projects:
        title = sp['project']['title']
        
        # Extract base name (everything before the first '-', '–', '|', or ':')
        # If no separator exists, fall back to the first 2 words
        base_name_match = re.split(r'[-–|:]', title)
        if len(base_name_match) > 1:
            base_name = base_name_match[0].strip().lower()
        else:
            words = title.split()
            base_name = " ".join(words[:2]).lower() if len(words) >= 2 else title.lower()

        # Only add if we haven't seen a variant of this project yet
        if base_name not in seen_base_names:
            final_selected.append(sp)
            seen_base_names.add(base_name)

        if len(final_selected) == top_n:
            break

    print("\n🏆 Top Ranked Projects (Deduplicated):")
    for i, sp in enumerate(final_selected):
        title = sp['project']['title']
        print(f"  {i+1}. {title}")
        print(f"     Score: {sp['score']:.2f} | Matched: {', '.join(sp['matched_skills'])}")

    # Return just the extracted project dictionaries
    return [sp["project"] for sp in final_selected]


# --- Quick Test Execution ---
if __name__ == "__main__":
    # Test the crawler
    projs = crawl_resumes_for_projects("./resumes")
    
    # Mock JD data to test the ranker
    mock_jd = {
        "job_role": "Nest.js Developer",
        "required_skills": ["Node.js", "Nest.js", "TypeScript", "React", "Next.js", "PostgreSQL", "Redis"]
    }
    
    if projs:
        best_projects = rank_projects_for_jd(projs, mock_jd, top_n=3)