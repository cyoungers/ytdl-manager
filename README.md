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

## How It Works

### Channels (RSS-based discovery)
Channel subscriptions use YouTube's public RSS feed to discover new videos:
```
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
```
This returns the 15 most recent videos instantly, with no authentication required.
Only the actual video downloads need cookies. This eliminates the full-channel-scan
problem that caused cookie rotation and bot detection.

### Playlists
Playlist subscriptions use `yt-dlp --flat-playlist` to fetch video IDs only (no
video data), then download new IDs individually.

### Downloads
Both modes download each new video individually via a separate yt-dlp call, with a
5-second pause between videos to avoid rate limiting. Videos already in the
download archive are skipped automatically.

---

## Project Structure

```
ytdl-manager/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── refresh-cookies.sh      # Run on the Ubuntu host to refresh YouTube cookies
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

## YouTube Cookies (Required for Downloads)

RSS-based channel discovery needs no authentication. However, the actual video
downloads require valid YouTube session cookies to avoid bot detection.

### Initial setup

1. Open **Chrome** and log into YouTube
2. Install the **"Get cookies.txt LOCALLY"** extension:
   https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
3. Go to **youtube.com**, click the extension icon → **Export**
   (saves `cookies.txt` to `~/Downloads`)
4. Find your container name and copy the cookies in:
   ```bash
   CONTAINER=$(docker ps --filter name=ytdl --format "{{.Names}}" | head -1)
   docker cp ~/Downloads/cookies.txt $CONTAINER:/data/cookies.txt
   ```
   Or use the included helper script: `~/refresh-cookies.sh`
   (update the container name inside the script if needed)

### Refreshing cookies

When downloads start failing with "sign in to confirm you're not a bot" in the
logs, refresh them:

1. Go to youtube.com in Chrome (make sure you're logged in)
2. Click the extension → Export
3. Run the copy command above or `~/refresh-cookies.sh`

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
# Monitor a channel (RSS-based — no date_after needed)
curl -X POST http://192.168.0.166:8911/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/channel/UC0YvoAYGgdOfySQSLcxtu1w",
    "name":           "belleranch",
    "output_dir":     "/downloads/belleranch",
    "interval_hours": 6,
    "quality":        "1080"
  }'

# Monitor a playlist (date_after recommended to avoid large initial scan)
curl -X POST http://192.168.0.166:8911/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/playlist?list=PLxxxxxx",
    "name":           "my-playlist",
    "output_dir":     "/downloads/my-playlist",
    "interval_hours": 6,
    "quality":        "1080",
    "date_after":     "today-7days"
  }'
```

**Fields:**

| Field | Default | Description |
|---|---|---|
| `url` | required | YouTube channel or playlist URL |
| `name` | url | Friendly display name |
| `output_dir` | required | Path inside the container (e.g. `/downloads/channelname`) |
| `interval_hours` | `6` | How often to check for new videos |
| `quality` | `1080` | `1080`, `720`, `480`, or `best` |
| `backfill` | `false` | `true` = download full history on first run (playlists only) |
| `date_after` | none | Only download videos on/after this date (playlists only — channels use RSS) |

**`date_after` formats:** `today-7days` · `today-30days` · `20250101`

**Channel subscriptions** do not need `date_after` — RSS only returns the 15
most recent videos, so there is no risk of scanning thousands of old videos.

**Playlist subscriptions** should use `date_after` to limit the initial scan.

---

### List all subscriptions

```bash
curl http://192.168.0.166:8911/subscriptions | jq
```

### Get one subscription

```bash
curl http://192.168.0.166:8911/subscriptions/<id> | jq
```

### One-liner to find a subscription ID by name

```bash
curl -s http://192.168.0.166:8911/subscriptions | jq -r '.[] | select(.name=="belleranch") | .id'
```

---

### Trigger a manual check

```bash
curl -X POST http://192.168.0.166:8911/subscriptions/<id>/check
```

Only one job runs per subscription at a time — duplicate triggers are ignored
while a run is already in progress.

---

### View logs

```bash
# Last 100 lines (default)
curl -s "http://192.168.0.166:8911/subscriptions/<id>/log" | jq -r .log

# Last 50 lines
curl -s "http://192.168.0.166:8911/subscriptions/<id>/log?lines=50" | jq -r .log

# Shortcut using name
ID=$(curl -s http://192.168.0.166:8911/subscriptions | jq -r '.[] | select(.name=="belleranch") | .id') \
  && curl -s "http://192.168.0.166:8911/subscriptions/$ID/log?lines=50" | jq -r .log
```

---

### Update a subscription

```bash
# Change check interval
curl -X PATCH http://192.168.0.166:8911/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"interval_hours": 12}'

# Pause
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
# All jobs + next scheduled run per subscription
curl http://192.168.0.166:8911/jobs | jq

# Only running jobs
curl "http://192.168.0.166:8911/jobs?status=running" | jq

# Only failed jobs
curl "http://192.168.0.166:8911/jobs?status=failed" | jq

# Specific job
curl http://192.168.0.166:8911/jobs/<job_id> | jq
```

Job records include `videos_found`, `videos_done`, and `videos_failed` counts.
Job history is in-memory only and resets on container restart.

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

Files are edited on the Mac via the SMB mount at `/Volumes/Shared-1/ai/ytdl-manager/`.
Changes are pushed to GitHub from the Ubuntu machine:

```bash
cd ~/ai/ytdl-manager
git add . && git commit -m "describe change" && git push
```

Then in Portainer → stack → **Pull and redeploy**.
