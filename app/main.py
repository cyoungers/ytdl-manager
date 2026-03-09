import os
import re
import sqlite3
import subprocess
import threading
import uuid
import urllib.request
import xml.etree.ElementTree as ET
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
_running_subs: set[str] = set()
_running_subs_lock = threading.Lock()

RSS_NS = {
    "atom":   "http://www.w3.org/2005/Atom",
    "yt":     "http://www.youtube.com/xml/schemas/2015",
    "media":  "http://search.yahoo.com/mrss/",
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
            channel_id     TEXT,
            enabled        INTEGER NOT NULL DEFAULT 1,
            initialized    INTEGER NOT NULL DEFAULT 0,
            last_checked   TEXT,
            created_at     TEXT NOT NULL
        )
    """)
    # Migrations for existing installs
    cols = [r[1] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    for col, typedef in [("date_after", "TEXT"), ("channel_id", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {typedef}")
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
# URL helpers
# ---------------------------------------------------------------------------

def _is_playlist(url: str) -> bool:
    return "playlist?list=" in url or "/playlist/" in url


def _extract_channel_id_from_url(url: str) -> Optional[str]:
    """Extract UC... channel ID directly from URL if present."""
    m = re.search(r"/channel/(UC[\w-]+)", url)
    return m.group(1) if m else None


def _resolve_channel_id(sub: dict) -> Optional[str]:
    """
    Return the UC... channel ID for a subscription.
    Tries (in order):
      1. Already cached in DB
      2. Extract directly from URL (e.g. /channel/UC...)
      3. Scrape the YouTube channel page HTML (works for @handles, /c/, /user/)
      4. Fall back to yt-dlp --print channel_id
    Caches result in the DB so we only resolve once.
    """
    if sub.get("channel_id"):
        return sub["channel_id"]

    channel_id = _extract_channel_id_from_url(sub["url"])

    if not channel_id:
        # Scrape the channel page — YouTube embeds the UC... ID in the HTML
        try:
            req = urllib.request.Request(
                sub["url"],
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/120.0.0.0 Safari/537.36"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            # YouTube embeds channel ID in several places in the HTML
            for pattern in [
                r'"channelId":"(UC[\w-]+)"',
                r'"externalId":"(UC[\w-]+)"',
                r'channel/(UC[\w-]+)',
            ]:
                m = re.search(pattern, html)
                if m:
                    channel_id = m.group(1)
                    break
        except Exception:
            pass

    if not channel_id:
        # Last resort: yt-dlp (slow, may be rate-limited)
        try:
            result = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--playlist-items", "1",
                 "--print", "channel_id", "--quiet", sub["url"]],
                capture_output=True, text=True, timeout=30
            )
            cid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
            if cid and cid.startswith("UC"):
                channel_id = cid
        except Exception:
            pass

    if channel_id:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE subscriptions SET channel_id=? WHERE id=?",
                     (channel_id, sub["id"]))
        conn.commit()
        conn.close()

    return channel_id


# ---------------------------------------------------------------------------
# RSS feed helpers (channels only)
# ---------------------------------------------------------------------------

def _fetch_rss_video_ids(channel_id: str) -> list[str]:
    """
    Fetch the YouTube RSS feed for a channel and return video IDs.
    Returns up to 15 most recent video IDs. No auth required.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        video_ids = []
        for entry in root.findall("atom:entry", RSS_NS):
            vid = entry.find("yt:videoId", RSS_NS)
            if vid is not None and vid.text:
                video_ids.append(vid.text.strip())
        return video_ids
    except Exception as e:
        return []


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

def _load_archive(sub_id: str) -> set[str]:
    """Return set of video IDs already downloaded for this subscription."""
    path = os.path.join(ARCHIVES_DIR, f"{sub_id}.txt")
    if not os.path.exists(path):
        return set()
    ids = set()
    with open(path) as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) == 2:
                ids.add(parts[1])  # format: "youtube <video_id>"
    return ids


def _append_archive(sub_id: str, video_id: str):
    """Mark a video as downloaded in the archive."""
    path = os.path.join(ARCHIVES_DIR, f"{sub_id}.txt")
    with open(path, "a") as f:
        f.write(f"youtube {video_id}\n")


# ---------------------------------------------------------------------------
# yt-dlp download
# ---------------------------------------------------------------------------

FORMAT_MAP = {
    "1080": "bestvideo[height<=1080][ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]/bestvideo[height<=1080][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[protocol!=m3u8][protocol!=m3u8_native]/best[height<=1080][protocol!=m3u8]/best",
    "720":  "bestvideo[height<=720][ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]/bestvideo[height<=720][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[protocol!=m3u8][protocol!=m3u8_native]/best[height<=720][protocol!=m3u8]/best",
    "480":  "bestvideo[height<=480][ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]/best[height<=480][protocol!=m3u8]/best",
    "best": "bestvideo[ext=mp4][protocol!=m3u8][protocol!=m3u8_native]+bestaudio[ext=m4a][protocol!=m3u8][protocol!=m3u8_native]/bestvideo[protocol!=m3u8][protocol!=m3u8_native]+bestaudio[protocol!=m3u8][protocol!=m3u8_native]/best",
}

OUTPUT_TEMPLATE = "%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s"


