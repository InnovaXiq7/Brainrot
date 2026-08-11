FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=10000
ENV DEFAULT_SCENE_DURATION=10
ENV MAX_SCENES=12
EXPOSE 10000
CMD ["gunicorn","--bind","0.0.0.0:10000","--workers","1","--threads","2","--timeout","900","app:app"]
