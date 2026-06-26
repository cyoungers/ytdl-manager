#!/bin/bash
# Verify bgutil server files are present, start the bgutil POT HTTP server
# in the background, then start the app

BGUTIL_SCRIPT="/root/bgutil-ytdlp-pot-provider/server/build/generate_once.js"
BGUTIL_SERVER="/root/bgutil-ytdlp-pot-provider/server/build/main.js"

if [ -f "$BGUTIL_SCRIPT" ]; then
    echo "bgutil PO token script found: $BGUTIL_SCRIPT"
else
    echo "WARNING: bgutil script not found at $BGUTIL_SCRIPT"
    echo "Files in /root/bgutil-ytdlp-pot-provider/server/:"
    ls /root/bgutil-ytdlp-pot-provider/server/ 2>/dev/null || echo "(directory missing)"
fi

if [ -f "$BGUTIL_SERVER" ]; then
    echo "Starting bgutil POT HTTP server on port 4416"
    node "$BGUTIL_SERVER" &
else
    echo "WARNING: bgutil HTTP server not found at $BGUTIL_SERVER"
fi

cd /app
exec uvicorn main:app --host 0.0.0.0 --port 8080
