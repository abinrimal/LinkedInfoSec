#!/usr/bin/env python3
"""
LinkedInfoSec – app.py
======================
Flask web application that provides a browser-based UI for running the
LinkedInfoSec scraping pipeline.

Architecture overview
---------------------
* Each submitted job is assigned a UUID and stored in an in-memory dict
  (``jobs``) that is also persisted to a SQLite database (``jobs.db``).
* scrape.py then handle.py are spawned as child processes via
  ``subprocess.Popen`` (not ``subprocess.run``) so they can be terminated
  mid-run from the /cancel endpoint.
* A daemon ``threading.Thread`` per job drives the two-step pipeline;
  a ``threading.Lock`` guards all reads/writes to the shared ``jobs`` dict.
* The job page polls ``/api/job/<id>`` every two seconds for live updates.

Original project by ahessmat: https://github.com/ahessmat/LinkedInfoSec
Extended with Flask UI, SQLite persistence, cancel, CSV downloads, edit/delete,
and scraped-jobs table with description and date fields.
"""

import ast
import csv
import json
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "jobs.db"
app = Flask(__name__)

# In-memory job store: { job_id: job_dict }.
# All mutations must be done under jobs_lock to keep the background threads and
# the Flask request threads from racing each other.
jobs = {}
jobs_lock = threading.Lock()

# Tracks Popen objects for running jobs so /cancel can terminate them.
running_processes = {}


def _db_conn():
    """Return a SQLite connection with Row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                output_prefix TEXT NOT NULL,
                params_json TEXT NOT NULL,
                created_at TEXT,
                started_at TEXT,
                ended_at TEXT,
                scrape_log TEXT,
                handle_log TEXT,
                certs_json TEXT,
                error TEXT,
                canceled INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def _save_job(job):
    """Upsert a job dict into the database.

    Called after every status change so the UI survives an app restart.
    Must be called while holding jobs_lock (or on a freshly created job).
    """
    with _db_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, output_prefix, params_json, created_at, started_at, ended_at,
                scrape_log, handle_log, certs_json, error, canceled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                output_prefix=excluded.output_prefix,
                params_json=excluded.params_json,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                scrape_log=excluded.scrape_log,
                handle_log=excluded.handle_log,
                certs_json=excluded.certs_json,
                error=excluded.error,
                canceled=excluded.canceled
            """,
            (
                job["id"],
                job["status"],
                job["output_prefix"],
                json.dumps(job["params"]),
                job.get("created_at", ""),
                job.get("started_at", ""),
                job.get("ended_at", ""),
                job.get("scrape_log", ""),
                job.get("handle_log", ""),
                json.dumps(job.get("certs", [])),
                job.get("error", ""),
                1 if job.get("canceled") else 0,
            ),
        )
        conn.commit()


def _load_jobs_from_db():
    """Load all persisted jobs from SQLite on startup."""
    with _db_conn() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()

    loaded = {}
    for row in rows:
        loaded[row["id"]] = {
            "id": row["id"],
            "status": row["status"],
            "output_prefix": row["output_prefix"],
            "params": json.loads(row["params_json"] or "{}"),
            "created_at": row["created_at"] or "",
            "started_at": row["started_at"] or "",
            "ended_at": row["ended_at"] or "",
            "scrape_log": row["scrape_log"] or "",
            "handle_log": row["handle_log"] or "",
            "certs": json.loads(row["certs_json"] or "[]"),
            "error": row["error"] or "",
            "canceled": bool(row["canceled"]),
        }
    return loaded


def _update_job(job_id, **kwargs):
    """Thread-safe in-place update of a job dict that also persists to DB."""
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            _save_job(jobs[job_id])


def _build_scrape_command(params, output_prefix):
    """Translate the form params dict into a scrape.py argv list."""
    cmd = [
        sys.executable,
        str(BASE_DIR / "scrape.py"),
        "-j",
        params["job"],
        "-l",
        params["location"],
        "-t",
        params["time_window"],
        "-s",
        params["seniority"],
        "-i",
        str(params["increment"]),
        "-o",
        output_prefix,
        "-b",
        params["browser"],
    ]
    if params["quick"]:
        cmd.append("-q")
    if params["keywords"]:
        cmd.extend(["-k", params["keywords"]])
    if params["max_jobs"] is not None:
        cmd.extend(["--max", str(params["max_jobs"])])
    return cmd


