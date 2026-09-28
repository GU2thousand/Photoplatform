"""CLIP with one immutable official checkpoint and a verified local load path."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
from urllib.request import urlopen
from PIL import Image


MODEL_VERSION = "clip-vit-b32-openai-v1"
MODEL_ARCHITECTURE = "ViT-B-32"
MODEL_PROVIDER = "OpenAI"
MODEL_WEIGHTS = "OpenAI CLIP ViT-B/32"
# Official registry: https://github.com/openai/CLIP/blob/main/clip/clip.py
# Its downloader defines the URL's parent directory as the expected SHA256.
MODEL_SHA256 = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
MODEL_URL = (
    "https://openaipublic.azureedge.net/clip/models/"
    "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt"
)
MODEL_CACHE = Path(os.getenv("XDG_CACHE_HOME", "/tmp/cache")) / "photoplatform-clip"


def validate_model_contract():
    if os.getenv("CLIP_MODEL_VERSION", MODEL_VERSION) != MODEL_VERSION:
        raise ValueError("Unsupported CLIP model version")
    if os.getenv("CLIP_MODEL_SHA256", MODEL_SHA256).lower() != MODEL_SHA256:
        raise ValueError("Unsupported CLIP checkpoint digest")


def artifact_metadata(verified=False):
    return {"artifactSha256": MODEL_SHA256 if verified else None,
            "expectedArtifactSha256": MODEL_SHA256, "provider": MODEL_PROVIDER,
            "architecture": MODEL_ARCHITECTURE, "weights": MODEL_WEIGHTS,
            "artifactUrl": MODEL_URL, "artifactVerified": verified}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_file(path):
    if not path.is_file():
        raise FileNotFoundError("CLIP checkpoint is not a regular file")
    if _sha256(path) != MODEL_SHA256:
        raise ValueError("CLIP checkpoint checksum mismatch")
    return path


def verified_checkpoint():
    """No ML imports or downloads occur before validating the fixed contract."""
    validate_model_contract()
    explicit = os.getenv("CLIP_MODEL_PATH")
    if explicit:
        # A missing/tampered configured artifact must fail rather than silently
        # switching providers or accepting a caller-supplied replacement hash.
        return _verify_file(Path(explicit))
    MODEL_CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = MODEL_CACHE / "ViT-B-32.pt"
    if target.exists():
        return _verify_file(target)
    temporary = None
    try:
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=MODEL_CACHE, prefix=".download-", delete=False) as output:
            temporary = Path(output.name)
            with urlopen(MODEL_URL, timeout=30) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError("Downloaded CLIP checkpoint checksum mismatch")
        os.replace(temporary, target)
        temporary = None
        return _verify_file(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_libraries():
    import open_clip
    import torch
    return open_clip, torch


class Encoder:
    def __init__(self):
        checkpoint = verified_checkpoint()
        open_clip, torch = _load_libraries()
        self.model_version = MODEL_VERSION
        self.artifact_sha256 = MODEL_SHA256
        self.artifact_verified = True
        self.torch=torch
        torch.set_num_threads(int(os.getenv("TORCH_THREADS","2")))
        self.device=os.getenv("CLIP_DEVICE","cpu")
        # open_clip 3.3.0 accepts a local pretrained path. The official artifact
        # is TorchScript, requiring weights_only=False in its loader; trust is
        # established by the fixed official SHA256 before any deserialization.
        self.model,_,self.preprocess=open_clip.create_model_and_transforms(
            MODEL_ARCHITECTURE, pretrained=str(checkpoint), device=self.device,
            force_quick_gelu=True, require_pretrained=True, weights_only=False,
            cache_dir=str(MODEL_CACHE))
        self.model.eval()
        self.tokenize=open_clip.get_tokenizer(MODEL_ARCHITECTURE)
        self.lock=threading.Lock()

    def normalized(self,features):
        features=features/features.norm(dim=-1,keepdim=True)
        return features[0].cpu().float().tolist()

    def image(self,data):
        with Image.open(io.BytesIO(data)) as image:
            tensor=self.preprocess(image.convert("RGB")).unsqueeze(0).to(self.device)
        with self.lock,self.torch.inference_mode():
            return self.normalized(self.model.encode_image(tensor))

    def text(self,text):
        tokens=self.tokenize([text]).to(self.device)
        with self.lock,self.torch.inference_mode():
            return self.normalized(self.model.encode_text(tokens))