def _download_video(sub: dict, video_id: str, log_path: str) -> int:
    """Download a single video by ID."""
    os.makedirs(sub["output_dir"], exist_ok=True)
    archive_path = os.path.join(ARCHIVES_DIR, f"{sub['id']}.txt")
    output_tmpl  = os.path.join(sub["output_dir"], OUTPUT_TEMPLATE)
    fmt          = FORMAT_MAP.get(sub["quality"], FORMAT_MAP["1080"])
    video_url    = f"https://www.youtube.com/watch?v={video_id}"

    cmd = [
        "yt-dlp",
        "--download-archive",    archive_path,
        "--output",              output_tmpl,
        "--format",              fmt,
        "--merge-output-format", "mp4",
        "--retries",             "10",
        "--fragment-retries",    "10",
        "--concurrent-fragments","2",
        "--sleep-requests",      "2",
        "--sleep-interval",      "3",
        "--max-sleep-interval",  "8",
        "--js-runtimes",         "node",
        "--remote-components",   "ejs:github",
        "--newline",
    ]

    cookies_path = "/data/cookies.txt"
    if os.path.exists(cookies_path):
        cmd += ["--cookies", cookies_path]

    cmd.append(video_url)

    with open(log_path, "a") as log:
        result = subprocess.run(cmd, stdout=log, stderr=log, text=True)

    return result.returncode


def _get_playlist_new_ids(sub: dict, log_path: str) -> list[str]:
    """
    For playlists: use yt-dlp --flat-playlist to get video IDs quickly
    without downloading. Apply date_after filter if set.
    Returns only IDs not already in the archive.
    """
    archive = _load_archive(sub["id"])
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print", "id",
        "--quiet",
        "--no-warnings",
    ]
    if sub.get("date_after"):
        cmd += ["--dateafter", sub["date_after"]]
        cmd.append("--break-on-reject")
    cmd.append(sub["url"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        new_ids = [vid_id for vid_id in ids if vid_id not in archive]
        return new_ids
    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"ERROR fetching playlist IDs: {e}\n")
        return []


# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------

def _make_job_id() -> str:
    return str(uuid.uuid4())[:8]


def _job_start(job_id, sub_id, sub_name, trigger):
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":        job_id,
            "sub_id":        sub_id,
            "sub_name":      sub_name,
            "trigger":       trigger,
            "status":        "running",
            "videos_found":  0,
            "videos_done":   0,
            "videos_failed": 0,
            "started_at":    datetime.now(timezone.utc).isoformat(),
            "finished_at":   None,
            "exit_code":     None,
        }


def _job_finish(job_id, exit_code):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"]      = "completed" if exit_code == 0 else "failed"
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["exit_code"]   = exit_code


def _job_update(job_id, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def run_subscription(sub_id: str, trigger: str = "scheduler"):
    sub = get_sub(sub_id)
    if not sub or not sub["enabled"]:
        return

    # Prevent concurrent runs for the same subscription
    with _running_subs_lock:
        if sub_id in _running_subs:
            return  # already running, skip
        _running_subs.add(sub_id)

    try:
        job_id   = _make_job_id()
        log_path = os.path.join(LOGS_DIR, f"{sub_id}.log")
        _job_start(job_id, sub_id, sub["name"], trigger)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(log_path, "a") as log:
            log.write(f"\n{'='*60}\n{timestamp}  [{trigger.upper()}]\n{'='*60}\n")

        try:
            if _is_playlist(sub["url"]):
                new_ids = _get_playlist_new_ids(sub, log_path)
            else:
                channel_id = _resolve_channel_id(sub)
                if not channel_id:
                    with open(log_path, "a") as log:
                        log.write("ERROR: Could not resolve channel ID\n")
                    _job_finish(job_id, 1)
                    return

                rss_ids  = _fetch_rss_video_ids(channel_id)
                archive  = _load_archive(sub_id)
                new_ids  = [vid for vid in rss_ids if vid not in archive]

            _job_update(job_id, videos_found=len(new_ids))

            with open(log_path, "a") as log:
                log.write(f"Found {len(new_ids)} new video(s)\n")

            import time
            overall_rc = 0
            for i, video_id in enumerate(new_ids):
                if i > 0:
                    time.sleep(5)  # avoid rate limiting between downloads
                with open(log_path, "a") as log:
                    log.write(f"\n--- Downloading {video_id} ---\n")
                rc = _download_video(sub, video_id, log_path)
                if rc == 0:
                    _job_update(job_id,
                                videos_done=_jobs[job_id]["videos_done"] + 1)
                else:
                    overall_rc = rc
                    _job_update(job_id,
                                videos_failed=_jobs[job_id]["videos_failed"] + 1)

        except Exception as e:
            overall_rc = -1
            with open(log_path, "a") as log:
                log.write(f"\nException: {e}\n")

        _job_finish(job_id, overall_rc)

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE subscriptions SET initialized=1, last_checked=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), sub_id)
        )
        conn.commit()
        conn.close()

        with open(log_path, "a") as log:
            log.write(f"Run complete. Exit code: {overall_rc}\n")

    finally:
        with _running_subs_lock:
            _running_subs.discard(sub_id)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def schedule_sub(sub_id: str, interval_hours: float):
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        run_subscription, "interval", hours=interval_hours,
        id=job_id, args=[sub_id, "scheduler"], replace_existing=True,
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


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

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
           (id, url, name, output_dir, interval_hours, quality, backfill, date_after, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sub_id, body.url, body.name or body.url, body.output_dir,
         body.interval_hours, body.quality, int(body.backfill),
         body.date_after, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    schedule_sub(sub_id, body.interval_hours)
    threading.Thread(target=run_subscription, args=(sub_id, "initial"), daemon=True).start()
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
    conn.execute(f"UPDATE subscriptions SET {set_clause} WHERE id=?",
                 list(updates.values()) + [sub_id])
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
def trigger_check(sub_id: str):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    threading.Thread(target=run_subscription, args=(sub_id, "manual"), daemon=True).start()
    return {"message": "Check triggered in background", "id": sub_id}


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
def list_jobs(
    status: Optional[str] = Query(default=None,
        description="Filter: running | completed | failed")
):
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
