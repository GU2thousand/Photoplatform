"""Container configuration shared by local Compose and AWS ECS tasks."""
import os
import ssl

import boto3
import pika
from botocore.config import Config


def aws_environment():
    return os.getenv("STORAGE_PROVIDER", "minio").lower() == "aws"


def storage_client():
    provider = os.getenv("STORAGE_PROVIDER", "minio").lower()
    if provider not in {"aws", "minio", "s3"}:
        raise ValueError("STORAGE_PROVIDER must be aws, minio, or s3")
    kwargs = {"region_name": os.getenv("STORAGE_REGION", os.getenv("AWS_REGION", "us-east-1"))}
    # No endpoint or explicit credentials in AWS mode: boto3 resolves the ECS task
    # role and refreshes temporary credentials via its standard credential chain.
    if provider != "aws":
        kwargs.update(endpoint_url=os.getenv("STORAGE_ENDPOINT") or None,
                      aws_access_key_id=os.getenv("STORAGE_ACCESS_KEY") or None,
                      aws_secret_access_key=os.getenv("STORAGE_SECRET_KEY") or None)
    path_style = provider != "aws" and os.getenv("STORAGE_PATH_STYLE_ACCESS", "true").lower() == "true"
    return boto3.client("s3", **kwargs, config=Config(signature_version="s3v4",
        connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3},
        s3={"addressing_style": "path" if path_style else "virtual"}))


def database_parameters():
    """Separate fields let Secrets Manager inject passwords without URI escaping."""
    kwargs = {"connect_timeout": int(os.getenv("DATABASE_CONNECT_TIMEOUT", "5")),
              "application_name": "photoplatform-worker",
              "options": "-c statement_timeout=" + str(int(os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "30000")))}
    if not os.getenv("DATABASE_URL"):
        kwargs.update(host=os.environ["DATABASE_HOST"], port=int(os.getenv("DATABASE_PORT", "5432")),
                      dbname=os.getenv("DATABASE_NAME", "photoplatform"), user=os.environ["DATABASE_USER"],
                      password=os.environ["DATABASE_PASSWORD"])
    mode = os.getenv("DATABASE_SSL_MODE", os.getenv("DATABASE_SSLMODE", "verify-full" if aws_environment() else "prefer"))
    if aws_environment() and mode != "verify-full":
        raise ValueError("AWS worker requires DATABASE_SSL_MODE=verify-full")
    # Preserve any stronger sslmode already present in a legacy local DSN.
    # AWS always overrides/validates it to verify-full.
    if (aws_environment() or not os.getenv("DATABASE_URL")
            or os.getenv("DATABASE_SSL_MODE") or os.getenv("DATABASE_SSLMODE")):
        kwargs["sslmode"] = mode
    root = os.getenv("DATABASE_SSL_ROOT_CERT", os.getenv("DATABASE_SSLROOTCERT"))
    if root or aws_environment():
        kwargs["sslrootcert"] = root or "/app/certs/global-bundle.pem"
    return kwargs


def rabbit_parameters():
    url = os.getenv("RABBITMQ_URL")
    if url:
        params = pika.URLParameters(url)
        tls = url.lower().startswith("amqps://")
    else:
        tls = os.getenv("RABBITMQ_SSL_ENABLED", os.getenv("RABBITMQ_TLS", str(aws_environment()))).lower() == "true"
        params = pika.ConnectionParameters(host=os.environ["RABBITMQ_HOST"],
            port=int(os.getenv("RABBITMQ_PORT", "5671" if tls else "5672")),
            virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
            credentials=pika.PlainCredentials(os.getenv("RABBITMQ_USER", os.getenv("RABBITMQ_USERNAME", "guest")),
                                               os.environ["RABBITMQ_PASSWORD"]))
    if aws_environment() and not tls:
        raise ValueError("AWS worker requires TLS for RabbitMQ")
    if tls:
        context = ssl.create_default_context(cafile=os.getenv("RABBITMQ_CA_FILE") or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        params.ssl_options = pika.SSLOptions(context, params.host)
    params.heartbeat = int(os.getenv("RABBITMQ_HEARTBEAT_SECONDS", "60"))
    params.blocked_connection_timeout = 30
    params.socket_timeout = 5
    params.stack_timeout = 15
    params.connection_attempts = 1  # Consumer owns the capped reconnect backoff.
    return params
