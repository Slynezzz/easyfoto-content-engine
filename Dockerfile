FROM python:3.12-slim

# ffmpeg — сборка видео; tzdata — часовые пояса для расписания
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

CMD ["python", "-m", "engine.app"]
