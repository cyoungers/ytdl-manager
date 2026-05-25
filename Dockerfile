FROM python:3.12-slim

# ffmpeg for merging video+audio; nodejs/npm for yt-dlp JS runtime and bgutil server; git to clone bgutil server
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install bgutil PO token provider yt-dlp plugin
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

# Clone and build the bgutil server (expected at /root/bgutil-ytdlp-pot-provider/server)
# Pull latest before building to ensure generate_once.js is up to date
RUN git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider \
      /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider \
    && git pull \
    && cd server \
    && npm install \
    && npx tsc \
    && node build/generate_once.js --version

COPY app/ .
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
