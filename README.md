# yt-dlp Manager

A lightweight self-hosted API for monitoring YouTube channels/playlists and
automatically downloading new videos via yt-dlp.

## Setup

1. Edit `docker-compose.yml`:
   - Change `/mnt/media/youtube` to your actual downloads path on the host
   - Change `TZ` to your timezone

2. Deploy (Portainer or CLI):
   ```bash
   docker compose up -d --build
   ```

3. Confirm it's running:
   ```bash
   curl http://<your-server-ip>:8080/health
   ```

---

## File naming

Downloaded videos are saved as:
```
Video Title_(2025_03_07)_[dQw4w9WgXcQ].mp4
```
Template: `%(title)s_(%(upload_date>%Y_%m_%d)s)_[%(id)s].%(ext)s`

---

## curl API Reference

Replace `SERVER` with your Ubuntu machine's IP.

---

### Add a subscription

```bash
# Monitor a channel — only new videos from now on (recommended)
curl -s -X POST http://SERVER:8080/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/@SomeChannel",
    "name":           "Some Channel",
    "output_dir":     "/downloads/SomeChannel",
    "interval_hours": 6,
    "quality":        "1080",
    "backfill":       false
  }' | jq

# Monitor a playlist, download full backlog on first run
curl -s -X POST http://SERVER:8080/subscriptions \
  -H "Content-Type: application/json" \
  -d '{
    "url":            "https://www.youtube.com/playlist?list=PLxxxxxx",
    "name":           "My Playlist",
    "output_dir":     "/downloads/MyPlaylist",
    "interval_hours": 12,
    "quality":        "1080",
    "backfill":       true
  }' | jq
```

**quality options:** `1080` (default) · `720` · `480` · `best`

**backfill:**
- `false` — marks all existing videos as already downloaded, then only fetches new ones going forward
- `true`  — downloads everything in the channel/playlist on first run

---

### List all subscriptions

```bash
curl -s http://SERVER:8080/subscriptions | jq
```

### Get one subscription

```bash
curl -s http://SERVER:8080/subscriptions/<id> | jq
```

---

### Trigger a manual check/download now

```bash
curl -s -X POST http://SERVER:8080/subscriptions/<id>/check | jq
```

---

### View the log for a subscription

```bash
# Last 100 lines (default)
curl -s "http://SERVER:8080/subscriptions/<id>/log" | jq -r .log

# Last 50 lines
curl -s "http://SERVER:8080/subscriptions/<id>/log?lines=50" | jq -r .log
```

---

### Update a subscription

```bash
# Change check interval
curl -s -X PATCH http://SERVER:8080/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"interval_hours": 12}' | jq

# Pause a subscription
curl -s -X PATCH http://SERVER:8080/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}' | jq

# Resume it
curl -s -X PATCH http://SERVER:8080/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}' | jq

# Change quality
curl -s -X PATCH http://SERVER:8080/subscriptions/<id> \
  -H "Content-Type: application/json" \
  -d '{"quality": "720"}' | jq
```

---

### Delete a subscription

```bash
curl -s -X DELETE http://SERVER:8080/subscriptions/<id> | jq
```

---

## Notes

- **Download archive** — each subscription has its own archive file at
  `/data/archives/<id>.txt` inside the container (persisted via the
  `ytdl-data` named volume). This is the key mechanism that prevents
  re-downloading videos and ensures nothing is missed — yt-dlp writes
  every downloaded video ID here and skips it on future runs.

- **yt-dlp updates** — rebuild the container periodically to pick up the
  latest yt-dlp release (YouTube changes its internals frequently):
  ```bash
  docker compose build --no-cache && docker compose up -d
  ```

- **Logs** — per-subscription logs live at `/data/logs/<id>.log` and are
  viewable via the `/log` endpoint above.

- **Interactive docs** — FastAPI auto-generates a Swagger UI at:
  `http://SERVER:8080/docs`
