# Non-root CLIP cache migration

The ML image runs as UID/GID `10001:10001`. Its build creates `/home/worker/.cache` and `/tmp/cache` with that ownership before switching to the runtime user. Compose mounts the new `model-cache-v2` named volume at `/home/worker/.cache` for both `encoder` and `embedding-worker`. Docker initializes a fresh empty named volume from the owned image directory, allowing the ordinary runtime user to create the pinned checkpoint cache.

An existing named volume hides the directory supplied by the new image. Rebuilding the image therefore does not repair an older root-owned cache volume. The new Compose key deliberately avoids reusing `model-cache`; Docker retains that old volume and its files. There is no root runtime, privileged repair container, or initialization service that changes existing volume ownership. This migration does not modify PostgreSQL, object storage, or RabbitMQ volumes.

## Use the fresh cache

Build the updated ML image and replace the encoder and embedding worker using their normal graceful lifecycle. The first start with a fresh `model-cache-v2` volume downloads the fixed official checkpoint and verifies its full SHA256 before deserialization. Compose sets `XDG_CACHE_HOME=/home/worker/.cache`, so the checkpoint is stored at `/home/worker/.cache/photoplatform-clip/ViT-B-32.pt`.

If a volume named `model-cache-v2` already exists, confirm its ownership and users before starting; the versioned key alone does not make a retained volume fresh. Do not automatically reuse or rewrite a retained root-owned cache. Keep the old volume available while verifying the new deployment. An unrelated volume or a host directory must not be mounted as a cache replacement.

Kubernetes uses a separate `emptyDir` cache per Pod. The Pod security context already sets `fsGroup: 10001`, and the process still runs as UID/GID `10001:10001`. These writable cache and `/tmp` mounts support a read-only root filesystem. The Compose volume change does not require a Helm permission repair or a root init container.

## Optional recovery from the old volume

Downloading a fresh verified checkpoint is the default. If an old checkpoint must be reused, first identify the actual Compose project-prefixed old volume and inspect its container references. Mount only that named volume read-only, using the existing ML image as UID/GID `10001:10001`. Never make the old volume writable merely to recover a file.

Only the official OpenAI ViT-B/32 bytes are accepted. Before any copy or model use, calculate the entire file's SHA256 and require exactly:

```text
40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af
```

The expected digest comes from the [official OpenAI CLIP registry](https://github.com/openai/CLIP/blob/main/clip/clip.py). A Hugging Face cache filename or the configured model version is insufficient evidence. Do not substitute a different `CLIP_MODEL_SHA256` to accept arbitrary bytes. If the old file cannot be read by UID `10001`, leave it untouched and use the fresh download route.

For an optional verified copy, use a separate UID/GID `10001:10001` process with only the identified old named volume mounted read-only and the fresh cache volume mounted writable. Copy the already verified file into a temporary file in the new checkpoint directory, verify the copied digest again, and atomically rename it to `ViT-B-32.pt`. The loader independently rechecks the bytes before use. Do not mount external host files, use `chmod 777`, or perform a blanket `chown` of retained data.

Keep the old volume after the migration. Cleanup is a separate explicit action only after confirming that no running, stopped, or ongoing task still needs it and the new deployment's cache/inference checks have completed. Broad volume-pruning or Compose volume-removal commands are inappropriate for this migration because they can remove unrelated durable state.

## Verification scope

The image filesystem smoke covers a fresh named cache volume, execution as UID/GID `10001:10001`, and a read-only root filesystem with the intended writable mounts. It checks permission behavior without establishing model accuracy, model download success, or inference performance. Actual checkpoint loading and image/text inference require the hosted stack rerun; that rerun is pending. A passed filesystem smoke alone does not satisfy the real-CLIP acceptance case.
