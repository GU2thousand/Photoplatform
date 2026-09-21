from contextlib import asynccontextmanager
from functools import lru_cache
import hmac
import os
from fastapi import FastAPI,Header,HTTPException
from pydantic import BaseModel,Field
from .model import Encoder
from .telemetry import configure

tracer=configure("text-encoder")
version=os.getenv("CLIP_MODEL_VERSION","clip-vit-b32-openai-v1")
model=None


@asynccontextmanager
async def lifespan(app):
    global model
    model=Encoder()
    yield


app=FastAPI(lifespan=lifespan)


class Request(BaseModel):
    text:str=Field(min_length=1,max_length=300)
    modelVersion:str


@lru_cache(maxsize=512)
def encode_text(text):
    return model.text(text)


@app.get("/health")
def health(): return {"ready":model is not None,"modelVersion":version}


@app.post("/encode")
def encode(request:Request,authorization:str=Header(default="")):
    secret=os.environ.get("ENCODER_TOKEN","")
    if not secret or not hmac.compare_digest(authorization,"Bearer "+secret):
        raise HTTPException(401,"Unauthorized")
    if request.modelVersion!=version: raise HTTPException(409,"Model version mismatch")
    with tracer.start_as_current_span("clip.text_embedding"):
        return {"modelVersion":version,"embedding":encode_text(request.text)}
