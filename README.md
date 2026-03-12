# yt-dlp Manager

A lightweight self-hosted API for monitoring YouTube channels/playlists and
automatically downloading new videos via yt-dlp.

## Stack

- **FastAPI** — REST API for managing subscriptions and monitoring jobs
- **APScheduler** — per-subscription scheduling with individual intervals and jitter
- **SQLite** — persistent subscription storage
- **yt-dlp** — the actual downloader (built into the container)
- **ffmpeg** — merges separate video/audio streams into mp4
- **Node.js** — JavaScript runtime required by yt-dlp for YouTube extraction
- **bgutil-ytdlp-pot-provider** — generates PO tokens to avoid YouTube bot detection

---

## How It Works

### Channels (RSS-based discovery)
Channel subscriptions use YouTube's public RSS feed to discover new videos:
```
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
```
This returns the 15 most recent videos instantly with no authentication required.
Only the actual video downloads use yt-dlp. This eliminates the full-channel-scan
problem that caused cookie rotation and bot detection issues.

### Playlists
Playlist subscriptions use `yt-dlp --flat-playlist` to fetch video IDs only (no
video data), then download new IDs individually.

### Downloads
Both modes download each new video individually via a separate yt-dlp call, with a
5-second pause between videos to avoid rate limiting. Videos already in the
download archive are skipped automatically.

### Bot Detection Avoidance
- Uses the `android_vr` YouTube player client, which bypasses most bot detection
- bgutil PO token provider generates GVS PO tokens when needed
- Scheduling jitter spreads subscription checks randomly across each interval
  window so they don't all fire at once
- Shorts and live streams are filtered out (`duration>180`, `/shorts/` URL exclusion)

---

## Project Structure

```
ytdl-manager/
├── docker-compose.yml
├── Dockerfile
├── README.md
├── start.sh                # Container entrypoint — checks bgutil then starts uvicorn
├── app/
│   ├── main.py
│   └── requirements.txt
└── scripts/
    └── ytdl.sh             # Interactive management menu (run on Ubuntu host)
```

---

## Setup

### 1. Edit docker-compose.yml

The current configuration:

```yaml
services:
  ytdl-manager:
    build: .
    container_name: ytdl-manager
    restart: unless-stopped
    ports:
      - "8911:8080"
    volumes:
      - ytdl-data:/data
      - /mnt/nas/video/ytdl-manager:/downloads
    environment:
      - TZ=America/Los_Angeles
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  ytdl-data:
```

**To customize:**
- Change `/mnt/nas/video/ytdl-manager` to wherever you want videos stored on the host
- Change `TZ=America/Los_Angeles` to your timezone
- Change the left side of `8911:8080` if you need a different port

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

## Management Script

The easiest way to manage subscriptions is the interactive menu script:

```bash
~/ai/ytdl-manager/scripts/ytdl.sh
```

### Menu Options

| # | Option | Description |
|---|--------|-------------|
| 1 | Add subscription | Add a channel or playlist, auto-triggers initial run |
| 2 | List subscriptions | Shows all subs with quality, interval, last checked |
| 3 | Trigger manual check | Immediately check a subscription for new videos |
| 4 | View logs | Show last N lines of a sub's log, or `s` to follow live |
| 5 | Pause/Resume | Toggle a subscription on or off |
| 6 | Update settings | Change interval, quality, or date_after |
| 7 | Delete subscription | Removes sub and cancels any running job |
| 8 | Job status | Shows recent jobs with found/done/failed counts |
| 9 | Recent downloads | Lists recently downloaded files |
| 10 | Health check | Pings the API |
| 11 | Refresh cookies | Copies latest cookies.txt from ~/Downloads into container |
| 12 | Container operations | Status, logs, restart, update yt-dlp |

---

## YouTube Cookies

The `android_vr` player client used for downloads generally does not require
cookies. However, keeping a cookies file available provides a fallback for any
web client requests.

### Refreshing cookies (if needed)

1. Open **Chrome** on the Ubuntu machine and log into YouTube
2. Install the **"Get cookies.txt LOCALLY"** Chrome extension
3. Go to **youtube.com**, click the extension icon → **Export**
   (saves `cookies.txt` to `~/Downloads`)
