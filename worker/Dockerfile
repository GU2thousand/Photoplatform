FROM python:3.12-slim AS media
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY certs ./certs
RUN useradd --create-home worker && mkdir -p /home/worker/.cache && chown -R worker:worker /home/worker/.cache
USER worker
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app.consumer"]
