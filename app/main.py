import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="yt-dlp Manager")
scheduler = BackgroundScheduler()

DB_PATH = "/data/subscriptions.db"
ARCHIVES_DIR = "/data/archives"
LOGS_DIR = "/data/logs"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    os.makedirs(ARCHIVES_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id            TEXT PRIMARY KEY,
            url           TEXT NOT NULL,
            name          TEXT,
            output_dir    TEXT NOT NULL,
            interval_hours REAL NOT NULL DEFAULT 6.0,
            quality       TEXT NOT NULL DEFAULT '1080',
            backfill      INTEGER NOT NULL DEFAULT 0,
            enabled       INTEGER NOT NULL DEFAULT 1,
            initialized   INTEGER NOT NULL DEFAULT 0,
            last_checked  TEXT,
            created_at    TEXT NOT NULL
        )
    """)
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


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

FORMAT_MAP = {
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
}

OUTPUT_TEMPLATE = "%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s"


def _run_ytdlp(sub: dict, skip_download: bool = False):
    """
    Core yt-dlp invocation.
    skip_download=True populates the archive without downloading (used on
    first run when backfill=False so we don't re-download historical videos).
    """
    output_dir = sub["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    archive_path = os.path.join(ARCHIVES_DIR, f"{sub['id']}.txt")
    log_path     = os.path.join(LOGS_DIR, f"{sub['id']}.log")
    output_tmpl  = os.path.join(output_dir, OUTPUT_TEMPLATE)
    fmt          = FORMAT_MAP.get(sub["quality"], FORMAT_MAP["1080"])

    cmd = [
        "yt-dlp",
        "--download-archive", archive_path,   # never re-download
        "--output",           output_tmpl,
        "--format",           fmt,
        "--merge-output-format", "mp4",
        "--no-abort-on-error",                # keep going if one video fails
        "--retries",          "5",
        "--fragment-retries", "5",
        "--concurrent-fragments", "4",
        "--newline",                          # flush output line-by-line
    ]

    if skip_download:
        cmd.append("--skip-download")

    cmd.append(sub["url"])

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(log_path, "a") as log:
        log.write(f"\n{'='*60}\n{timestamp}  {'[ARCHIVE INIT]' if skip_download else '[DOWNLOAD RUN]'}\n{'='*60}\n")
        result = subprocess.run(cmd, stdout=log, stderr=log, text=True)
        log.write(f"\nExit code: {result.returncode}\n")

    return result.returncode


def run_subscription(sub_id: str):
    """Called by the scheduler (and on manual trigger)."""
    sub = get_sub(sub_id)
    if not sub or not sub["enabled"]:
        return

    # First-ever run: if backfill is off, populate archive without downloading
    if not sub["initialized"] and not sub["backfill"]:
        _run_ytdlp(sub, skip_download=True)

    _run_ytdlp(sub)

    # Mark initialized + update last_checked
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE subscriptions SET initialized=1, last_checked=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), sub_id)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------

def schedule_sub(sub_id: str, interval_hours: float):
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        run_subscription,
        "interval",
        hours=interval_hours,
        id=job_id,
        args=[sub_id],
        replace_existing=True,
    )


def unschedule_sub(sub_id: str):
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    init_db()
    scheduler.start()
    # Re-schedule any existing enabled subscriptions after container restart
    for sub in all_subs():
        if sub["enabled"]:
            schedule_sub(sub["id"], sub["interval_hours"])


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SubCreate(BaseModel):
    url:            str
    name:           Optional[str] = None
    output_dir:     str
    interval_hours: float = 6.0
    quality:        str   = "1080"   # 1080 | 720 | 480 | best
    backfill:       bool  = False    # True  = download all history on first run
                                     # False = only download videos posted from now on


class SubUpdate(BaseModel):
    name:           Optional[str]   = None
    output_dir:     Optional[str]   = None
    interval_hours: Optional[float] = None
    quality:        Optional[str]   = None
    enabled:        Optional[bool]  = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/subscriptions", status_code=201)
def add_subscription(body: SubCreate):
    sub_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO subscriptions
           (id, url, name, output_dir, interval_hours, quality, backfill, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            sub_id,
            body.url,
            body.name or body.url,
            body.output_dir,
            body.interval_hours,
            body.quality,
            int(body.backfill),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    schedule_sub(sub_id, body.interval_hours)

    # Kick off the first run in background so the POST returns immediately
    threading.Thread(target=run_subscription, args=(sub_id,), daemon=True).start()

    return {
        "id":      sub_id,
        "message": "Subscription added — initial run started in background",
    }


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

    # Convert enabled bool → int for SQLite
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values     = list(updates.values()) + [sub_id]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"UPDATE subscriptions SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()

    # Re-schedule if timing or enabled state changed
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
def trigger_check(sub_id: str):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    threading.Thread(target=run_subscription, args=(sub_id,), daemon=True).start()
    return {"message": "Download triggered in background", "id": sub_id}


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