def _run_command(job_id, cmd):
    """Run a subprocess and capture combined stdout+stderr.

    We use Popen instead of subprocess.run so that the process handle is stored
    in running_processes, allowing /cancel to call proc.terminate() at any time.
    Returns (returncode, combined_output_str).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with jobs_lock:
        running_processes[job_id] = proc
    stdout, stderr = proc.communicate()
    with jobs_lock:
        running_processes.pop(job_id, None)
    return proc.returncode, (stdout or "") + "\n" + (stderr or "")


def _is_canceled(job_id):
    """Return True if the job has been flagged for cancellation."""
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.get("canceled"))


def _run_pipeline(job_id):
    """Background thread target: run scrape.py then handle.py in sequence.

    After each step we check the canceled flag so a cancel request takes effect
    promptly rather than waiting for the entire pipeline to finish.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    output_prefix = job["output_prefix"]
    params = job["params"]

    _update_job(job_id, status="running", started_at=datetime.utcnow().isoformat() + "Z")

    scrape_cmd = _build_scrape_command(params, output_prefix)
    scrape_code, scrape_log = _run_command(job_id, scrape_cmd)
    _update_job(job_id, scrape_log=scrape_log.strip())

    if _is_canceled(job_id):
        _update_job(
            job_id,
            status="canceled",
            error="Canceled by user",
            ended_at=datetime.utcnow().isoformat() + "Z",
        )
        return

    if scrape_code != 0:
        _update_job(
            job_id,
            status="failed",
            error="scrape.py failed",
            ended_at=datetime.utcnow().isoformat() + "Z",
        )
        return

    info_file = f"{output_prefix}_allinfo.csv"
    handle_cmd = [
        sys.executable,
        str(BASE_DIR / "handle.py"),
        "-f",
        info_file,
    ]

    handle_code, handle_log = _run_command(job_id, handle_cmd)
    raw_result = ""
    if handle_log.strip():
        raw_result = handle_log.strip().splitlines()[0]

    if _is_canceled(job_id):
        _update_job(
            job_id,
            status="canceled",
            error="Canceled by user",
            handle_log=handle_log.strip(),
            ended_at=datetime.utcnow().isoformat() + "Z",
        )
        return

    if handle_code != 0:
        _update_job(
            job_id,
            status="failed",
            error="handle.py failed",
            handle_log=handle_log.strip(),
            ended_at=datetime.utcnow().isoformat() + "Z",
        )
        return

    certs = {}
    if raw_result:
        try:
            parsed = ast.literal_eval(raw_result)
            if isinstance(parsed, dict):
                certs = parsed
        except (ValueError, SyntaxError):
            certs = {}

    sorted_certs = sorted(certs.items(), key=lambda x: x[1], reverse=True)

    _update_job(
        job_id,
        status="completed",
        certs=sorted_certs,
        handle_log=handle_log.strip(),
        ended_at=datetime.utcnow().isoformat() + "Z",
    )


def _delete_job_files(job):
    """Delete all CSV output files associated with a job."""
    for artifact in ("allinfo", "data", "allcerts"):
        path = _output_file_for(job, artifact)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _output_file_for(job, artifact):
    """Resolve the filesystem path for one of a job's CSV output files."""
    output_prefix = job["output_prefix"]
    suffixes = {
        "allinfo": "_allinfo.csv",
        "data": "_data.csv",
        "allcerts": "_all_certs.csv",
    }
    if artifact not in suffixes:
        return None
    return BASE_DIR / f"{output_prefix}{suffixes[artifact]}"


def _extract_tags_and_criteria(description):
    """Extract job tags and concise criteria lines from a description."""
    if not description:
        return [], []

    description_lower = description.lower()
    tag_patterns = {
        "SIEM": r"\bsiem\b|\bsplunk\b|\bqradar\b|\bsentinel\b",
        "Incident Response": r"incident\s+response|\bdfir\b|threat\s+hunting",
        "Cloud Security": r"cloud\s+security|\baws\b|\bazure\b|\bgcp\b",
        "Network Security": r"network\s+security|firewall|ids|ips|zero\s+trust",
        "IAM": r"\biam\b|identity\s+and\s+access|okta|entra|active\s+directory",
        "Governance/Risk": r"\bgrc\b|risk\s+management|compliance|iso\s*27001|nist",
        "Penetration Testing": r"penetration\s+testing|pentest|red\s+team|vulnerability\s+assessment",
        "Scripting": r"\bpython\b|\bpowershell\b|\bbash\b|automation",
    }

    tags = [name for name, pattern in tag_patterns.items() if re.search(pattern, description_lower)]

    raw_parts = re.split(r"(?<=[\.!?])\s+|\s*;\s*", description)
    criteria = []
    criteria_signals = (
        "required", "must", "experience", "ability", "knowledge", "proficient",
        "familiar", "hands-on", "responsible", "skills", "qualifications"
    )
    for part in raw_parts:
        cleaned = " ".join(part.strip().split())
        if not cleaned:
            continue
        if any(signal in cleaned.lower() for signal in criteria_signals):
            criteria.append(cleaned)
        if len(criteria) >= 6:
            break

    return tags[:8], criteria


