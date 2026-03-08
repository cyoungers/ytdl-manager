import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="yt-dlp Manager")
scheduler = BackgroundScheduler()

DB_PATH = "/data/subscriptions.db"
ARCHIVES_DIR = "/data/archives"
LOGS_DIR = "/data/logs"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def init_db():
    os.makedirs(ARCHIVES_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id             TEXT PRIMARY KEY,
            url            TEXT NOT NULL,
            name           TEXT,
            output_dir     TEXT NOT NULL,
            interval_hours REAL NOT NULL DEFAULT 6.0,
            quality        TEXT NOT NULL DEFAULT '1080',
            backfill       INTEGER NOT NULL DEFAULT 0,
            date_after     TEXT,
            enabled        INTEGER NOT NULL DEFAULT 1,
            initialized    INTEGER NOT NULL DEFAULT 0,
            last_checked   TEXT,
            created_at     TEXT NOT NULL
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    if "date_after" not in cols:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN date_after TEXT")
    conn.commit()
    conn.close()


def get_sub(sub_id: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def all_subs() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM subscriptions ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

FORMAT_MAP = {
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
}

OUTPUT_TEMPLATE = "%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s"


def _run_ytdlp(sub: dict, skip_download: bool = False, date_after: Optional[str] = None) -> int:
    output_dir = sub["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    archive_path = os.path.join(ARCHIVES_DIR, f"{sub['id']}.txt")
    log_path     = os.path.join(LOGS_DIR, f"{sub['id']}.log")
    output_tmpl  = os.path.join(output_dir, OUTPUT_TEMPLATE)
    fmt          = FORMAT_MAP.get(sub["quality"], FORMAT_MAP["1080"])

    cmd = [
        "yt-dlp",
        "--download-archive",    archive_path,
        "--output",              output_tmpl,
        "--format",              fmt,
        "--merge-output-format", "mp4",
        "--no-abort-on-error",
        "--retries",             "5",
        "--fragment-retries",    "5",
        "--concurrent-fragments","2",
        "--sleep-requests",      "1.5",
        "--sleep-interval",      "2",
        "--max-sleep-interval",  "5",
        "--js-runtimes",         "node",
        "--newline",
    ]

    if skip_download:
        cmd.append("--skip-download")

    if date_after:
        cmd += ["--dateafter", date_after]
        # Stop scanning as soon as we hit a video older than date_after.
        # YouTube channels are newest-first, so this avoids paging through
        # thousands of old videos unnecessarily.
        cmd.append("--break-on-reject")

    cookies_path = "/data/cookies.txt"
    if os.path.exists(cookies_path):
        cmd += ["--cookies", cookies_path]

    cmd.append(sub["url"])

    label = "[ARCHIVE INIT]" if skip_download else "[DOWNLOAD RUN]"
    if date_after:
        label += f" date_after={date_after}"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(log_path, "a") as log:
        log.write(f"\n{'='*60}\n{timestamp}  {label}\n{'='*60}\n")
        result = subprocess.run(cmd, stdout=log, stderr=log, text=True)
        log.write(f"\nExit code: {result.returncode}\n")

    return result.returncode


def _make_job_id() -> str:
    return str(uuid.uuid4())[:8]


def _job_start(job_id, sub_id, sub_name, trigger, date_after=None):
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":      job_id,
            "sub_id":      sub_id,
            "sub_name":    sub_name,
            "trigger":     trigger,
            "date_after":  date_after,
            "status":      "running",
            "started_at":  datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "exit_code":   None,
        }


def _job_finish(job_id, exit_code):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"]      = "completed" if exit_code == 0 else "failed"
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["exit_code"]   = exit_code


def run_subscription(sub_id: str, trigger: str = "scheduler", date_after: Optional[str] = None):
    sub = get_sub(sub_id)
    if not sub or not sub["enabled"]:
        return

    job_id = _make_job_id()
    _job_start(job_id, sub_id, sub["name"], trigger, date_after)

    try:
        effective_date = date_after or sub.get("date_after")
        # Skip archive init when date_after is set — --break-on-reject means
        # yt-dlp stops at the first old video, so we never scan the full history.
        needs_init = not sub["initialized"] and not sub["backfill"]
        if needs_init and not effective_date:
            _run_ytdlp(sub, skip_download=True)
        rc = _run_ytdlp(sub, date_after=effective_date)
    except Exception as e:
        rc = -1
        with open(os.path.join(LOGS_DIR, f"{sub_id}.log"), "a") as log:
            log.write(f"\nException: {e}\n")

    _job_finish(job_id, rc)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE subscriptions SET initialized=1, last_checked=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), sub_id)
    )
    conn.commit()
    conn.close()


