FROM python:3.12-slim AS media
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache HF_HOME=/tmp/cache/huggingface TORCH_HOME=/tmp/cache/torch
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY certs ./certs
COPY --chmod=755 start.sh ./start.sh
RUN groupadd --gid 10001 worker && useradd --uid 10001 --gid 10001 --no-create-home worker
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/start.sh"]
CMD ["python", "-m", "app.consumer"]