def _extract_key_skills(description, criteria):
    """Extract key skill keywords from description and criteria text."""
    text = f"{description} {' '.join(criteria)}".lower()
    skill_patterns = {
        "Python": r"\bpython\b",
        "Django": r"\bdjango\b",
        "Laravel": r"\blaravel\b",
        "DevOps": r"\bdevops\b|\bcicd\b|\bkubernetes\b|\bdocker\b",
        "Cloud": r"\baws\b|\bazure\b|\bgcp\b|cloud",
        "WordPress": r"\bwordpress\b",
        "Shopify": r"\bshopify\b",
        "SIEM": r"\bsiem\b|\bsplunk\b|\bqradar\b|\bsentinel\b",
        "Incident Response": r"incident\s+response|\bdfir\b|threat\s+hunting",
        "Risk & Compliance": r"\bgrc\b|risk\s+management|compliance|iso\s*27001|nist",
        "Communication": r"communication|stakeholder|collaboration",
        "Leadership": r"leadership|lead|mentor|management",
    }
    skills = [name for name, pattern in skill_patterns.items() if re.search(pattern, text)]
    return skills[:10]


def _clean_requirement_text(text):
    """Normalize requirement fragments so they read naturally in prose."""
    cleaned = " ".join((text or "").split()).strip(" .;:-")
    return cleaned[:140]


def _build_cover_letter(row, candidate):
    """Build a professional 300-400 word cover letter tailored to the role."""
    title = row.get("title") or "Cybersecurity Role"
    tags = row.get("tags") or []
    criteria = row.get("criteria") or []
    description = (row.get("description") or "").lower()
    key_skills = _extract_key_skills(row.get("description", ""), criteria)

    name = candidate.get("name", "Applicant Name")
    email = candidate.get("email", "applicant@example.com")
    phone = candidate.get("phone", "+00 000 000 000")
    experience = candidate.get("experience", "Relevant professional experience")
    education = candidate.get("education", "Relevant qualification")
    location = candidate.get("location", "Sydney, Australia")
    additional_notes = candidate.get("additional_notes", "Strong problem-solving and collaborative mindset")

    years_match = re.search(r"(\d+\+?)\s*years", experience.lower())
    years_text = f"over {years_match.group(1)} years" if years_match else "several years"

    # Build a role-relevant skill summary from overlap with extracted job skills.
    exp_lower = experience.lower()
    matched_profile_skills = [skill for skill in key_skills if skill.lower() in exp_lower]
    if not matched_profile_skills:
        matched_profile_skills = key_skills[:3] if key_skills else tags[:3]
    if matched_profile_skills:
        if len(matched_profile_skills) == 1:
            profile_skills_text = matched_profile_skills[0]
        elif len(matched_profile_skills) == 2:
            profile_skills_text = f"{matched_profile_skills[0]} and {matched_profile_skills[1]}"
        else:
            profile_skills_text = ", ".join(matched_profile_skills[:2]) + f", and {matched_profile_skills[2]}"
    else:
        profile_skills_text = "execution, collaboration, and pragmatic problem solving"

    education_phrase = " ".join(education.split()).strip(" .")
    if education_phrase:
        education_context = f"My academic foundation in {education_phrase} supports a structured approach to analysis, prioritization, and decision-making"
    else:
        education_context = "My academic background supports a structured approach to analysis, prioritization, and decision-making"

    notes_phrase = " ".join(additional_notes.split()).strip(" .")
    if notes_phrase:
        notes_context = f"I also bring {notes_phrase.lower()}, which helps me align teams and maintain momentum through ambiguity"
    else:
        notes_context = "I also bring strong ownership and communication habits, which help maintain momentum through ambiguity"

    title_lower = title.lower()
    is_security_role = any(word in title_lower for word in ["cyber", "security", "soc", "threat", "grc", "iso 27001"]) or any(
        "security" in tag.lower() for tag in tags
    ) or any(word in description for word in ["cyber", "security", "incident response", "siem", "vulnerability"])

    if is_security_role:
        focus_phrase = "secure-by-design delivery, risk reduction, and reliable incident-ready operations"
        impact_line = (
            "In comparable roles, I have strengthened monitoring and response outcomes, improved documentation quality, "
            "and translated technical findings into practical actions for business stakeholders"
        )
    else:
        focus_phrase = "structured execution, delivery quality, and measurable business outcomes"
        impact_line = (
            "In comparable roles, I have delivered measurable results through disciplined execution, cross-functional collaboration, "
            "and clear communication with technical and non-technical stakeholders"
        )

    tag_line = ", ".join(tags[:4]) if tags else "collaboration, ownership, and continuous improvement"
    skills_line = ", ".join(key_skills[:6]) if key_skills else "problem solving, communication, and delivery excellence"
    reqs = [_clean_requirement_text(c) for c in criteria if _clean_requirement_text(c)]
    if reqs:
        if len(reqs) == 1:
            requirements_prose = reqs[0]
        elif len(reqs) == 2:
            requirements_prose = f"{reqs[0]} and {reqs[1]}"
        else:
            requirements_prose = f"{reqs[0]}, {reqs[1]}, and {reqs[2]}"
    else:
        requirements_prose = "strong delivery ownership, practical problem-solving, and effective stakeholder communication"

    return (
        f"{name}\n"
        f"{location}\n"
        f"{email} | {phone}\n\n"
        f"{datetime.utcnow().strftime('%B %d, %Y')}\n\n"
        f"Hiring Manager\n"
        f"[Company Name]\n"
        f"[Company Address]\n\n"
        f"Subject: Application for {title}\n\n"
        f"Dear Hiring Manager,\n\n"
        f"I am pleased to apply for the {title} position. With {years_text} of hands-on delivery experience, I have developed a practical, "
        f"results-focused approach to delivering high-quality outcomes in dynamic environments. I am motivated by the opportunity "
        f"to contribute in {location} and support your team with dependable execution, clear communication, and strong accountability from the outset.\n\n"
        f"My recent work has required me to apply skills such as {skills_line}, with particular depth in {profile_skills_text}, while consistently balancing technical depth with business priorities. "
        f"This experience has strengthened my ability to work across {tag_line} and align delivery with organizational goals. {education_context}. {notes_context}. "
        f"As reflected in your job description, the role emphasizes {requirements_prose}, and these are areas where I have demonstrated consistent performance.\n\n"
        f"Beyond role fit, I would bring immediate value through {focus_phrase}. {impact_line}. I am comfortable taking ownership of complex tasks, "
        f"collaborating across teams, and maintaining momentum from planning through execution. I also prioritize clean documentation and transparent updates "
        f"so stakeholders can make timely, informed decisions throughout the project lifecycle.\n\n"
        f"I would welcome the opportunity to discuss how my background aligns with your priorities and how I can contribute quickly in this role. "
        f"Thank you for your time and consideration, and I look forward to the possibility of speaking with you.\n\n"
        f"Sincerely,\n"
        f"{name}"
    )


