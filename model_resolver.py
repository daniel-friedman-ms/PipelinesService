"""Pull-through model cache — fetches .pt files from the models service on demand."""

import asyncio
import hashlib
import logging
import os
import uuid

import httpx

from config import MODEL_CACHE_DIR, MODELS_SERVICE_URL

logger = logging.getLogger(__name__)

# Per-filename locks to prevent concurrent downloads of the same model
_download_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_lock(filename: str) -> asyncio.Lock:
    """Get or create a per-filename lock."""
    async with _locks_guard:
        if filename not in _download_locks:
            _download_locks[filename] = asyncio.Lock()
        return _download_locks[filename]


def _model_path(filename: str) -> str:
    return os.path.join(MODEL_CACHE_DIR, filename)


def _sha_path(filename: str) -> str:
    return os.path.join(MODEL_CACHE_DIR, f"{filename}.sha256")


def _read_local_sha(filename: str) -> str | None:
    """Read the SHA-256 sidecar file, or None if it doesn't exist."""
    path = _sha_path(filename)
    if os.path.isfile(path):
        return open(path).read().strip()
    return None


async def _fetch_remote_info(client: httpx.AsyncClient, filename: str) -> dict | None:
    """Fetch model info (sha256, size_bytes) from the models service. Returns None on failure."""
    try:
        resp = await client.get(f"{MODELS_SERVICE_URL}/models/{filename}/info")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        logger.warning(f"Failed to fetch model info for {filename}: {e}")
        return None


async def _download_model(client: httpx.AsyncClient, filename: str) -> str:
    """Download a model file from the models service. Returns the SHA-256 hash."""
    tmp_name = f"{filename}.tmp.{uuid.uuid4().hex[:8]}"
    tmp_path = os.path.join(MODEL_CACHE_DIR, tmp_name)
    final_path = _model_path(filename)

    sha = hashlib.sha256()

    try:
        async with client.stream("GET", f"{MODELS_SERVICE_URL}/models/{filename}/download") as resp:
            if resp.status_code == 404:
                raise FileNotFoundError(f"Model '{filename}' not found on models service at {MODELS_SERVICE_URL}")
            resp.raise_for_status()

            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    sha.update(chunk)

        # Atomic replace
        os.replace(tmp_path, final_path)
        logger.info(f"Downloaded model {filename} ({os.path.getsize(final_path) / 1024 / 1024:.1f} MB)")

    except BaseException:
        # Clean up temp file on any failure
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    file_sha = sha.hexdigest()

    # Write SHA-256 sidecar
    with open(_sha_path(filename), "w") as f:
        f.write(file_sha)

    return file_sha


async def ensure_model_on_disk(filename: str) -> tuple[str, bool]:
    """Ensure a model file exists in the local cache, fetching from models service if needed.

    Returns (path, changed) where changed=True if the file was downloaded or updated.
    """
    lock = await _get_lock(filename)
    async with lock:
        path = _model_path(filename)
        local_sha = _read_local_sha(filename)

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30)) as client:
            if os.path.isfile(path):
                # File exists — check staleness
                remote_info = await _fetch_remote_info(client, filename)

                if remote_info is None:
                    # Models service unreachable — use cached copy
                    logger.warning(f"Models service unreachable, using cached model {filename}")
                    return path, False

                remote_sha = remote_info.get("sha256", "")
                if local_sha and local_sha == remote_sha:
                    return path, False

                # Stale — re-download
                logger.info(f"Model {filename} is stale (local={local_sha[:8] if local_sha else 'none'}..., remote={remote_sha[:8]}...), re-downloading")
                await _download_model(client, filename)
                return path, True

            else:
                # File doesn't exist — download
                remote_info = await _fetch_remote_info(client, filename)
                if remote_info is None:
                    raise FileNotFoundError(
                        f"Model '{filename}' not found in local cache and models service "
                        f"at {MODELS_SERVICE_URL} is unreachable"
                    )

                await _download_model(client, filename)
                return path, True


async def fetch_model(filename: str) -> dict:
    """Unconditionally download a model from the models service (for pre-warming).

    Returns metadata dict with filename, size_bytes, and sha256.
    """
    lock = await _get_lock(filename)
    async with lock:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30)) as client:
            file_sha = await _download_model(client, filename)

        path = _model_path(filename)
        return {
            "filename": filename,
            "size_bytes": os.path.getsize(path),
            "sha256": file_sha,
        }
