FROM python:3.12-slim

# ffmpeg for merging video+audio; nodejs for yt-dlp JS runtime; npm for bgutil plugin
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install bgutil PO token provider plugin for yt-dlp
# This solves the GVS PO Token requirement for server IPs
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

COPY app/ .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