def _parse_education_details(education_text):
    """Parse education text into degree, field, year, institution."""
    text = " ".join((education_text or "").split()).strip()
    result = {
        "degree": "",
        "field": "",
        "year": "",
        "institution": "",
    }
    if not text:
        return result

    result["degree"] = text
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match:
        result["year"] = year_match.group(0)

    field_match = re.search(r"in\s+([A-Za-z][A-Za-z\s&/]+)", text, re.I)
    if field_match:
        result["field"] = " ".join(field_match.group(1).split())

    parts = [p.strip() for p in re.split(r",|\|| - ", text) if p.strip()]
    if len(parts) >= 2:
        result["institution"] = parts[-1]

    return result


def _rewrite_experience_bullets(raw_points, req_summary, ats_keywords):
    """Rewrite responsibilities into concise JD-aligned, ATS-friendly bullets."""
    points = [" ".join((p or "").split()).strip(" .") for p in raw_points if p and str(p).strip()]
    if not points:
        points = [
            "Delivered role responsibilities in fast-paced environments",
            "Coordinated with stakeholders to keep priorities aligned",
        ]

    rewritten = []
    for idx, p in enumerate(points[:3]):
        if idx == 0:
            rewritten.append(f"Applied {p.lower()} while aligning delivery with priorities in {req_summary}.")
        elif idx == 1:
            keyword = ats_keywords[0] if ats_keywords else "key job requirements"
            rewritten.append(f"Improved quality and consistency by using {keyword} practices and clear execution standards.")
        else:
            rewritten.append(f"Collaborated across teams to resolve blockers quickly and maintain reliable delivery outcomes.")

    metrics = re.findall(r"\b\d+%\b|\$\s?\d[\d,]*(?:\.\d+)?|\b\d+\+?\s+(?:projects|clients|tickets|incidents|reports|users|systems)\b", " ".join(points), re.I)
    if metrics:
        rewritten.append(f"Delivered measurable outcomes including {metrics[0]}, supporting stronger business performance.")

    if len(rewritten) < 4:
        keyword = ats_keywords[1] if len(ats_keywords) > 1 else "stakeholder communication"
        rewritten.append(f"Produced concise documentation and updates to improve {keyword} and decision-making speed.")

    return rewritten[:4]


