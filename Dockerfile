FROM python:3.11-slim

# Install system binaries: ffmpeg, streamlink, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    streamlink \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp and Python dependencies
RUN pip install --no-cache-dir fastapi uvicorn pydantic yt-dlp google-genai python-multipart

WORKDIR /app

# Copy application files
COPY gta6_clipper/ /app/gta6_clipper/

# Create necessary directories
RUN mkdir -p /app/gta6_clipper/streams /app/gta6_clipper/clips /app/gta6_clipper/logs /app/gta6_clipper/static

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "gta6_clipper.app:app", "--host", "0.0.0.0", "--port", "8000"]
