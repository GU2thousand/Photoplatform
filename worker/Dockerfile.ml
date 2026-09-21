FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements-ml.txt
COPY app ./app
RUN useradd --create-home worker && mkdir -p /home/worker/.cache && chown -R worker:worker /home/worker/.cache
USER worker
CMD ["python", "-m", "app.consumer"]