def _parse_candidate_experience_entries(experience_text, target_role, location, req_summary, ats_keywords):
    """Parse candidate profile experience into structured entries.

    Supported formats:
    1) JSON list of objects with keys: title, company, dates, location,
       employment_type, responsibilities, achievements.
    2) Pipe-delimited lines:
       title | company | dates | location | employment_type | responsibility...
    3) Fallback plain text summary (single inferred role entry).
    """
    text = (experience_text or "").strip()
    if not text:
        return []

    entries = []

    # JSON format support
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("job_title") or target_role).strip()
                company = str(item.get("company") or "").strip()
                dates = str(item.get("dates") or "").strip()
                loc = str(item.get("location") or location or "").strip()
                emp_type = str(item.get("employment_type") or "").strip()

                responsibilities = item.get("responsibilities") or []
                achievements = item.get("achievements") or []
                raw_points = []
                if isinstance(responsibilities, str):
                    raw_points.extend([p.strip() for p in responsibilities.split(";") if p.strip()])
                elif isinstance(responsibilities, list):
                    raw_points.extend([str(p).strip() for p in responsibilities if str(p).strip()])

                if isinstance(achievements, str):
                    raw_points.extend([p.strip() for p in achievements.split(";") if p.strip()])
                elif isinstance(achievements, list):
                    raw_points.extend([str(p).strip() for p in achievements if str(p).strip()])

                bullets = _rewrite_experience_bullets(raw_points, req_summary, ats_keywords)
                entries.append({
                    "title": title,
                    "company": company,
                    "dates": dates,
                    "location": loc,
                    "employment_type": emp_type,
                    "bullets": bullets,
                })
    except json.JSONDecodeError:
        pass

    if entries:
        return entries

    # Pipe-delimited lines support
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()]:
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        title, company, dates, loc, emp_type = parts[:5]
        raw_rest = parts[5:]
        raw_points = []
        for chunk in raw_rest:
            raw_points.extend([p.strip() for p in re.split(r";|\\. ", chunk) if p.strip()])
        bullets = _rewrite_experience_bullets(raw_points, req_summary, ats_keywords)
        entries.append({
            "title": title,
            "company": company,
            "dates": dates,
            "location": loc,
            "employment_type": emp_type,
            "bullets": bullets,
        })

    if entries:
        return entries

    # Fallback: infer one role entry from plain text profile summary.
    fallback_title = target_role if target_role and target_role != "Target Role" else "Professional Experience"
    raw_points = [p.strip() for p in re.split(r",|;", text) if p.strip()]
    return [{
        "title": fallback_title,
        "company": "",
        "dates": "",
        "location": location or "",
        "employment_type": "",
        "bullets": _rewrite_experience_bullets(raw_points, req_summary, ats_keywords),
    }]


