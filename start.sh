#!/bin/bash
# Start the bgutil PO token server, then start the app

# Find bgutil server script location
BGUTIL_DIR=$(pip show bgutil-ytdlp-pot-provider 2>/dev/null | grep Location | awk '{print $2}')
BGUTIL_SERVER=""

# Search common locations
for candidate in \
    "$BGUTIL_DIR/bgutil_ytdlp_pot_provider/server.js" \
    "$BGUTIL_DIR/bgutil-ytdlp-pot-provider/server.js" \
    "/usr/local/lib/python3.12/site-packages/bgutil_ytdlp_pot_provider/server.js" \
    "/usr/local/lib/python3.12/site-packages/bgutil-ytdlp-pot-provider/server.js"
do
    if [ -f "$candidate" ]; then
        BGUTIL_SERVER="$candidate"
        break
    fi
done

# If not found by path, try find
if [ -z "$BGUTIL_SERVER" ]; then
    BGUTIL_SERVER=$(find /usr/local/lib -name "server.js" -path "*/bgutil*" 2>/dev/null | head -1)
fi

if [ -n "$BGUTIL_SERVER" ]; then
    echo "Starting bgutil PO token server: $BGUTIL_SERVER"
    SERVER_DIR=$(dirname "$BGUTIL_SERVER")
    # Install node deps if needed
    if [ -f "$SERVER_DIR/package.json" ] && [ ! -d "$SERVER_DIR/node_modules" ]; then
        echo "Installing bgutil node dependencies..."
        cd "$SERVER_DIR" && npm install --silent
    fi
    node "$BGUTIL_SERVER" &
    echo "bgutil server started (PID $!)"
    sleep 2
else
    echo "WARNING: bgutil server.js not found — PO tokens unavailable"
    find /usr/local/lib -name "*.js" -path "*/bgutil*" 2>/dev/null | head -5
fi

cd /app
exec uvicorn main:app --host 0.0.0.0 --port 8080
