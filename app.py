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


def _build_cover_letter(row):
    """Build an APA-style sample cover letter from parsed job row metadata."""
    title = row.get("title") or "Cybersecurity Role"
    tags = row.get("tags") or []
    criteria = row.get("criteria") or []

    tag_line = ", ".join(tags) if tags else "Cybersecurity, Risk Management, Communication"
    criteria_line = "\n".join([f"- {c}" for c in criteria[:4]]) if criteria else "- Demonstrated cybersecurity fundamentals and clear communication skills"

    return (
        f"[Applicant Name]\n"
        f"[Street Address]\n"
        f"[City, State, Postcode]\n"
        f"[Email Address] | [Phone Number]\n\n"
        f"{datetime.utcnow().strftime('%B %d, %Y')}\n\n"
        f"Hiring Manager\n"
        f"[Company Name]\n"
        f"[Company Address]\n\n"
        f"Subject: Application for {title}\n\n"
        f"Dear Hiring Manager,\n\n"
        f"I am writing to apply for the {title} role. My background in cybersecurity operations, "
        f"combined with practical experience across {tag_line}, aligns with the priorities outlined in your posting. "
        f"I can contribute quickly, collaborate effectively, and help the team improve security outcomes from day one.\n\n"
        f"From your description, the highest-value criteria appear to be:\n"
        f"{criteria_line}\n\n"
        f"In comparable roles, I have supported detection and response workflows, improved analyst-ready documentation, "
        f"and translated technical findings into clear recommendations for stakeholders. I am confident this mix of "
        f"hands-on execution and communication can help your team move efficiently from alert triage to measurable risk reduction.\n\n"
        f"I would welcome a first screening phone call to discuss how my experience maps to your current needs and how "
        f"I can support rapid onboarding into this position. Thank you for your consideration.\n\n"
        f"Sincerely,\n"
        f"[Applicant Name]"
    )


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
                parsed_row["cover_letter"] = _build_cover_letter(parsed_row)
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

    return render_template("cover_letter.html", job=job, row=target)


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