def _build_resume(row, candidate):
    """Build a structured, job-tailored resume payload for template rendering."""
    title = row.get("title") or "Target Role"
    tags = row.get("tags") or []
    criteria = row.get("criteria") or []
    description = row.get("description") or ""
    key_skills = _extract_key_skills(description, criteria)

    name = candidate.get("name", "Applicant Name")
    email = candidate.get("email", "applicant@example.com")
    phone = candidate.get("phone", "+00 000 000 000")
    experience = candidate.get("experience", "Relevant professional experience")
    education = candidate.get("education", "Relevant qualification")
    location = candidate.get("location", "Sydney, Australia")
    additional_notes = candidate.get("additional_notes", "Strong problem-solving and collaborative mindset")

    exp_text = " ".join(experience.split()).strip()
    years_match = re.search(r"(\d+\+?)\s*years", exp_text.lower())
    years_text = f"over {years_match.group(1)} years" if years_match else "several years"

    # Extract profile skills from candidate experience text and align with JD skills.
    profile_skill_patterns = {
        "Python": r"\bpython\b",
        "Django": r"\bdjango\b",
        "Laravel": r"\blaravel\b",
        "DevOps": r"\bdevops\b|\bcicd\b|\bkubernetes\b|\bdocker\b",
        "WordPress": r"\bwordpress\b",
        "Shopify": r"\bshopify\b",
        "Cloud": r"\baws\b|\bazure\b|\bgcp\b|cloud",
        "Accounting Systems": r"xero|myob|quickbooks|erp",
        "Reconciliation": r"reconciliation|reconcile",
        "Financial Reporting": r"financial\s+reporting|month-end|month\s+end",
        "Stakeholder Communication": r"stakeholder|communication|cross-functional",
        "Leadership": r"leadership|team\s+lead|management",
    }
    profile_skills = [
        name for name, pattern in profile_skill_patterns.items() if re.search(pattern, exp_text.lower())
    ]

    title_lower = title.lower()
    is_finance_role = any(word in title_lower for word in ["accountant", "account", "finance", "bookkeeper", "payroll", "ap", "ar"])
    is_security_role = any(word in title_lower for word in ["cyber", "security", "soc", "threat", "grc"]) or any(
        "security" in tag.lower() for tag in tags
    )

    ats_keywords = list(dict.fromkeys((key_skills + tags)[:12]))
    if not ats_keywords:
        ats_keywords = ["Problem Solving", "Communication", "Stakeholder Management"]

    # Prioritize overlap between candidate profile and JD-derived ATS keywords.
    matched_ats = [k for k in ats_keywords if any(k.lower() in s.lower() for s in profile_skills + [exp_text])]
    if not matched_ats:
        matched_ats = ats_keywords[:4]

    reqs = [_clean_requirement_text(c) for c in criteria if _clean_requirement_text(c)]
    if reqs:
        if len(reqs) == 1:
            req_summary = reqs[0]
        elif len(reqs) == 2:
            req_summary = f"{reqs[0]} and {reqs[1]}"
        else:
            req_summary = f"{reqs[0]}, {reqs[1]}, and {reqs[2]}"
    else:
        req_summary = "delivery ownership, stakeholder communication, and problem solving"

    # Build 3-5 sentence professional summary with integrated achievements/value.
    notes_phrase = " ".join(additional_notes.split()).strip(" .")
    if is_finance_role:
        summary = (
            f"Results-oriented professional with {years_text} of experience supporting high-accuracy finance and operations delivery. "
            f"I bring practical capability in {', '.join(matched_ats[:3]) if matched_ats else 'reconciliation, reporting, and stakeholder communication'}, "
            f"and consistently align execution with priorities such as {req_summary}. "
            f"My experience combines process discipline, quality control, and cross-functional collaboration to improve turnaround time and reporting reliability. "
            f"{notes_phrase.capitalize() if notes_phrase else 'I am known for dependable ownership and clear communication.'}"
        )
    elif is_security_role:
        summary = (
            f"Security-focused professional with {years_text} of practical experience delivering operational and risk-reduction outcomes. "
            f"I apply strengths in {', '.join(matched_ats[:3]) if matched_ats else 'incident coordination, communication, and documentation'} "
            f"to align execution with role priorities including {req_summary}. "
            f"I have a strong track record of translating technical issues into actionable steps that improve resilience and delivery quality. "
            f"{notes_phrase.capitalize() if notes_phrase else 'I bring calm ownership and clear stakeholder communication in high-pressure contexts.'}"
        )
    else:
        summary = (
            f"Results-driven professional with {years_text} of experience delivering measurable outcomes across dynamic, cross-functional environments. "
            f"I bring transferable strengths in {', '.join(matched_ats[:3]) if matched_ats else 'problem solving, communication, and execution'} and align work to priorities such as {req_summary}. "
            f"My approach combines structured execution, proactive collaboration, and practical decision-making to keep delivery on track and outcomes reliable. "
            f"{notes_phrase.capitalize() if notes_phrase else 'I am known for dependable ownership and continuous improvement.'}"
        )

    if is_finance_role:
        work_bullets = [
            f"Applied {exp_text} to support finance operations aligned with {req_summary}, improving processing reliability and turnaround time.",
            f"Used {', '.join(matched_ats[:2]) if len(matched_ats) >= 2 else matched_ats[0]} to improve reconciliation accuracy and reduce month-end rework.",
            "Partnered with operational and leadership stakeholders to provide clear status updates, risk visibility, and practical recommendations.",
            "Contributed to reporting and control improvements that strengthened audit readiness and supported compliant, data-driven decisions.",
        ]
        skills = list(dict.fromkeys(matched_ats + profile_skills + ["Reconciliation", "Financial Reporting", "Attention to Detail"]))[:12]
    elif is_security_role:
        work_bullets = [
            f"Applied {exp_text} to deliver outcomes aligned to {req_summary}, improving consistency and response quality across security operations.",
            f"Leveraged {', '.join(matched_ats[:2]) if len(matched_ats) >= 2 else matched_ats[0]} to prioritize actionable findings and support timely risk treatment.",
            "Created clear documentation and handover artifacts that improved team alignment, knowledge transfer, and onboarding speed.",
            "Collaborated across teams to embed practical security improvements without slowing delivery velocity.",
        ]
        skills = list(dict.fromkeys(matched_ats + profile_skills + ["Risk Reduction", "Incident Coordination", "Security Documentation"]))[:12]
    else:
        work_bullets = [
            f"Applied {exp_text} to execute priorities aligned with {req_summary}, ensuring reliable delivery and measurable progress.",
            f"Used {', '.join(matched_ats[:2]) if len(matched_ats) >= 2 else matched_ats[0]} to unblock issues quickly and maintain delivery momentum.",
            "Worked with technical and non-technical stakeholders to clarify expectations, risks, and dependencies early.",
            "Improved process clarity through concise documentation, reusable templates, and proactive status communication.",
        ]
        skills = list(dict.fromkeys(matched_ats + profile_skills + ["Execution", "Stakeholder Communication", "Process Improvement"]))[:12]

    notes_raw = [n.strip() for n in additional_notes.split(",") if n.strip()]
    notes_items = []
    for idx, n in enumerate(notes_raw[:3]):
        keyword = matched_ats[idx % len(matched_ats)] if matched_ats else "role priorities"
        notes_items.append(
            f"Applied {n.lower()} to strengthen {keyword} outcomes and improve delivery consistency."
        )
    if not notes_items:
        notes_items = [
            "Demonstrated strong ownership and collaborative communication to support role priorities and delivery quality."
        ]

    certs = re.findall(r"'([^']+)'", row.get("certs", "") or "")
    if certs:
        notes_items.append("Relevant certifications include " + ", ".join(certs[:5]) + ".")

    experience_entries = _parse_candidate_experience_entries(exp_text, title, location, req_summary, matched_ats)

    return {
        "target_role": title,
        "contact": {
            "name": name,
            "email": email,
            "phone": phone,
            "location": location,
        },
        "professional_summary": summary,
        "education": _parse_education_details(education),
        "work_experience": experience_entries,
        "skills": skills,
        "additional_notes": notes_items,
    }


