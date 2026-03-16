#!/usr/bin/env bash
# ytdl-manager management script
# Usage: ./scripts/ytdl.sh

API="http://192.168.0.166:8911"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── helpers ────────────────────────────────────────────────────────────────
require() { command -v "$1" &>/dev/null || { echo -e "${RED}Error: '$1' not found${RESET}"; exit 1; }; }
require curl; require jq

api()  { curl -sf "$API$1" "${@:2}"; }
apij() { api "$@" | jq; }

# Convert a UTC ISO timestamp to local time (e.g. "2026-03-09T01:16:31+00:00" → "Mar 09 07:16 CST")
to_local() {
  local ts="$1"
  [[ -z "$ts" || "$ts" == "null" || "$ts" == "never" ]] && echo "${ts:-never}" && return
  date -d "$ts" "+%b %d %H:%M %Z" 2>/dev/null \
    || date -j -f "%Y-%m-%dT%H:%M:%S" "${ts:0:19}" "+%b %d %H:%M %Z" 2>/dev/null \
    || echo "$ts"
}

hr()   { echo -e "${CYAN}$(printf '─%.0s' {1..60})${RESET}"; }
hdr()  { hr; echo -e "${BOLD}${CYAN}  $1${RESET}"; hr; }
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠  $1${RESET}"; }
err()  { echo -e "${RED}✗ $1${RESET}"; }

pause() { echo; read -rp "  Press Enter to continue..."; }

# ── health check ───────────────────────────────────────────────────────────
check_api() {
  curl -sf "$API/health" &>/dev/null || {
    err "Cannot reach API at $API"
    err "Is the container running?  docker ps --filter name=ytdl"
    exit 1
  }
}

# ── subscription helpers ───────────────────────────────────────────────────
list_subs_raw() { api /subscriptions | jq -r '.[] | "\(.id)\t\(.name)\t\(.enabled)\t\(.last_checked // "never")"'; }

pick_sub() {
  # Prints "id name" lines and lets user pick; sets $SUB_ID and $SUB_NAME
  local lines
  lines=$(api /subscriptions | jq -r '.[] | "\(.id)  \(.name)  [\(if .enabled then "enabled" else "paused" end)]"')
  if [[ -z "$lines" ]]; then warn "No subscriptions found."; return 1; fi
  echo
  echo -e "${BOLD}  Select a subscription:${RESET}"
  local i=1
  declare -a ids names
  while IFS= read -r line; do
    local id name
    id=$(echo "$line" | awk '{print $1}')
    name=$(echo "$line" | awk '{print $2}')
    ids+=("$id"); names+=("$name")
    printf "  ${YELLOW}%2d)${RESET} %s\n" "$i" "$line"
    ((i++))
  done <<< "$lines"
  echo
  read -rp "  Choice [1-$((i-1))]: " choice
  [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice < i )) || { err "Invalid choice"; return 1; }
  SUB_ID="${ids[$((choice-1))]}"
  SUB_NAME="${names[$((choice-1))]}"
}

