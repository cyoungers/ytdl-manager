#!/bin/bash
# Verify bgutil server files are present, then start the app

BGUTIL_SCRIPT="/root/bgutil-ytdlp-pot-provider/server/build/generate_once.js"

if [ -f "$BGUTIL_SCRIPT" ]; then
    echo "bgutil PO token script found: $BGUTIL_SCRIPT"
else
    echo "WARNING: bgutil script not found at $BGUTIL_SCRIPT"
    echo "Files in /root/bgutil-ytdlp-pot-provider/server/:"
    ls /root/bgutil-ytdlp-pot-provider/server/ 2>/dev/null || echo "(directory missing)"
fi

cd /app
exec uvicorn main:app --host 0.0.0.0 --port 8080
