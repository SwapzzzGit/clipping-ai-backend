# Clipper Studio backend (FastAPI + clipper + publisher), e.g. for Render.
# Render provides $PORT; data lives in /app/backend/data (mount a disk there to keep it).
FROM python:3.12-slim

# ffmpeg for rendering, Node.js as yt-dlp's JavaScript runtime, GL/GLib libs for OpenCV/MediaPipe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY clipper ./clipper
COPY publisher ./publisher
COPY backend ./backend
COPY config.yaml campaign_rules.txt ./
RUN pip install --no-cache-dir . -r backend/requirements.txt

ENV PYTHONUNBUFFERED=1 CLIPPER_DEFAULT_MODE=api
EXPOSE 8000
CMD ["sh", "-c", "python backend/run.py --host 0.0.0.0 --port ${PORT:-8000}"]
