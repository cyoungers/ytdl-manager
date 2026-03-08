# yt-dlp Manager

A lightweight self-hosted API for monitoring YouTube channels/playlists and
automatically downloading new videos via yt-dlp.

## Stack

- **FastAPI** — REST API for managing subscriptions and monitoring jobs
- **APScheduler** — per-subscription scheduling with individual intervals
- **SQLite** — persistent subscription storage
- **yt-dlp** — the actual downloader (built into the container)
- **ffmpeg** — merges separate video/audio streams into mp4
- **Node.js** — JavaScript runtime required by yt-dlp for YouTube extraction

---

## Project Structure

```
ytdl-manager/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── refresh-cookies.sh      # Run this on the Ubuntu host to refresh YouTube cookies
└── app/
    ├── main.py
    └── requirements.txt
```

---

## Setup

### 1. Edit docker-compose.yml

Change the downloads bind mount to your actual path on the host:

```yaml
volumes:
  - ytdl-data:/data
  - /your/actual/path:/downloads   # ← change this
```

Change the timezone:
```yaml
environment:
  - TZ=America/Chicago   # ← change to your timezone
```

The default port is **8911**. Change the left side if needed:
```yaml
ports:
  - "8911:8080"
```

### 2. Deploy via Portainer

In Portainer, create a new stack using **Repository** type:
- Repository URL: `https://github.com/cyoungers/ytdl-manager`
- Reference: `refs/heads/main`
- Compose path: `docker-compose.yml`

### 3. Confirm it's running

```bash
curl http://192.168.0.166:8911/health
```

---

## YouTube Cookies (Required)

YouTube rate-limits and blocks anonymous requests. You must provide valid
YouTube session cookies for reliable downloads.

### Initial setup

1. Open **Chrome on the Ubuntu machine** and log into YouTube
2. Install the **"Get cookies.txt LOCALLY"** Chrome extension:
   https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
3. Go to **youtube.com**, click the extension icon → **Export**
   (saves `cookies.txt` to `~/Downloads`)
4. Run the refresh script:
   ```bash
   ~/refresh-cookies.sh
   ```

### Refreshing cookies

Cookies typically last 1–2 weeks. When downloads start failing with
"sign in to confirm you're not a bot" in the logs, refresh them:

1. Go to youtube.com in Chrome (make sure you're logged in)
2. Click the extension → Export
3. Run `~/refresh-cookies.sh`

**Important:** Do not log out of YouTube in Chrome after exporting —
logging out invalidates the cookies immediately.

---

## File Naming

Downloaded videos are saved as:
```
Video Title_(2025_03_07)_[dQw4w9WgXcQ].mp4
```
Template: `%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s`

---

## curl API Reference

All examples use `http://192.168.0.166:8911`.

---

### Health check

```bash
curl http://192.168.0.166:8911/health
```

---

### Add a subscription

```bash
# Monitor a channel — only new videos from now on (recommended)
curl -X POST http://192.168.0.166:8911/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/channel/UC0YvoAYGgdOfySQSLcxtu1w",
    "name":           "belleranch",
    "output_dir":     "/downloads/belleranch",
    "interval_hours": 6,
    "quality":        "1080",
    "backfill":       false,
    "date_after":     "today-7days"
  }'
```

**Fields:**

| Field | Default | Description |
|---|---|---|
| `url` | required | YouTube channel or playlist URL |
| `name` | url | Friendly display name |
| `output_dir` | required | Path inside the container (e.g. `/downloads/channelname`) |
| `interval_hours` | `6` | How often to check for new videos (per subscription) |
| `quality` | `1080` | `1080`, `720`, `480`, or `best` |
| `backfill` | `false` | `true` = download full history on first run |
| `date_after` | none | Only download videos on/after this date (recommended — see note below) |

**`date_after` note:** Always set this when adding a new subscription.
Without it, the first run will attempt a full archive scan of the entire
channel history which takes hours and causes YouTube to rotate cookies.
With `date_after` set, yt-dlp uses `--break-on-reject` to stop scanning
as soon as it hits a video older than the cutoff — typically just a few
pages instead of thousands.

**`quality` options:** `1080` (default) · `720` · `480` · `best`

---

### List all subscriptions

```bash
curl http://192.168.0.166:8911/subscriptions | jq
```

### Get one subscription

```bash
curl http://192.168.0.166:8911/subscriptions/<id> | jq
```

---

### Trigger a manual check/download

```bash
# Check for new videos now (uses subscription's date_after if set)
curl -X POST http://192.168.0.166:8911/subscriptions/<id>/check

# One-off with a specific date range
curl -X POST "http://192.168.0.166:8911/subscriptions/<id>/check?date_after=today-7days"
curl -X POST "http://192.168.0.166:8911/subscriptions/<id>/check?date_after=20250101"
```

---

### View the log for a subscription

```bash
# Last 100 lines (default)
curl -s "http://192.168.0.166:8911/subscriptions/<id>/log" | jq -r .log

# Last 30 lines
curl -s "http://192.168.0.166:8911/subscriptions/<id>/log?lines=30" | jq -r .log
```

---

### Update a subscription

```bash
# Change check interval
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"interval_hours": 12}'

# Change date_after
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"date_after": "today-30days"}'

# Pause a subscription
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Resume
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Change quality
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"quality": "720"}'
```

---

### Delete a subscription

```bash
curl -X DELETE http://192.168.0.166:8911/subscriptions/<id>
```

---

### Job status

```bash
# All jobs (running + history) plus next scheduled run per subscription
curl http://192.168.0.166:8911/jobs | jq

# Only running jobs
curl "http://192.168.0.166:8911/jobs?status=running" | jq

# Only failed jobs
curl "http://192.168.0.166:8911/jobs?status=failed" | jq

# Specific job
curl http://192.168.0.166:8911/jobs/<job_id> | jq
```

**Note:** Job history is in-memory only and resets on container restart or redeploy.

---

## Swagger UI

FastAPI auto-generates interactive API docs at:
```
http://192.168.0.166:8911/docs
```

---

## Updating yt-dlp

YouTube changes its internals frequently. Rebuild the container periodically
to pick up the latest yt-dlp release:

```bash
# On the Ubuntu machine
cd ~/ai/ytdl-manager
docker compose build --no-cache && docker compose up -d
```

Or in Portainer: redeploy the stack with **Re-pull image** checked.

---

## Development Workflow

Files are edited on the Mac via the SMB mount at `/Volumes/Shared-1/ai/ytdl-manager/`
(Claude can write directly here). Changes are pushed to GitHub from the Ubuntu
machine using VS Code's integrated terminal:

```bash
cd ~/ai/ytdl-manager
git add . && git commit -m "describe change" && git push
```

Then in Portainer → stack → **Pull and redeploy**.
