# Feature PRD: Pull-Through Model Cache

> **Status:** Planned
> **Author:** Daniel Friedman
> **Date:** 2026-03-20
> **Related:** [PRD.md](../PRD.md) (main service PRD)

---

## 1. Problem

The pipelines service reads `.pt` model files directly from the models service's filesystem (`../fleet-ops-models-service/models`). This is a hard filesystem coupling that:

- **Breaks with containerization** — services in separate containers can't share a filesystem path
- **Breaks with horizontal scaling** — multiple pipelines service instances can't all mount the same directory
- **Breaks with separate hosts** — the two services must run on the same machine
- **Has no atomicity** — a model file could be read while the models service is writing it
- **Has no versioning** — no way to detect if a cached model is stale

---

## 2. Solution

Replace direct filesystem access with an **HTTP pull-through cache**. The pipelines service fetches models from the models service over HTTP on demand, caching them locally on disk.

### Resolution Chain

```
Pipeline references "yolov8n.pt"
        |
        v
[1] In-memory cache (_model_cache)  →  hit? return loaded model
        |
        v
[2] Local disk cache (MODEL_CACHE_DIR/yolov8n.pt)  →  exists + SHA matches? load from disk
        |
        v
[3] HTTP fetch from models service (GET /models/yolov8n.pt/download)  →  download, cache, load
```

### Model Discovery Is UI-Driven

The pipelines service does **not** auto-discover or sync all models. It only fetches models that are explicitly referenced in pipeline definitions. The flow:

1. **Models service** stores model files (source of truth)
2. **Fleet Ops UI** shows available models (queries models service catalog)
3. **Operator** configures a pipeline node to use a specific model
4. **Pipeline definition** saved to pipelines service with `"model_filename": "yolov8n.pt"`
5. **Pipelines service** fetches the model on first use, caches locally

This means the pipelines service only ever has models it actually needs.

---

## 3. Dependencies

### Models Service Endpoints (must be built first)

| Endpoint | Purpose |
|----------|---------|
| `GET /models/{filename}/download` | Stream `.pt` file as binary response |
| `GET /models/{filename}/info` | Return `{ sha256, size_bytes, modified }` for staleness checks |

A separate PRD prompt has been provided for the models service team.

---

## 4. Changes to This Service

### New Configuration (`config.py`)

| Variable | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `MODELS_SERVICE_URL` | `MODELS_SERVICE_URL` | `http://localhost:8100` | Base URL of the models service |
| `MODEL_CACHE_DIR` | `MODEL_CACHE_DIR` | `./model_cache` | Local directory for cached model files |

### New Module: `model_resolver.py`

Core pull-through cache logic with two public functions:

- **`ensure_model_on_disk(filename)`** — resolve a model filename to a local path, fetching if needed
- **`fetch_model(filename)`** — unconditionally download a model (for pre-warming)

### New Endpoint

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/models/fetch?filename=X` | Pre-warm: fetch a model into local cache before a pipeline needs it |

### Modified Endpoints

| Endpoint | Change |
|----------|--------|
| `GET /models` | Lists from `MODEL_CACHE_DIR` instead of `MODEL_DIR`. Filters out `.sha256` sidecar files. |
| `GET /health` | Reports `models_service_url` and `model_cache_dir` instead of `model_dir`. |

### Modified Stages

| Stage | Change |
|-------|--------|
| `yolo_detector.py` | `_get_model` becomes async `get_model`, delegates to `model_resolver` |
| `ensemble.py` | Updates import and adds `await` |

---

## 5. Design Decisions

### Pull (on-demand) over Push (models service pushes to us)

Push requires the models service to know about every consumer instance, handle retries, and implement service discovery. Pull-through cache gives the same resilience with far less complexity. Each pipelines service instance manages its own cache independently.

### No shared object storage (S3/MinIO)

Adding S3 infrastructure solely to share files between two services on the same network is over-engineering for an internal tool. The HTTP pull pattern can be swapped for S3 later by changing one function in `model_resolver.py`.

### Graceful degradation

If the models service is unreachable but a model is already cached on disk, the pipelines service logs a warning and uses the cached copy. The runtime dependency only matters for fetching models that have never been cached.

### Atomic writes

Downloads write to a temp file (`{filename}.tmp.{uuid}`), then `os.replace()` to the final path. This prevents loading a partially-downloaded model.

### SHA-256 staleness detection

Each cached model has a `.sha256` sidecar file. Before using a cached model, the resolver checks the models service's `/info` endpoint. If the hash matches, no re-download. If it differs, the model is re-downloaded. If the models service is unreachable, the cached copy is used as-is.

### Per-filename locking

An `asyncio.Lock` per filename prevents concurrent downloads of the same model (e.g., if an ensemble stage triggers multiple parallel loads).

---

## 6. What This Does NOT Include

- **Model auto-discovery or sync** — the service doesn't poll for new models
- **Cache eviction policy** — models stay cached until manually deleted or replaced by a newer version
- **Webhook/push notifications** for cache invalidation — polling via `/info` is sufficient
- **Model registry (MLflow, etc.)** — the models service already fills this role

---

## 7. Verification

1. `POST /models/fetch?filename=yolov8n.pt` — model appears in `model_cache/` with `.sha256` sidecar
2. `GET /models` — lists cached models, no `.sha256` or `.tmp` files shown
3. `GET /health` — shows `models_service_url` and `model_cache_dir`
4. Run a pipeline referencing the model — loads from cache, runs inference
5. Restart service — model loads from disk cache without hitting models service
6. Stop models service — graceful degradation, cached models still work, clear warning logged