def _parse_allinfo_rows(job, max_rows=400):
    """Parse *_allinfo.csv and return a list of scraped-job dicts.

    The CSV was written by write_results_to_file() in scrape.py using
    csv.writer, so we read it back with csv.reader to handle quoted fields
    that may contain commas (e.g. descriptions with commas).

    Returns up to max_rows rows as:
        [{job_id, title, certs, description, open_date, close_date}, ...]
    """
    path = _output_file_for(job, "allinfo")
    if not path or not path.exists():
        return []

    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 3:
                    continue
                job_id = row[0].strip()
                if not job_id.isdigit():
                    continue
                certs_text = (row[2] or "").strip()
                if certs_text in {"set()", "{}"}:
                    certs_text = ""
                description = row[3] if len(row) > 3 else ""
                if certs_text == "{'CEH'}" and "ceh" not in description.lower():
                    certs_text = ""
                tags, criteria = _extract_tags_and_criteria(description)
                parsed_row = {
                    "job_id": job_id,
                    "title": row[1],
                    "certs": certs_text,
                    "description": description,
                    "open_date": row[4] if len(row) > 4 else "",
                    "close_date": row[5] if len(row) > 5 else "",
                    "tags": tags,
                    "criteria": criteria,
                }
                parsed_row["cover_letter"] = ""
                rows.append({
                    **parsed_row
                })
                if len(rows) >= max_rows:
                    break
    except OSError:
        return []

    return rows


@app.route("/clear-completed", methods=["POST"])
def clear_completed():
    """Delete all completed, failed, and canceled jobs from memory and database."""
    with jobs_lock:
        to_delete = [
            jid for jid, j in jobs.items()
            if j["status"] in {"completed", "failed", "canceled"}
        ]
        for jid in to_delete:
            _delete_job_files(jobs[jid])
            del jobs[jid]
    
    with _db_conn() as conn:
        conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed', 'failed', 'canceled')"
        )
        conn.commit()
    
    return redirect(url_for("index"))


@app.route("/")
def index():
    """Home page: scrape form + all-jobs table."""
    with jobs_lock:
        recent_jobs = sorted(jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)
    return render_template("index.html", recent_jobs=recent_jobs)


@app.route("/start", methods=["POST"])
def start_job():
    """Create a new job from form data, persist it, and launch the pipeline thread.

    If ``original_job_id`` is included in the form (submitted by the Edit modal),
    the original job and its CSV files are deleted first.
    """
    job = request.form.get("job", "cybersecurity").strip() or "cybersecurity"
    location = request.form.get("location", "remote").strip() or "remote"
    keywords = request.form.get("keywords", "").strip()
    seniority = request.form.get("seniority", "12345").strip() or "12345"
    time_window = request.form.get("time_window", "all").strip() or "all"
    browser = request.form.get("browser", "chrome").strip() or "chrome"
    output_name = request.form.get("output_name", "web_run").strip() or "web_run"

    try:
        increment = float(request.form.get("increment", "1.5"))
    except ValueError:
        increment = 1.5

    max_jobs_raw = request.form.get("max_jobs", "").strip()
    max_jobs = int(max_jobs_raw) if max_jobs_raw.isdigit() else None

    quick = request.form.get("quick") == "on"

    job_id = uuid4().hex[:8]
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_prefix = f"{output_name}_{timestamp}_{job_id}"

    params = {
        "job": job,
        "location": location,
        "keywords": keywords,
        "seniority": seniority,
        "time_window": time_window,
        "browser": browser,
        "increment": increment,
        "max_jobs": max_jobs,
        "quick": quick,
        "output_name": output_name,
    }

    job_data = {
        "id": job_id,
        "status": "queued",
        "params": params,
        "output_prefix": output_prefix,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "scrape_log": "",
        "handle_log": "",
        "certs": [],
        "error": "",
        "canceled": False,
    }

    with jobs_lock:
        jobs[job_id] = job_data
        _save_job(job_data)

    # If this is an edit of an existing job, clean it up now
    original_job_id = request.form.get("original_job_id", "").strip()
    if original_job_id:
        with jobs_lock:
            orig = jobs.get(original_job_id)
            if orig:
                if orig["status"] == "running":
                    proc = running_processes.get(original_job_id)
                    if proc and proc.poll() is None:
                        proc.terminate()
                    running_processes.pop(original_job_id, None)
                orig_copy = dict(orig)
                del jobs[original_job_id]
        if orig:
            _delete_job_files(orig_copy)
            with _db_conn() as conn:
                conn.execute("DELETE FROM jobs WHERE id = ?", (original_job_id,))
                conn.commit()

    thread = threading.Thread(target=_run_pipeline, args=(job_id,), daemon=True)
    thread.start()

    return redirect(url_for("job_page", job_id=job_id))


