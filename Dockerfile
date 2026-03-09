FROM python:3.12-slim

# ffmpeg for merging video+audio; nodejs/npm for yt-dlp JS runtime and bgutil server
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install bgutil PO token provider — fixes YouTube 403s on server IPs
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

# Install the bgutil server's node dependencies
RUN BGUTIL_DIR=$(python3 -c "import bgutil_ytdlp_pot_provider, os; print(os.path.dirname(bgutil_ytdlp_pot_provider.__file__))") \
    && if [ -f "$BGUTIL_DIR/package.json" ]; then cd "$BGUTIL_DIR" && npm install; fi

COPY app/ .
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
