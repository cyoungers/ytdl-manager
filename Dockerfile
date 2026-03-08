FROM python:3.12-slim

# ffmpeg required by yt-dlp to merge video+audio streams
# nodejs used by yt-dlp as a JavaScript runtime (required for some YouTube formats)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
