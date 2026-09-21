"""Bounded decoding, deterministic variants, and privacy-conscious metadata."""
import hashlib
import io
import warnings
from dataclasses import dataclass

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class InvalidImage(ValueError):
    pass


@dataclass(frozen=True)
class Variant:
    name: str
    data: bytes
    content_type: str
    width: int
    height: int

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


def process_image(data: bytes):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in {"JPEG", "PNG", "GIF", "BMP", "WEBP"}:
                    raise InvalidImage("Unsupported format")
                if probe.width * probe.height > MAX_PIXELS:
                    raise InvalidImage("Pixel limit exceeded")
                fmt = probe.format
                probe.verify()
            with Image.open(io.BytesIO(data)) as source:
                source.seek(0)  # Animated images use their first frame for previews/search.
                metadata = {"format": fmt, "frames": getattr(source, "n_frames", 1)}
                # Deliberately omit camera serial numbers, GPS, comments and arbitrary EXIF text.
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.load()
        original_type = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "BMP": "image/bmp", "WEBP": "image/webp"}[fmt]
        variants = [Variant("original", data, original_type, image.width, image.height)]
        for name, edge in (("thumbnail", 256), ("small", 512), ("medium", 1024), ("large", 1920)):
            resized = image.copy()
            resized.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            resized.save(output, "WEBP", quality=82, method=4, exif=b"")
            variants.append(Variant(name, output.getvalue(), "image/webp", resized.width, resized.height))
        return variants, str(imagehash.phash(image)), metadata
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage("Image could not be safely decoded") from exc
