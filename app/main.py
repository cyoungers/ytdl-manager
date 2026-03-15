import io
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PIL import Image

_TZ = ZoneInfo("America/Los_Angeles")
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="yt-dlp Manager")
scheduler = BackgroundScheduler()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_YTDLP_VERSION = subprocess.run(
    ["yt-dlp", "--version"], capture_output=True, text=True
).stdout.strip()

DB_PATH = "/data/subscriptions.db"
ARCHIVES_DIR = "/data/archives"
LOGS_DIR = "/data/logs"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_running_subs: set[str] = set()
_running_subs_lock = threading.Lock()
_cancelled_subs: set[str] = set()
_cancelled_subs_lock = threading.Lock()

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
    rows = conn.execute("SELECT * FROM subscriptions ORDER BY name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Channel avatar helpers
# ---------------------------------------------------------------------------

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


def _extract_avatar_url(html: str) -> Optional[str]:
    """
    Extract the highest-resolution channel avatar URL from YouTube page HTML.
    YouTube embeds page data as JSON in <script> tags; the channel avatar is
    under c4TabbedHeaderRenderer -> avatar -> thumbnails.
    """
    # Try to find avatar thumbnails in the c4TabbedHeaderRenderer JSON block
    m = re.search(
        r'"c4TabbedHeaderRenderer".*?"avatar":\{"thumbnails":(\[[^\]]+\])',
        html, re.DOTALL
    )
    if m:
        try:
            thumbnails = json.loads(m.group(1))
            best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
            url = best.get("url", "")
            if url:
                return url
        except Exception:
            pass

    # Fallback: look for any "avatar" block with thumbnails
    m = re.search(r'"avatar":\{"thumbnails":(\[[^\]]+\])', html)
    if m:
        try:
            thumbnails = json.loads(m.group(1))
            best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
            url = best.get("url", "")
            if url:
                return url
        except Exception:
            pass

    # Last resort: first yt3.ggpht.com URL (channel avatars, not video thumbnails)
    m = re.search(r'"url":"(https://yt3\.ggpht\.com/[^"]+)"', html)
    if m:
        return m.group(1)

    return None


def _fetch_channel_avatar(channel_url: str, assets_dirs: list[str]):
    """
    Fetch the channel homepage, extract the avatar image, resize it to 4:3
    (pillarboxing with black bars if the source is square/portrait), and save
    it as avatar.jpg in each directory in assets_dirs.
    """
    for d in assets_dirs:
        os.makedirs(d, exist_ok=True)
    try:
        req = urllib.request.Request(channel_url, headers=_SCRAPE_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        avatar_url = _extract_avatar_url(html)
        if not avatar_url:
            return

        # Request a high-resolution variant by bumping the size parameter
        # YouTube avatar URLs often end with =s<N>-... ; replace with =s512
        avatar_url = re.sub(r'=s\d+', '=s512', avatar_url)

        img_req = urllib.request.Request(avatar_url, headers=_SCRAPE_HEADERS)
        with urllib.request.urlopen(img_req, timeout=15) as resp:
            img_data = resp.read()

        img = Image.open(io.BytesIO(img_data)).convert("RGB")
        w, h = img.size
        target_ratio = 4 / 3
        current_ratio = w / h

        if current_ratio < target_ratio:
            # Narrower than 4:3 — add black bars on left and right
            new_w = int(h * target_ratio)
            canvas = Image.new("RGB", (new_w, h), (0, 0, 0))
            canvas.paste(img, ((new_w - w) // 2, 0))
        elif current_ratio > target_ratio:
            # Wider than 4:3 — add black bars on top and bottom
            new_h = int(w / target_ratio)
            canvas = Image.new("RGB", (w, new_h), (0, 0, 0))
            canvas.paste(img, (0, (new_h - h) // 2))
        else:
            canvas = img

        for d in assets_dirs:
            canvas.save(os.path.join(d, "avatar.jpg"), "JPEG", quality=90)

    except Exception:
        pass  # Avatar fetch is best-effort; don't fail subscription creation


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
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1440": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
}

OUTPUT_TEMPLATE = "%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s"


DOWNLOADS_LOG = "/data/downloads.log"


def _log_download(sub: dict, filename: str):
    """Append a successful download entry to the downloads log."""
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    basename = os.path.basename(filename)
    with open(DOWNLOADS_LOG, "a") as f:
        f.write(f"{ts}\t{sub['name']}\t{basename}\n")


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
        "--match-filter",        "duration>180 & !is_live & !was_live & original_url!*=/shorts/",
        "--merge-output-format", "mp4",
        "--retries",             "10",
        "--fragment-retries",    "10",
        "--concurrent-fragments","2",
        "--sleep-requests",      "2",
        "--sleep-interval",      "3",
        "--max-sleep-interval",  "8",
        "--extractor-args",      "youtube:player_client=android_vr",
        "--js-runtimes",         "node",
        "--remote-components",   "ejs:github",
        "--newline",
    ]

    cookies_path = "/data/cookies.txt"
    if os.path.exists(cookies_path):
        cmd += ["--cookies", cookies_path]

    cmd.append(video_url)

    result = subprocess.run(cmd, capture_output=False,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Write output to log
    with open(log_path, "a") as log:
        log.write(result.stdout)
        if not result.stdout.endswith("\n"):
            log.write("\n")

    # Treat these non-fatal outcomes as success (not real failures)
    non_fatal_patterns = [
        "does not pass filter",           # --match-filter skipped (Shorts, live, <3min)
        "This live event will begin",     # Scheduled stream not yet started
        "Premieres in",                   # YouTube premiere not yet started
        "Sign in to confirm your age",    # Age-gated content, skip gracefully
    ]
    if result.returncode != 0 and any(p in result.stdout for p in non_fatal_patterns):
        return 0

    # Detect successful download — yt-dlp prints the final merged filename
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            # "Merging formats into "/path/to/file.mp4""
            if line.startswith("[Merger] Merging formats into"):
                filename = line.split('"')[1] if '"' in line else ""
                if filename:
                    _log_download(sub, filename)
                    break
            # Single-stream (no merge needed): "[download] Destination: /path/to/file.mp4"
            elif line.startswith("[download] Destination:") and line.endswith(".mp4"):
                filename = line[len("[download] Destination:"):].strip()
                if filename:
                    _log_download(sub, filename)
                    break
            # Already downloaded: "[download] /path/to/file.mp4 has already been downloaded"
            elif "has already been downloaded" in line:
                filename = line[len("[download]"):].strip()
                filename = filename.replace(" has already been downloaded", "")
                if filename:
                    _log_download(sub, filename)
                    break

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


def _get_channel_new_ids(sub: dict, log_path: str) -> list[str]:
    """
    For channels: use yt-dlp --flat-playlist to get the 15 most recent
    video IDs. More reliable than the YouTube RSS feed, which can fall
    behind by days or stop updating entirely for some channels.
    Returns only IDs not already in the archive.
    """
    archive = _load_archive(sub["id"])
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--playlist-end", "15",
        "--print", "id",
        "--quiet",
        "--no-warnings",
        sub["url"],
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        new_ids = [vid_id for vid_id in ids if vid_id not in archive]
        return new_ids
    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"ERROR fetching channel IDs: {e}\n")
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

        timestamp = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        with open(log_path, "a") as log:
            log.write(f"\n{'='*60}\n{timestamp}  [{trigger.upper()}]\n{'='*60}\n")

        try:
            if _is_playlist(sub["url"]):
                new_ids = _get_playlist_new_ids(sub, log_path)
            else:
                new_ids = _get_channel_new_ids(sub, log_path)

            _job_update(job_id, videos_found=len(new_ids))

            with open(log_path, "a") as log:
                log.write(f"Found {len(new_ids)} new video(s)\n")

            import time
            overall_rc = 0
            for i, video_id in enumerate(new_ids):
                with _cancelled_subs_lock:
                    if sub_id in _cancelled_subs:
                        with open(log_path, "a") as log:
                            log.write("Job cancelled (subscription deleted)\n")
                        _job_finish(job_id, -2)
                        return
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
        with _cancelled_subs_lock:
            _cancelled_subs.discard(sub_id)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def schedule_sub(sub_id: str, interval_hours: float, jitter: bool = False):
    import random
    from datetime import timedelta
    job_id = f"sub_{sub_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    # Spread out subscriptions by delaying the first run by a random offset
    # up to the full interval, so they don't all fire at the same time
    kwargs = dict(
        hours=interval_hours,
        id=job_id,
        args=[sub_id, "scheduler"],
        replace_existing=True,
    )
    if jitter:
        jitter_seconds = random.randint(0, int(interval_hours * 3600))
        kwargs["start_date"] = datetime.now(timezone.utc) + timedelta(seconds=jitter_seconds)
    scheduler.add_job(run_subscription, "interval", **kwargs)


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
            schedule_sub(sub["id"], sub["interval_hours"], jitter=True)


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
    quality:        str            = "1440"
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
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT id, name FROM subscriptions WHERE url = ?", (body.url,)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail=f"Already subscribed (id={existing[0]}, name={existing[1]})")
    sub_id = str(uuid.uuid4())[:8]
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

    assets_dir = os.path.join(body.output_dir, "assets")
    channel_name = os.path.basename(body.output_dir.rstrip("/"))
    root_dir = os.path.dirname(body.output_dir.rstrip("/"))
    dvr_assets_dir = os.path.join(root_dir, "channelsDVRassets", channel_name, "assets")
    for d in (assets_dir, dvr_assets_dir):
        os.makedirs(d, exist_ok=True)
    threading.Thread(
        target=_fetch_channel_avatar, args=(body.url, [assets_dir, dvr_assets_dir]), daemon=True
    ).start()

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
    # Signal any running job to stop
    with _cancelled_subs_lock:
        _cancelled_subs.add(sub_id)
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


@app.get("/downloads-log")
def get_downloads_log(lines: int = 50):
    if not os.path.exists(DOWNLOADS_LOG):
        return {"entries": []}
    with open(DOWNLOADS_LOG) as f:
        raw = f.readlines()
    entries = []
    for line in raw[-lines:]:
        parts = line.strip().split("\t")
        if len(parts) == 3:
            entries.append({"timestamp": parts[0], "subscription": parts[1], "filename": parts[2]})
    entries.reverse()  # newest first
    return {"entries": entries}


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


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

def _sub_status(sub_id: str, enabled: bool) -> tuple[str, Optional[str]]:
    """Return (status_string, last_error_or_None) for a subscription."""
    with _running_subs_lock:
        if sub_id in _running_subs:
            return "running", None
    if not enabled:
        return "idle", None
    with _jobs_lock:
        sub_jobs = [j for j in _jobs.values() if j["sub_id"] == sub_id]
    if not sub_jobs:
        return "ok", None
    last_job = max(sub_jobs, key=lambda j: j["started_at"])
    if last_job["status"] == "failed":
        return "error", f"Exit code {last_job['exit_code']}"
    return "ok", None


def _count_downloads_today() -> int:
    today = datetime.now(_TZ).strftime("%Y-%m-%d")
    if not os.path.exists(DOWNLOADS_LOG):
        return 0
    count = 0
    with open(DOWNLOADS_LOG) as f:
        for line in f:
            if line.startswith(today):
                count += 1
    return count


def _downloads_today_by_name() -> dict:
    today = datetime.now(_TZ).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    if not os.path.exists(DOWNLOADS_LOG):
        return counts
    with open(DOWNLOADS_LOG) as f:
        for line in f:
            if not line.startswith(today):
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                counts[parts[1]] = counts.get(parts[1], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

@app.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/status")
def api_status():
    subs = all_subs()
    today = datetime.now(_TZ).strftime("%Y-%m-%d")

    last_checked_id = None
    last_checked_name = None
    last_checked_at = None
    for s in subs:
        if s["last_checked"]:
            if last_checked_at is None or s["last_checked"] > last_checked_at:
                last_checked_at = s["last_checked"]
                last_checked_id = s["id"]
                last_checked_name = s["name"]

    errors_today = 0
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "failed" and job.get("started_at", "").startswith(today):
                errors_today += 1

    return {
        "subscription_count": len(subs),
        "downloads_today": _count_downloads_today(),
        "last_checked_id": last_checked_id,
        "last_checked_name": last_checked_name,
        "last_checked_at": last_checked_at,
        "errors_today": errors_today,
        "ytdlp_version": _YTDLP_VERSION,
    }


@app.get("/api/subscriptions")
def api_subscriptions():
    subs = all_subs()
    dl_by_name = _downloads_today_by_name()
    result = []
    for s in subs:
        status, last_error = _sub_status(s["id"], bool(s["enabled"]))
        result.append({
            "id": s["id"],
            "name": s["name"],
            "url": s["url"],
            "interval_hours": s["interval_hours"],
            "quality": s["quality"],
            "enabled": bool(s["enabled"]),
            "status": status,
            "last_checked_at": s["last_checked"],
            "last_error": last_error,
            "downloads_today": dl_by_name.get(s["name"], 0),
        })
    return result


@app.get("/api/downloads")
def api_downloads(limit: int = 200):
    if not os.path.exists(DOWNLOADS_LOG):
        return []
    with open(DOWNLOADS_LOG) as f:
        lines = f.readlines()
    entries = []
    for line in reversed(lines[-limit:]):
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        ts_str, channel_name, filename = parts
        basename = os.path.basename(filename)
        vid_match = re.search(r'\[([a-zA-Z0-9_-]+)\]\.[^.]+$', basename)
        video_id = vid_match.group(1) if vid_match else ""
        title_match = re.match(r'^(.+)_\(\d{4}_\d{2}_\d{2}\)_\[', basename)
        title = title_match.group(1) if title_match else basename
        entries.append({
            "video_id": video_id,
            "title": title,
            "channel_id": channel_name,
            "downloaded_at": ts_str,
        })
    return entries


@app.get("/api/log/{sub_id}")
def api_log(sub_id: str, lines: int = 500):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    log_path = os.path.join(LOGS_DIR, f"{sub_id}.log")
    if not os.path.exists(log_path):
        return []
    with open(log_path) as f:
        content = f.readlines()
    return [line.rstrip("\n") for line in content[-lines:]]


@app.post("/api/check/{sub_id}")
def api_check(sub_id: str):
    if not get_sub(sub_id):
        raise HTTPException(404, "Subscription not found")
    threading.Thread(target=run_subscription, args=(sub_id, "manual"), daemon=True).start()
    return {"status": "triggered"}


@app.post("/api/check-all")
def api_check_all():
    subs = all_subs()
    triggered = []
    for s in subs:
        if s["enabled"]:
            threading.Thread(target=run_subscription, args=(s["id"], "manual"), daemon=True).start()
            triggered.append(s["id"])
    return {"status": "triggered", "count": len(triggered)}


@app.post("/api/ytdlp-update")
def api_ytdlp_update():
    global _YTDLP_VERSION
    result = subprocess.run(
        ["pip", "install", "--upgrade", "yt-dlp", "--quiet"],
        capture_output=True, text=True
    )
    _YTDLP_VERSION = subprocess.run(
        ["yt-dlp", "--version"], capture_output=True, text=True
    ).stdout.strip()
    return {"version": _YTDLP_VERSION, "output": (result.stdout + result.stderr).strip()}
