"""
resume_indexer.py

Scans a folder of resume .docx files (e.g. one file per past application,
like Founding_FullStack_Engineer_Assistly_MohammedSanaved.docx) and builds
a cached project_bank.json. The matching step for a new JD then queries
this cache instead of re-parsing raw docx files every run.

Only files that are new or changed (by content hash) since the last run
get re-parsed. Run this whenever you add or edit a resume file:

    python resume_indexer.py /path/to/resume_folder

Heuristic parsing assumes the "Featured Projects" section has:
  - a bold project title line (optionally followed by a tag like
    "[Live on Google Play Store]")
  - a "| Tech, Stack, Here" segment on the same or next line
  - bullet paragraphs starting with "\u2022"
Adjust _looks_like_project_title / _extract_projects_from_docx if your
resume files are formatted differently.
"""

import os
import sys
import json
import hashlib
import docx

INDEX_PATH = "project_bank.json"


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _looks_like_project_title(paragraph):
    if not paragraph.runs:
        return False
    first_run = paragraph.runs[0]
    text = paragraph.text.strip()
    return bool(first_run.bold) and bool(text) and not text.isupper()


def _extract_projects_from_docx(path):
    doc = docx.Document(path)
    projects = []
    current = None
    in_projects_section = False

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        upper = text.upper()
        if "FEATURED" in upper and "PROJECT" in upper:
            in_projects_section = True
            continue
        if in_projects_section and upper in ("EDUCATION", "CERTIFICATIONS"):
            in_projects_section = False
            continue
        if not in_projects_section:
            continue

        if _looks_like_project_title(para):
            if current:
                projects.append(current)
            current = {
                "title": text,
                "tech": "",
                "bullets": [],
                "source_file": os.path.basename(path),
            }
        elif current and text.startswith("\u2022"):
            current["bullets"].append(text.lstrip("\u2022 ").strip())
        elif current and "|" in text and not current["tech"]:
            current["tech"] = text.split("|", 1)[-1].strip()

    if current:
        projects.append(current)
    return projects


def build_index(folder_path, index_path=INDEX_PATH):
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            index = json.load(f)
    else:
        index = {"files": {}, "projects": []}

    changed = False
    seen_files = set()

    for fname in os.listdir(folder_path):
        if not fname.lower().endswith(".docx"):
            continue
        fpath = os.path.join(folder_path, fname)
        fhash = _file_hash(fpath)
        seen_files.add(fname)

        if index["files"].get(fname) == fhash:
            continue  # unchanged since last run, skip re-parsing

        print(f"Indexing {fname} ...")
        new_projects = _extract_projects_from_docx(fpath)
        index["projects"] = [p for p in index["projects"] if p.get("source_file") != fname]
        index["projects"].extend(new_projects)
        index["files"][fname] = fhash
        changed = True

    removed = set(index["files"]) - seen_files
    for fname in removed:
        index["projects"] = [p for p in index["projects"] if p.get("source_file") != fname]
        del index["files"][fname]
        changed = True

    if changed:
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2)
        print(f"Index updated: {len(index['projects'])} projects across {len(index['files'])} files.")
    else:
        print("No changes detected, index is already up to date.")

    return index["projects"]


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "./resumes"
    build_index(folder)
