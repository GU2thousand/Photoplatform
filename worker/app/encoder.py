"""Authenticated CLIP text service with independent process and model probes."""
from contextlib import asynccontextmanager
from functools import lru_cache
import hmac
import logging
import math
from numbers import Real
import os
import threading

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field

from .config import load_secret_files
from .model import Encoder, MODEL_SHA256, MODEL_VERSION, artifact_metadata, validate_model_contract
from .telemetry import configure

logger = logging.getLogger(__name__)
tracer = configure("text-encoder")
# This service supports exactly one artifact; arbitrary labels cannot relabel
# the installed OpenAI checkpoint or silently advertise another architecture.
version = MODEL_VERSION
MODEL_READY = Gauge("encoder_model_ready", "Verified CLIP model warmed and serving", ["model_version"])
MODEL_MISMATCHES = Counter("encoder_model_version_mismatches_total", "Requests for unsupported CLIP versions")
MODEL_READY.labels(version).set(0)
model = None
_state_lock = threading.Lock()
_generation = 0
initialization_finished = threading.Event()


def _validate_warmup(embedding):
    """Readiness requires the vector contract, not just allocated ML objects."""
    if len(embedding) != 512 or any(
        isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
        for value in embedding
    ):
        raise ValueError("Invalid CLIP warmup vector")
    norm = math.sqrt(math.fsum(float(value) ** 2 for value in embedding))
    if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
        raise ValueError("CLIP warmup vector is not normalized")


def _initialize_model(generation, finished):
    global model
    try:
        load_secret_files()
        validate_model_contract()
        candidate = Encoder()
        if (candidate.model_version != MODEL_VERSION or candidate.artifact_sha256 != MODEL_SHA256
                or not candidate.artifact_verified):
            raise ValueError("Encoder artifact contract mismatch")
        _validate_warmup(candidate.text("Photoplatform readiness warmup"))
        with _state_lock:
            # A slow load that finishes after shutdown must not make a later
            # application lifecycle ready or revive a stopped service.
            if generation == _generation:
                model = candidate
                MODEL_READY.labels(version).set(1)
    except Exception as error:
        # Dependency/download errors can include credentials or file contents.
        # Emit only the exception class, never the message or traceback.
        logger.error("Encoder initialization failed (%s)", type(error).__name__)
        with _state_lock:
            if generation == _generation:
                MODEL_READY.labels(version).set(0)
    finally:
        finished.set()


@asynccontextmanager
async def lifespan(app):
    global model, _generation, initialization_finished
    with _state_lock:
        _generation += 1
        generation = _generation
        model = None
        MODEL_READY.labels(version).set(0)
        initialization_finished = threading.Event()
        encode_text.cache_clear()
    # Serve /livez while imports, weight downloads and warmup run. A daemon
    # thread also avoids waiting for an interrupted download at process exit.
    threading.Thread(target=_initialize_model,
                     args=(generation, initialization_finished), daemon=True,
                     name="clip-model-loader").start()
    try:
        yield
    finally:
        with _state_lock:
            _generation += 1
            model = None
            MODEL_READY.labels(version).set(0)
            encode_text.cache_clear()


app = FastAPI(lifespan=lifespan)


class Request(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    modelVersion: str


@lru_cache(maxsize=512)
def encode_text(text, encoder):
    # Include the instance to keep retired in-flight requests out of a newer
    # model's cache if the application lifecycle changes.
    return encoder.text(text)


def _health():
    with _state_lock:
        ready = model is not None
        return {"ready": ready, "modelVersion": version, **artifact_metadata(verified=ready)}


@app.get("/livez")
def livez():
    return {"status": "alive"}


@app.get("/readyz")
def readyz():
    health = _health()
    return JSONResponse(health, status_code=200 if health["ready"] else 503)


@app.get("/health")
def health():
    # Preserve existing ready/modelVersion fields and informational HTTP 200.
    return _health()


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.post("/encode")
def encode(request: Request, authorization: str = Header(default="")):
    secret = os.environ.get("ENCODER_TOKEN", "")
    if not secret or not hmac.compare_digest(
        authorization.encode("utf-8"), ("Bearer " + secret).encode("utf-8")
    ):
        raise HTTPException(401, "Unauthorized")
    if request.modelVersion != version:
        MODEL_MISMATCHES.inc()
        raise HTTPException(409, "Model version mismatch")
    with _state_lock:
        encoder = model
    if encoder is None:
        raise HTTPException(503, "Encoder is not ready")
    with tracer.start_as_current_span("clip.text_embedding"):
        return {"modelVersion": version, "embedding": encode_text(request.text, encoder)}
