#!/bin/bash
# Start the bgutil PO token server in the background, then start the app

# Find the bgutil server script
BGUTIL_SERVER=$(python3 -c "import bgutil_ytdlp_pot_provider; import os; print(os.path.join(os.path.dirname(bgutil_ytdlp_pot_provider.__file__), 'server.js'))" 2>/dev/null)

if [ -n "$BGUTIL_SERVER" ] && [ -f "$BGUTIL_SERVER" ]; then
    echo "Starting bgutil PO token server from: $BGUTIL_SERVER"
    node "$BGUTIL_SERVER" &
    BGUTIL_PID=$!
    echo "bgutil server started with PID $BGUTIL_PID"
    # Give it a moment to start
    sleep 2
else
    echo "WARNING: bgutil server script not found, PO tokens will not be available"
fi

# Start the main app
exec uvicorn main:app --host 0.0.0.0 --port 8080