4. Run the management script and select option **11**
   - The script automatically picks the newest `cookies*.txt` file
   - Copies it into the container
   - Deletes all local cookie files from Downloads afterward

**Note:** Do not log out of YouTube in Chrome after exporting —
logging out immediately invalidates the cookies.

---

## File Naming

Downloaded videos are saved as:
```
Video Title_(2025_03_07)_[dQw4w9WgXcQ].mp4
```
Template: `%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s`

---

## Quality Options

| Setting | Description |
|---------|-------------|
| `best` | No height cap — downloads highest available (4K if offered) |
| `1080` | Caps at 1080p (default) |
| `720` | Caps at 720p — smaller files |
| `480` | Caps at 480p — smallest files |

`best` is recommended for channels that post in 4K. File sizes can be 3–5x
larger than 1080p for 4K content.

---

## Scheduling

Each subscription fires on its own interval (e.g. every 6 hours). On container
startup, a random jitter is applied so subscriptions spread out across the
interval window instead of all firing at the same time. This reduces the chance
of YouTube seeing a burst of requests from the same IP.

---

## Shorts & Live Stream Filtering

Videos are automatically skipped if:
- Duration is 3 minutes or less (`duration>180`)
- URL contains `/shorts/`
- Video is live or was live (`!is_live & !was_live`)

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
    "quality":        "best"
  }'

# Monitor a playlist (date_after recommended to avoid large initial scan)
curl -X POST http://192.168.0.166:8911/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/playlist?list=PLxxxxxx",
    "name":           "my-playlist",
    "output_dir":     "/downloads/my-playlist",
    "interval_hours": 6,
    "quality":        "best",
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
| `quality` | `1080` | `best`, `1080`, `720`, or `480` |
| `backfill` | `false` | `true` = download full history on first run (playlists only) |
| `date_after` | none | Only download videos on/after this date (playlists only) |

**`date_after` formats:** `today-7days` · `today-30days` · `today-6months` · `today-1year` · `20250101`

**Note:** Adding a duplicate URL returns HTTP 409 with the existing subscription's ID and name.

---

### List all subscriptions

```bash
curl http://192.168.0.166:8911/subscriptions | jq
```

### Get one subscription

```bash
curl http://192.168.0.166:8911/subscriptions/<id> | jq
```

### Find a subscription ID by name

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
  -d '{"quality": "best"}'
```

---

### Delete a subscription

```bash
curl -X DELETE http://192.168.0.166:8911/subscriptions/<id>
```

Deleting a subscription also cancels any currently running job for that subscription.

---

### Job status

```bash
# All recent jobs
curl http://192.168.0.166:8911/jobs | jq

# Only running jobs
curl "http://192.168.0.166:8911/jobs?status=running" | jq

# Only failed jobs
curl "http://192.168.0.166:8911/jobs?status=failed" | jq
```

Job records include `videos_found`, `videos_done`, and `videos_failed` counts.
Job history is in-memory only and resets on container restart.

---

### Recent downloads log

```bash
curl "http://192.168.0.166:8911/downloads-log?lines=50" | jq
```

Returns a list of successfully downloaded files with timestamp, subscription name,
and filename.

---

## Swagger UI

FastAPI auto-generates interactive API docs at:
```
http://192.168.0.166:8911/docs
```

---

## Rebuilding the Container

Required when `main.py`, `Dockerfile`, or `requirements.txt` change.
Script-only changes (`ytdl.sh`) just need a `git pull` on the Ubuntu host.

```bash
cd ~/ai/ytdl-manager
docker compose down && docker compose build --no-cache && docker compose up -d
```

---

## Development Workflow

Files are edited via SSH on the Ubuntu machine or via the SMB share.
After editing, commit and push from the Ubuntu machine:

```bash
cd ~/ai/ytdl-manager
git add . && git commit -m "describe change" && git push
```

Then either:
- **Script changes only:** `git pull` on Ubuntu — no rebuild needed
- **App/Docker changes:** Full rebuild (see above) or Portainer → **Pull and redeploy**