# ── ADD SUBSCRIPTION ───────────────────────────────────────────────────────
cmd_add() {
  hdr "Add Subscription"

  # Channel URL
  echo -e "  ${BOLD}Channel/Playlist URL${RESET}"
  echo    "  Examples:"
  echo    "    https://www.youtube.com/channel/UC0YvoAYGgdOfySQSLcxtu1w"
  echo    "    https://www.youtube.com/@channelhandle"
  echo    "    https://www.youtube.com/playlist?list=PLxxxxxx"
  echo
  read -rp "  URL: " url
  [[ -z "$url" ]] && { err "URL is required"; return; }

  # Name
  read -rp "  Name (display label): " name
  [[ -z "$name" ]] && name="$url"

  # Output dir — auto-default to /downloads/<name>
  local default_dir="/downloads/${name// /_}"
  echo
  echo -e "  ${BOLD}Output directory${RESET} (must start with /downloads/)"
  read -rp "  Output dir [${default_dir}]: " output_dir
  output_dir="${output_dir:-$default_dir}"
  # Auto-prefix /downloads/ if user omitted it
  if [[ "$output_dir" != /downloads/* ]]; then
    output_dir="/downloads/$output_dir"
    echo -e "  ${YELLOW}→ Adjusted to: $output_dir${RESET}"
  fi

  # Interval
  echo
  read -rp "  Check interval in hours [default: 6]: " interval
  interval=${interval:-6}

  # Quality
  echo
  echo -e "  ${BOLD}Quality:${RESET}  1) 1080  2) 720  3) 480  4) best  5) 1440"
  read -rp "  Choice [default: 1]: " qchoice
  case "$qchoice" in
    2) quality="720"  ;;
    3) quality="480"  ;;
    4) quality="best" ;;
    5) quality="1440" ;;
    *) quality="1080" ;;
  esac

  # date_after (playlist only — channels use RSS)
  local date_after_json="null"
  if [[ "$url" == *"playlist"* ]]; then
    echo
    echo -e "  ${BOLD}Date filter${RESET} (playlist only — leave blank to download all)"
    echo    "  Examples: today-7days  today-30days  20250101"
    read -rp "  date_after: " date_after
    [[ -n "$date_after" ]] && date_after_json="\"$date_after\""
  fi

  # Confirm
  echo
  hr
  echo -e "  ${BOLD}Summary:${RESET}"
  printf "    %-16s %s\n" "URL:"        "$url"
  printf "    %-16s %s\n" "Name:"       "$name"
  printf "    %-16s %s\n" "Output dir:" "$output_dir"
  printf "    %-16s %s\n" "Interval:"   "${interval}h"
  printf "    %-16s %s\n" "Quality:"    "$quality"
  [[ "$date_after_json" != "null" ]] && printf "    %-16s %s\n" "date_after:" "$date_after"
  hr
  echo
  read -rp "  Add this subscription? [y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { warn "Cancelled."; return; }

  local payload
  payload=$(jq -n \
    --arg url        "$url"        \
    --arg name       "$name"       \
    --arg output_dir "$output_dir" \
    --argjson interval "$interval" \
    --arg quality    "$quality"    \
    --argjson date_after "$date_after_json" \
    '{url:$url, name:$name, output_dir:$output_dir,
      interval_hours:$interval, quality:$quality, date_after:$date_after}')

  local resp http_code
  resp=$(curl -s -w "\n%{http_code}" -X POST "$API/subscriptions" \
    -H "Content-Type: application/json" \
    -d "$payload")
  http_code=$(echo "$resp" | tail -1)
  resp=$(echo "$resp" | head -n -1)

  if [[ "$http_code" == "409" ]]; then
    local detail
    detail=$(echo "$resp" | jq -r '.detail')
    warn "Already subscribed: $detail"
    pause; return
  elif [[ "$http_code" != "201" ]]; then
    err "API call failed (HTTP $http_code)"
    pause; return
  fi

  local id
  id=$(echo "$resp" | jq -r '.id')
  ok "Subscription added!  ID: $id"
  echo -e "  Initial run started in background."
  pause
  cmd_list
}

# ── LIST SUBSCRIPTIONS ─────────────────────────────────────────────────────
cmd_list() {
  hdr "Subscriptions"
  local data
  data=$(api /subscriptions) || { err "API call failed"; return; }
  local count
  count=$(echo "$data" | jq 'length')
  if [[ "$count" -eq 0 ]]; then warn "No subscriptions."; pause; return; fi

  echo "$data" | jq -r '.[] | [.id, .name, .quality, (.interval_hours|tostring)+"h",
    (if .enabled then "enabled" else "PAUSED" end), (.last_checked // "never")] | @tsv' \
  | while IFS=$'\t' read -r id name quality interval status checked; do
    local color="$GREEN"
    [[ "$status" == "PAUSED" ]] && color="$YELLOW"
    local checked_local
    checked_local=$(to_local "$checked")
    printf "  ${CYAN}%s${RESET}  ${BOLD}%-20s${RESET}  %-6s  %-5s  ${color}%-7s${RESET}  %s\n" \
      "$id" "$name" "$quality" "$interval" "$status" "$checked_local"
  done
  echo
  pause
}

# Convert UTC timestamps in log output to local Pacific time
utc_to_pacific() {
  python3 -c "
import sys, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
tz = ZoneInfo('America/Los_Angeles')
for line in sys.stdin:
    def convert(m):
        dt = datetime.strptime(m.group(0), '%Y-%m-%d %H:%M:%S UTC')
        dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
        return dt.strftime('%Y-%m-%d %H:%M:%S %Z')
    line = re.sub(r'\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} UTC', convert, line)
    sys.stdout.write(line)
    sys.stdout.flush()
"
}

# ── VIEW LOGS ──────────────────────────────────────────────────────────────
cmd_logs() {
  hdr "View Logs"
  pick_sub || { pause; return; }
  echo
  echo -e "  Enter number of lines to show, or ${BOLD}s${RESET} to scroll/follow live:"
  read -rp "  [default: 80]: " lines
  echo

  if [[ "$lines" == "s" || "$lines" == "S" ]]; then
    # Get the log file path from inside the container and tail -f it via docker exec
    local container
    container=$(docker ps --filter name=ytdl --format "{{.Names}}" 2>/dev/null | head -1)
    if [[ -z "$container" ]]; then
      err "No ytdl container found."
      pause; return
    fi
    hr
    echo -e "  ${CYAN}Scrolling log for ${BOLD}$SUB_NAME${RESET}${CYAN} — press Ctrl+C to stop${RESET}"
    hr
    docker exec "$container" tail -f "/data/logs/$SUB_ID.log" | utc_to_pacific
    hr
    pause
  else
    lines=${lines:-80}
    hr
    api "/subscriptions/$SUB_ID/log?lines=$lines" | jq -r '.log' | utc_to_pacific
    hr
    pause
  fi
}

# ── TRIGGER MANUAL CHECK ───────────────────────────────────────────────────
cmd_check() {
  hdr "Trigger Manual Check"
  pick_sub || { pause; return; }
  local resp
  resp=$(curl -sf -X POST "$API/subscriptions/$SUB_ID/check") || { err "API call failed"; return; }
  ok "Check triggered for '$SUB_NAME'"
  echo -e "  Use ${CYAN}View Logs${RESET} to follow progress."
  pause
}

# ── JOB STATUS ─────────────────────────────────────────────────────────────
cmd_jobs() {
  hdr "Job Status"
  local data
  data=$(api /jobs) || { err "API call failed"; return; }

  echo -e "${BOLD}  Running / Recent Jobs:${RESET}"
  local jobs
  jobs=$(echo "$data" | jq -r '.jobs[] |
    [.job_id, .sub_name, .trigger, .status,
     (.videos_found|tostring), (.videos_done|tostring), (.videos_failed|tostring),
     .started_at] | @tsv')

  if [[ -z "$jobs" ]]; then
    warn "  No jobs in history."
  else
    printf "  ${BOLD}%-10s %-16s %-10s %-10s %5s %5s %5s  %s${RESET}\n" \
      "ID" "Name" "Trigger" "Status" "Found" "Done" "Fail" "Started"
    while IFS=$'\t' read -r jid name trigger status found done fail started; do
      local color="$RESET"
      [[ "$status" == "running"   ]] && color="$CYAN"
      [[ "$status" == "completed" ]] && color="$GREEN"
      [[ "$status" == "failed"    ]] && color="$RED"
      local started_local
      started_local=$(to_local "$started")
      printf "  ${color}%-10s %-16s %-10s %-10s %5s %5s %5s  %s${RESET}\n" \
        "$jid" "$name" "$trigger" "$status" "$found" "$done" "$fail" "$started_local"
    done <<< "$jobs"
  fi

  echo
  echo -e "${BOLD}  Upcoming Scheduled Runs:${RESET}"
  echo "$data" | jq -r '.scheduled[] | "\(.sub_name)\t\(.next_run // "not scheduled")"' \
  | while IFS=$'\t' read -r name next_run; do
      echo "  $name  →  $(to_local "$next_run")"
    done
  echo
  pause
}

# ── PAUSE / RESUME ─────────────────────────────────────────────────────────
cmd_toggle() {
  hdr "Pause / Resume Subscription"
  pick_sub || { pause; return; }
  local sub enabled
  sub=$(api "/subscriptions/$SUB_ID")
  enabled=$(echo "$sub" | jq -r '.enabled')
  local new_state action
  if [[ "$enabled" == "true" ]]; then
    new_state="false"; action="Paused"
  else
    new_state="true"; action="Resumed"
  fi
  curl -sf -X PATCH "$API/subscriptions/$SUB_ID" \
    -H "Content-Type: application/json" \
    -d "{\"enabled\": $new_state}" >/dev/null || { err "API call failed"; return; }
  ok "$action '$SUB_NAME'"
  pause
}

# ── UPDATE SUBSCRIPTION ────────────────────────────────────────────────────
cmd_update() {
  hdr "Update Subscription"
  pick_sub || { pause; return; }
  echo
  echo -e "  Updating ${BOLD}$SUB_NAME${RESET} (leave blank to keep current value)"
  echo
  read -rp "  url: " url
  read -rp "  name: " name
  read -rp "  interval_hours: " interval
  read -rp "  quality (best/1440/1080/720/480): " quality
  echo
  echo -e "  ${BOLD}date_after${RESET} — only download videos uploaded on or after this date (playlists only)."
  echo    "  Format: YYYYMMDD  or  a relative value like today-Ndays / today-Nmonths / today-Nyear"
  echo    "  Examples:  20250101       (Jan 1, 2025)"
  echo    "             today-7days    (last 7 days)"
  echo    "             today-30days   (last 30 days)"
  echo    "             today-6months  (last 6 months)"
  echo    "             today-1year    (last year)"
  echo    "  Leave blank to keep current value. Enter 'clear' to remove the filter."
  read -rp "  date_after: " date_after
  echo

  local payload="{}"
  [[ -n "$url"        ]] && payload=$(echo "$payload" | jq --arg v "$url" '.url=$v')
  [[ -n "$name"       ]] && payload=$(echo "$payload" | jq --arg v "$name" '.name=$v')
  [[ -n "$interval"   ]] && payload=$(echo "$payload" | jq --argjson v "$interval" '.interval_hours=$v')
  [[ -n "$quality"    ]] && payload=$(echo "$payload" | jq --arg v "$quality" '.quality=$v')
  [[ -n "$date_after" ]] && payload=$(echo "$payload" | jq --arg v "$date_after" '.date_after=$v')

  if [[ "$payload" == "{}" ]]; then warn "Nothing to update."; pause; return; fi

  curl -sf -X PATCH "$API/subscriptions/$SUB_ID" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null || { err "API call failed"; return; }
  ok "Updated '$SUB_NAME'"
  pause
}

# ── DELETE SUBSCRIPTION ────────────────────────────────────────────────────
cmd_delete() {
  hdr "Delete Subscription"
  pick_sub || { pause; return; }
  echo
  warn "This will remove the subscription (downloaded files are kept)."
  read -rp "  Delete '$SUB_NAME'? [y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { warn "Cancelled."; pause; return; }
  curl -sf -X DELETE "$API/subscriptions/$SUB_ID" >/dev/null || { err "API call failed"; return; }
  ok "Deleted '$SUB_NAME'"
  pause
}

# ── REFRESH COOKIES ────────────────────────────────────────────────────────
cmd_cookies() {
  hdr "Refresh YouTube Cookies"
  # Prefer www.youtube.com_cookies.txt; fall back to most-recent cookies*.txt
  local src
  if [[ -f "$HOME/Downloads/www.youtube.com_cookies.txt" ]]; then
    src="$HOME/Downloads/www.youtube.com_cookies.txt"
  else
    src=$(ls -t "$HOME/Downloads/cookies"*.txt 2>/dev/null | head -1)
  fi
  if [[ -z "$src" || ! -f "$src" ]]; then
    err "No cookies file found in ~/Downloads"
    echo
    echo "  1. Open Chrome and log into YouTube"
    echo "  2. Click the 'Get cookies.txt LOCALLY' extension"
    echo "  3. Export — saves to ~/Downloads/cookies.txt or www.youtube.com_cookies.txt"
    echo "  4. Re-run this option"
    pause; return
  fi

  local lines
  lines=$(wc -l < "$src")
  echo -e "  Found ${BOLD}$src${RESET} (${lines} lines)"
  echo

  local container
  container=$(docker ps --filter name=ytdl --format "{{.Names}}" 2>/dev/null | head -1)
  if [[ -z "$container" ]]; then
    err "No ytdl container found.  Is Docker running?"
    pause; return
  fi

  echo -e "  Container: ${BOLD}$container${RESET}"
  read -rp "  Copy cookies into container? [y/N]: " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { warn "Cancelled."; pause; return; }

  if docker cp "$src" "$container:/data/cookies.txt"; then
    ok "Cookies updated!"
    # Remove all cookies files (both naming conventions) from Downloads
    rm -f "$HOME/Downloads/cookies"*.txt "$HOME/Downloads/www.youtube.com_cookies.txt" \
      && echo -e "  Cleaned up local cookies files from ~/Downloads"
  else
    err "Copy failed"
  fi
  pause
}

# Strip date and YouTube ID from filename for display
# Truncate a filename keeping the start and extension
truncate_filename() {
  local name="$1"
  local max="${2:-70}"
  if [[ "${#name}" -le "$max" ]]; then
    echo "$name"
    return
  fi
  local ext="${name##*.}"
  local base="${name%.*}"
  local keep=$(( max - ${#ext} - 4 ))
  echo "${base:0:$keep}...${ext}"
}

# Strip _(date)_[id] from filename for cleaner display
# e.g. "FIRST SPIN MAGIC_(2026_03_03)_[GKNvJvhzHZ0].mp4" → "FIRST SPIN MAGIC.mp4"
clean_filename() {
  local name="$1"
  # Use sed extended regex to strip _(YYYY_MM_DD)_[videoID] before the final .ext
  echo "$name" | sed -E 's/_\([0-9]{4}_[0-9]{2}_[0-9]{2}\)_\[[A-Za-z0-9_-]+\](\.[^.]+)$/\1/'
}
cmd_downloads() {
  hdr "Recent Downloads"
  read -rp "  How many entries to show [default: 30]: " n
  n=${n:-30}
  local data
  data=$(api "/downloads-log?lines=$n") || { err "API call failed"; return; }
  local count
  count=$(echo "$data" | jq '.entries | length')
  if [[ "$count" -eq 0 ]]; then
    warn "No downloads logged yet."
    pause; return
  fi
  printf "\n  ${BOLD}%-22s %-16s %s${RESET}\n" "Downloaded" "Subscription" "Filename"
  hr
  echo "$data" | jq -r '.entries[] | [.timestamp, .subscription, .filename] | @tsv' \
  | while IFS=$'\t' read -r ts sub filename; do
      local ts_local short_name
      ts_local=$(to_local "$ts")
      short_name=$(truncate_filename "$(clean_filename "$filename")" 70)
      printf "  %-22s ${CYAN}%-16s${RESET} %s\n" "$ts_local" "$sub" "$short_name"
    done
  echo
  pause
}


cmd_container() {
  hdr "Container Operations"
  echo "  1) Show container status"
  echo "  2) Show recent container logs (stderr/stdout)"
  echo "  3) Restart container"
  echo "  4) Update yt-dlp inside container"
  echo "  5) Back"
  echo
  read -rp "  Choice: " c
  case "$c" in
    1)
      docker ps --filter name=ytdl --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
      ;;
    2)
      local container
      container=$(docker ps --filter name=ytdl --format "{{.Names}}" | head -1)
      [[ -z "$container" ]] && { err "Container not found"; pause; return; }
      docker logs --tail 50 "$container"
      ;;
    3)
      local container
      container=$(docker ps --filter name=ytdl --format "{{.Names}}" | head -1)
      [[ -z "$container" ]] && { err "Container not found"; pause; return; }
      read -rp "  Restart '$container'? [y/N]: " confirm
      [[ "$confirm" =~ ^[Yy]$ ]] || { warn "Cancelled."; pause; return; }
      docker restart "$container" && ok "Restarted" || err "Failed"
      ;;
    4)
      local container
      container=$(docker ps --filter name=ytdl --format "{{.Names}}" | head -1)
      [[ -z "$container" ]] && { err "Container not found"; pause; return; }
      echo "  Updating yt-dlp..."
      docker exec "$container" pip install -q --upgrade yt-dlp && ok "yt-dlp updated" || err "Failed"
      ;;
  esac
  pause
}

# ── HEALTH ─────────────────────────────────────────────────────────────────
cmd_health() {
  hdr "Health Check"
  local resp
  resp=$(curl -sf "$API/health") && ok "API is up: $resp" || err "API not reachable at $API"
  echo
  docker ps --filter name=ytdl --format "  Container: {{.Names}}  ({{.Status}})" 2>/dev/null \
    || warn "docker not available or no container running"
  pause
}

# ══════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════════════════
main_menu() {
  while true; do
    clear
    echo -e "${BOLD}${CYAN}"
    echo "  ╔═══════════════════════════════════╗"
    echo "  ║        yt-dlp Manager             ║"
    echo "  ╚═══════════════════════════════════╝"
    echo -e "${RESET}"
    echo -e "  ${YELLOW}Subscriptions${RESET}"
    echo    "    1)  Add subscription"
    echo    "    2)  List subscriptions"
    echo    "    3)  Trigger manual check"
    echo    "    4)  View logs"
    echo    "    5)  Pause / Resume"
    echo    "    6)  Update settings"
    echo    "    7)  Delete subscription"
    echo
    echo -e "  ${YELLOW}Monitoring${RESET}"
    echo    "    8)  Job status"
    echo    "    9)  Recent downloads"
    echo    "    10) Health check"
    echo
    echo -e "  ${YELLOW}Maintenance${RESET}"
    echo    "    11) Refresh YouTube cookies"
    echo    "    12) Container operations"
    echo
    echo    "    q)  Quit"
    echo
    read -rp "  Choice: " opt
    case "$opt" in
      1)  cmd_add       ;;
      2)  cmd_list      ;;
      3)  cmd_check     ;;
      4)  cmd_logs      ;;
      5)  cmd_toggle    ;;
      6)  cmd_update    ;;
      7)  cmd_delete    ;;
      8)  cmd_jobs      ;;
      9)  cmd_downloads ;;
      10) cmd_health    ;;
      11) cmd_cookies   ;;
      12) cmd_container ;;
      q|Q) echo; ok "Bye!"; echo; exit 0 ;;
      *) warn "Unknown option" ;;
    esac
  done
}

# ── entry point ────────────────────────────────────────────────────────────
check_api
main_menu