@app.route("/job/<job_id>")
def job_page(job_id):
    """Job detail page (server-side render; JS polling keeps it live)."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    view_job = dict(job)
    view_job["scraped_jobs"] = _parse_allinfo_rows(job)
    return render_template("job.html", job=view_job)


@app.route("/job/<job_id>/cover-letter/<linkedin_job_id>")
def cover_letter_page(job_id, linkedin_job_id):
    """Render a generated cover letter page for one scraped LinkedIn job row."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)

    rows = _parse_allinfo_rows(job)
    target = next((r for r in rows if r.get("job_id") == linkedin_job_id), None)
    if not target:
        abort(404)

    candidate = {
        "name": (request.args.get("name", "") or "Abin Rimal").strip(),
        "email": (request.args.get("email", "") or "abinrimal7@gmail.com").strip(),
        "phone": (request.args.get("phone", "") or "+61 040 343 079").strip(),
        "experience": (request.args.get("experience", "") or "7+ years in software development, skilled in Python, Laravel, Django, DevOps, WordPress, Shopify").strip(),
        "education": (request.args.get("education", "") or "Master's in IT").strip(),
        "location": (request.args.get("location", "") or "Sydney, Australia").strip(),
        "additional_notes": (request.args.get("additional_notes", "") or "Strong problem-solving, team leadership, and project management experience").strip(),
    }
    target = dict(target)
    target["cover_letter"] = _build_cover_letter(target, candidate)
    resume = _build_resume(target, candidate)

    return render_template("cover_letter.html", job=job, row=target, candidate=candidate, resume=resume)


@app.route("/api/job/<job_id>")
def job_api(job_id):
    """JSON API polled by the job page every 2 s for live status updates."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    api_job = dict(job)
    api_job["scraped_jobs"] = _parse_allinfo_rows(job)
    return jsonify(api_job)


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Signal a running job to stop at the next pipeline checkpoint.

    Sets the `canceled` flag on the job dict; the pipeline thread checks this
    flag between steps and marks the job canceled before exiting.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] in {"completed", "failed", "canceled"}:
            return jsonify({"ok": True, "status": job["status"]})
        job["canceled"] = True
        proc = running_processes.get(job_id)
        if proc and proc.poll() is None:
            proc.terminate()
        _save_job(job)
    return jsonify({"ok": True, "status": "canceling"})


@app.route("/rerun/<job_id>", methods=["POST"])
def rerun_job(job_id):
    """Create a new job using the same parameters as an existing job.

    Useful for re-running a completed, failed, or canceled job without having
    to navigate back to the home page and manually re-fill the form.
    The original job is left untouched.
    """
    with jobs_lock:
        source = jobs.get(job_id)
    if not source:
        abort(404)

    params = dict(source["params"])
    new_job_id = uuid4().hex[:8]
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    output_name = params.get("output_name", "web_run")
    output_prefix = f"{output_name}_{timestamp}_{new_job_id}"

    job_data = {
        "id": new_job_id,
        "status": "queued",
        "params": params,
        "output_prefix": output_prefix,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "scrape_log": "",
        "handle_log": "",
        "certs": [],
        "error": "",
        "canceled": False,
    }

    with jobs_lock:
        jobs[new_job_id] = job_data
        _save_job(job_data)

    thread = threading.Thread(target=_run_pipeline, args=(new_job_id,), daemon=True)
    thread.start()

    return redirect(url_for("job_page", job_id=new_job_id))


@app.route("/delete/<job_id>", methods=["POST"])
def delete_job(job_id):
    """Remove a job from memory, the database, and its CSV output files.

    If the job is currently running, its subprocess is terminated first.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            abort(404)
        if job["status"] == "running":
            proc = running_processes.get(job_id)
            if proc and proc.poll() is None:
                proc.terminate()
            running_processes.pop(job_id, None)
        job_copy = dict(job)
        del jobs[job_id]
    _delete_job_files(job_copy)
    with _db_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    return redirect(url_for("index"))


@app.route("/download/<job_id>/<artifact>")
def download_artifact(job_id, artifact):
    """Stream a CSV output file as an attachment download.

    artifact must be one of: ``allinfo``, ``data``, ``allcerts``.
    Returns 404 if the job or file does not exist.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    path = _output_file_for(job, artifact)
    if not path or not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


_init_db()
with jobs_lock:
    jobs.update(_load_jobs_from_db())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