def schedule_sub(sub_id: str, interval_hours: float):
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        run_subscription, "interval", hours=interval_hours,
        id=job_id, args=[sub_id, "scheduler", None], replace_existing=True,
    )


def unschedule_sub(sub_id: str):
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


@app.on_event("startup")
def startup():
    init_db()
    scheduler.start()
    for sub in all_subs():
        if sub["enabled"]:
            schedule_sub(sub["id"], sub["interval_hours"])


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


class SubCreate(BaseModel):
    url:            str
    name:           Optional[str]  = None
    output_dir:     str
    interval_hours: float          = 6.0
    quality:        str            = "1080"
    backfill:       bool           = False
    date_after:     Optional[str]  = None


class SubUpdate(BaseModel):
    name:           Optional[str]   = None
    output_dir:     Optional[str]   = None
    interval_hours: Optional[float] = None
    quality:        Optional[str]   = None
    enabled:        Optional[bool]  = None
    date_after:     Optional[str]   = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/subscriptions", status_code=201)
def add_subscription(body: SubCreate):
    sub_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO subscriptions
           (id, url, name, output_dir, interval_hours, quality, backfill, date_after, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sub_id, body.url, body.name or body.url, body.output_dir,
         body.interval_hours, body.quality, int(body.backfill),
         body.date_after, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    schedule_sub(sub_id, body.interval_hours)
    threading.Thread(target=run_subscription, args=(sub_id, "initial", body.date_after), daemon=True).start()
    return {"id": sub_id, "message": "Subscription added — initial run started in background"}


@app.get("/subscriptions")
def list_subscriptions():
    subs = all_subs()
    for s in subs:
        s["enabled"]  = bool(s["enabled"])
        s["backfill"] = bool(s["backfill"])
    return subs


@app.get("/subscriptions/{sub_id}")
def get_subscription(sub_id: str):
    sub = get_sub(sub_id)
    if not sub:
        raise HTTPException(404, "Subscription not found")
    sub["enabled"]  = bool(sub["enabled"])
    sub["backfill"] = bool(sub["backfill"])
    return sub


@app.patch("/subscriptions/{sub_id}")
def update_subscription(sub_id: str, body: SubUpdate):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"UPDATE subscriptions SET {set_clause} WHERE id=?", list(updates.values()) + [sub_id])
    conn.commit()
    conn.close()
    sub = get_sub(sub_id)
    if sub["enabled"]:
        schedule_sub(sub_id, sub["interval_hours"])
    else:
        unschedule_sub(sub_id)
    return {"message": "Updated", "id": sub_id}


@app.delete("/subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    unschedule_sub(sub_id)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    return {"message": "Deleted", "id": sub_id}


@app.post("/subscriptions/{sub_id}/check")
def trigger_check(
    sub_id: str,
    date_after: Optional[str] = Query(default=None,
        description="Only download videos on/after this date. e.g. today-7days or 20250101")
):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    threading.Thread(target=run_subscription, args=(sub_id, "manual", date_after), daemon=True).start()
    return {"message": "Download triggered in background", "id": sub_id, "date_after": date_after}


@app.get("/subscriptions/{sub_id}/log")
def get_log(sub_id: str, lines: int = 100):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    log_path = os.path.join(LOGS_DIR, f"{sub_id}.log")
    if not os.path.exists(log_path):
        return {"log": "(no log yet)"}
    with open(log_path) as f:
        content = f.readlines()
    return {"log": "".join(content[-lines:])}


@app.get("/jobs")
def list_jobs(status: Optional[str] = Query(default=None, description="Filter: running | completed | failed")):
    with _jobs_lock:
        jobs = list(_jobs.values())
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    jobs.sort(key=lambda j: (j["status"] != "running", j["started_at"]))
    scheduled = []
    for job in scheduler.get_jobs():
        if job.id.startswith("sub_"):
            sub_id = job.id[4:]
            sub = get_sub(sub_id)
            scheduled.append({
                "sub_id":   sub_id,
                "sub_name": sub["name"] if sub else sub_id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
    scheduled.sort(key=lambda j: j["next_run"] or "")
    return {"jobs": jobs, "scheduled": scheduled}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job
