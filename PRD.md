# Fleet Ops Pipeline Runtime Service — PRD

> **Repository:** `fleet-ops-pipelines-service`
> **AI server location:** `~/__MS__staging/fleet-ops-pipelines-service/`
> **Port:** 8200
> **Last updated:** 2026-03-20

---

## 1. What This Service Is

The Pipeline Runtime Service is a FastAPI application that **executes ML inference pipelines** against images. It runs on the AI server (`10.125.17.219`) alongside the GPU and model weights, and serves as the inference engine for the Fleet Ops visual pipeline builder.

Its purpose is to **replace** the current hardcoded SeaScanner Detection Service + Alerts Hub with a single, configurable pipeline system. Instead of two bespoke services with logic buried in code, detection-to-alert logic is expressed as a visual DAG of composable nodes — designed in Fleet Ops, executed here.

**This service owns all pipeline data.** Pipeline definitions, version history, test run records, and deployment state are stored in the service's own PostgreSQL database. Fleet Ops is a UI consumer that proxies requests to this service's API.

---

## 2. Company Context

We are a maritime security company. YOLO-based detection models identify threats to clients' vessels and alert them in real time. Images are captured from cameras on vessels, processed through a detection pipeline, and alerts are generated and distributed to operators.

**Fleet Ops** is our internal ML operations platform (React + FastAPI + PostgreSQL on the Training server). It provides an alert analytics dashboard, detection browser, model hub, and visual pipeline builder. This service is the runtime backend for that pipeline builder.

---

## 3. The Problem

The current detection-to-alert pipeline is hardcoded across two separate services:

1. **SeaScanner Detection Service** (AI server) — loads a YOLO model, runs inference on images, returns bounding box detections.
2. **Alerts Hub** (AH server) — applies rule-based logic (confidence thresholds, class filters, priority assignment, deduplication) to decide what becomes an alert.

This architecture has compounding problems:

- **High false positive rate.** Adding post-processing stages (ensemble models, confidence gating, re-checking mid-tier scores) requires code changes and redeployment. So it doesn't happen.
- **Slow iteration.** Testing a new model or changing a threshold requires code changes, redeployment, days of production observation, and rollback if results are worse.
- **No visibility.** There is no way to see what the pipeline does, why a specific image became an alert, or what would happen with different parameters.
- **Rigid architecture.** Future use cases (CV screening, container tampering detection) would each need their own bespoke services.

---

## 4. Architecture

### 4.1 Three-Repo Ecosystem

| Repo | Role | DB | UI |
|------|------|-----|-----|
| `fleet-ops` | UI + orchestration | PostgreSQL (alerts, model metadata) | React dashboard (all pages including Pipeline Editor) |
| `fleet-ops-models-service` | Model file storage + HTTP push deployment | None (filesystem) | None |
| `fleet-ops-pipelines-service` | Pipeline execution + definitions + state | **Own PostgreSQL** (pipelines, versions, test runs, deployments) | **None** |

### 4.2 Data Ownership

| Data | Owner | Why |
|------|-------|-----|
| Alert analytics, sync state | Fleet Ops DB | UI/dashboard concern |
| Model metadata (name, tags, metrics, deploy history) | Fleet Ops DB | Registry/UI concern |
| Model files (.pt) | Models service filesystem | File I/O concern |
| Pipeline definitions, versions, active state | **Pipelines service DB** | Execution + resilience |
| Pipeline test run results | **Pipelines service DB** | Service ran the test |

### 4.3 Key Architectural Decisions

**This service owns its own PostgreSQL database.** Pipeline data (definitions, versions, test runs, deployments) no longer lives in Fleet Ops PostgreSQL. It has been moved here.

Why:
- **Resilience** — if Fleet Ops goes down, production pipelines keep running. The service has its own data.
- **Generic** — in the future, different production flows (maritime detection, CV screening, container inspection) are just different pipelines in this service.
- **Decoupled** — Fleet Ops is one UI consumer. Another UI or CLI could talk to the same API.
- **Data ownership** — the service that executes pipelines should own the pipeline definitions.

PostgreSQL over SQLite because this is a production service handling live detection pipelines. The AI server runs its own PostgreSQL container.

**Fleet Ops owns the UI, this service is backend-only.** The Pipeline Editor UI (React Flow canvas, node palette, node inspector, test results panel) stays in Fleet Ops frontend. This service has no UI.

Why:
- Fleet Ops is the unified ops dashboard — pipeline editor is one tool alongside Model Hub, Detection Browser, etc.
- The UI needs data from multiple services (model dropdown from Model Hub API, alert data, deployment status).
- Micro-frontends add complexity for zero user benefit.
- The API is the boundary, not the UI.

**Fleet Ops backend is a thin proxy.** Fleet Ops backend no longer has pipeline business logic. It proxies all pipeline CRUD and execution requests to this service. Pipeline tables have been removed from Fleet Ops PostgreSQL.

### 4.4 Where This Service Fits

This service sits in a **compatibility sandwich** — it must honor two existing contracts while redesigning everything in between.

```
+-----------+     HTTP      +----------------------------------+    HTTP     +-----------------+
|   CRON    |-------------->|  Pipeline Runtime Service         |----------->| SeaScanner Cloud|
|           |  POST image   |  (AI server, THIS SERVICE)        |  alert JSON|                 |
|           |  (same API    |                                    |  (same API |  Unchanged      |
|           |   contract)   |  Owns PostgreSQL: pipeline defs,   |   contract)|                 |
|           |               |  versions, active state            |            |                 |
|           |               |  Executes active pipeline:         |            |                 |
|           |               |  YOLO -> Gate -> Ensemble -> Alert |            |                 |
+-----------+               +----------------+------------------+            +-----------------+
                                             | HTTP
                            +----------------v------------------+
                            |  Fleet Ops (Training server)       |
                            |  - Pipeline Editor UI (React Flow) |
                            |  - Thin proxy (NO pipeline tables) |
                            |  - Proxies all requests to this    |
                            |    service's API                   |
                            +------------------------------------+
```

**Input contract (Cron -> this service):** Must accept the same HTTP POST that SeaScanner Detection Service accepts today. Cron does not change.

**Output contract (this service -> Cloud):** Must POST alerts in the same JSON format that Alerts Hub sends today. SeaScanner Cloud does not change.

Everything in between is ours to redesign. This is what makes the migration safe.

### 4.5 Communication Flow

```
User designs pipeline in Fleet Ops UI (React Flow editor)
       |
Fleet Ops Backend (thin proxy — no pipeline tables in its DB)
       |
       v
Fleet Ops Pipelines Service (THIS SERVICE)
  - Owns PostgreSQL: pipeline definitions, versions, active state
  - Executes pipelines against images
  - Fully independent at runtime
       |
       v
Production: Cron -> POST /detect -> runs active pipeline -> alerts to Cloud
```

### 4.6 Server Inventory

| Server | IP | Hosts | Role |
|--------|-----|-------|------|
| **AI server** | `10.125.17.219` | This service (+ its PostgreSQL), models service, SeaScanner Detection Service (legacy), GPU | ML inference |
| **Training server** | — | Fleet Ops (dashboard, pipeline editor, backend, PostgreSQL) | UI + orchestration |
| **Cron server** | — | Cron service | Pulls images from vessel cameras, sends to detection pipeline |
| **AH server** | — | Alerts Hub (legacy) | Rule-based post-detection filtering. Will be decommissioned. |
| **Cloud server** | `10.10.9.12` | SeaScanner Cloud | Alert persistence, distribution, SSE stream |

### 4.7 AI Server Filesystem Layout

```
~/__MS__staging/
+-- fleet-ops-models-service/          # Model storage + HTTP push deployment
|   +-- models/                        # .pt files stored here
+-- fleet-ops-pipelines-service/       # THIS SERVICE
|   +-- model_cache/                   # Cached .pt files (pulled from models service on demand)
+-- SeaScannerObjectDetectionService/  # Will be replaced by this service (Phase 3)
+-- video-annotator-service/           # Receives models from models service
```

---

## 5. What Exists Today

### 5.1 Origin

A fully working Phase 1 implementation exists inside the Fleet Ops monorepo at `fleet-ops/pipeline-runtime/`. This service extracts that code into its own repository for independent deployment on the AI server.

### 5.2 Service Code (this repo)

**`main.py`** — FastAPI application with three endpoints:

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| `POST` | `/pipelines/test` | Execute pipeline against test image. Accepts multipart (image file + `pipeline_definition` JSON string as form field). Returns `{ success, execution_time_ms, node_results, final_output }`. | **Working** |
| `GET` | `/models` | List `.pt` files in model directory. Returns `{ models: [{ filename, size_mb, modified }], model_dir }`. | **Working** |
| `GET` | `/health` | Health check. Returns `{ status, device, model_dir, model_dir_exists, loaded_models, loaded_model_count }`. | **Working** |

**`engine.py`** — Core pipeline engine with four components:

- **`PipelineContext`** — Dataclass that carries data through the pipeline. Accumulates results at each stage: `image` -> `image_metadata` -> `detections` -> `scores` -> `stage_results` -> `should_alert` -> `alert_json`. Also carries `gate_decisions` for future routing metadata.
- **`PipelineStage`** (ABC) — Base interface all nodes implement. Two abstract members: `async process(ctx) -> ctx` and `stage_type` property. The engine never changes when new stage types are added (open/closed principle).
- **`StageRegistry`** — Factory pattern. Maps type strings to stage classes. New stages register with one line: `StageRegistry.register("confidence_gate", GateStage)`.
- **`PipelineEngine`** — Builds executable pipeline from JSON definition. Performs topological sort for DAG execution order. Executes nodes sequentially with per-node timing and error handling. Returns full execution trace.

**`config.py`** — Environment-based configuration:

| Variable | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `MODEL_DIR` | `MODEL_DIR` | `../fleet-ops-models-service/models` | (Deprecated) Legacy path to `.pt` model files |
| `MODEL_CACHE_DIR` | `MODEL_CACHE_DIR` | `./model_cache` | Local directory for cached model files pulled from models service |
| `MODELS_SERVICE_URL` | `MODELS_SERVICE_URL` | `http://localhost:8100` | Base URL of the models service for fetching model files |
| `PORT` | `PIPELINE_RUNTIME_PORT` | `8200` | Service port |
| `DEVICE` | `PIPELINE_RUNTIME_DEVICE` | `cpu` | Inference device (`cpu` or `cuda`) |

**`stages/`** — Four implemented stage types:

| Stage | Type String | What It Does |
|-------|-------------|--------------|
| `ImageInputStage` | `image_input` | Reads image dimensions and format, passes image through. Entry point for every pipeline. |
| `YOLODetectorStage` | `yolo_detector` | Loads ultralytics YOLO model via pull-through cache (in-memory → local disk → HTTP fetch from models service), runs inference, returns detections with bounding boxes, class names, and confidence scores. Config: `model_filename`, `confidence_threshold`, `iou_threshold`. |
| `EnsembleStage` | `ensemble` | Runs N models on the same image, groups detections by IoU overlap, aggregates confidence via mean/max/weighted_average. Config: `models[]`, `strategy`, `weights[]`. |
| `JSONOutputStage` | `json_output` | Formats pipeline context into clean output JSON (detection count, detections array, metadata, alert decision). Terminal node. |

### 5.3 Fleet Ops Backend Integration (Training server)

The Fleet Ops backend (`fleet-ops/backend/app/routers/pipelines.py`) currently integrates with this service as follows:

- **Pipeline CRUD** with versioning — `Pipeline`, `PipelineVersion`, `PipelineTestRun` database models currently live in Fleet Ops PostgreSQL. **These will be removed from Fleet Ops and moved to this service's own PostgreSQL** (see Section 11.2).
- **Test proxy** — `POST /api/v1/pipelines/{id}/test` reads pipeline definition from DB, proxies image + definition to `http://10.125.17.219:8200/pipelines/test`, saves test run record. **This will become a passthrough to this service's expanded API.**
- **Runtime proxies** — `/api/v1/pipeline-runtime/models` and `/api/v1/pipeline-runtime/status` proxy to this service's `/models` and `/health`
- **Node type registry** — `GET /api/v1/pipeline-nodes/types` returns config schemas for all 4 implemented node types
- **Version history** — `GET /api/v1/pipelines/{id}/versions` lists all saved versions. **Will proxy to this service.**
- **Test run history** — `GET /api/v1/pipelines/{id}/test-runs` lists past test executions. **Will proxy to this service.**

Config: `ai_server_pipeline_runtime_url = "http://10.125.17.219:8200"`

### 5.4 Fleet Ops Frontend Integration (Training server)

The Fleet Ops React app already has:

- **Pipeline Editor page** (`PipelineEditor.tsx`) — React Flow canvas with drag-and-drop
- **Pipeline List page** (`Pipelines.tsx`) — saved pipeline definitions
- **Node palette sidebar** — drag node types onto canvas
- **Node inspector panel** — configure selected node (model filename, thresholds, etc.)
- **Test results panel** — per-node execution results after test run
- **Toolbar** — save, test, pipeline metadata
- **Full TypeScript types** — `PipelineDefinition`, `PipelineTestResult`, `NodeTypeInfo`, `RuntimeModel`

---

## 6. Pipeline Definition Format

Pipeline definitions are JSON documents stored in **this service's PostgreSQL database** and executed by the pipeline engine. They describe a DAG of nodes and edges.

```json
{
  "nodes": [
    {
      "id": "n1",
      "type": "image_input",
      "position": { "x": 100, "y": 200 },
      "config": {}
    },
    {
      "id": "n2",
      "type": "yolo_detector",
      "position": { "x": 350, "y": 200 },
      "config": {
        "model_filename": "best.pt",
        "confidence_threshold": 0.25,
        "iou_threshold": 0.45
      }
    },
    {
      "id": "n3",
      "type": "json_output",
      "position": { "x": 600, "y": 200 },
      "config": {}
    }
  ],
  "edges": [
    { "source": "n1", "target": "n2" },
    { "source": "n2", "target": "n3" }
  ]
}
```

The `position` field is used by the frontend (React Flow) for layout. The engine ignores it. The `config` field is type-specific and passed directly to the stage constructor.

---

## 7. Core Abstractions

### PipelineContext

The data object that flows through every node. Each stage reads from it and adds to it. Nothing is replaced — results accumulate for full observability.

```python
@dataclass
class PipelineContext:
    image: bytes | None = None
    image_metadata: dict          # vessel_id, channel_id, timestamp, geofence, etc.
    detections: list[dict]        # [{detection_id, bbox, class_name, confidence}]
    scores: list[float]           # confidence values
    stage_results: dict[str, Any] # node_id -> output (for debugging / test mode)
    should_alert: bool = False
    alert_priority: str | None    # critical, high, medium, low
    alert_json: dict | None       # formatted for SeaScanner Cloud
    gate_decisions: dict[str, str] # node_id -> "pass" / "reject"
```

### PipelineStage (ABC)

Every node implements this. The engine processes stages generically — it never needs to know what a specific stage does internally.

```python
class PipelineStage(ABC):
    node_id: str = ""

    @abstractmethod
    async def process(self, ctx: PipelineContext) -> PipelineContext: ...

    @property
    @abstractmethod
    def stage_type(self) -> str: ...
```

### StageRegistry

Factory pattern. Maps type strings to stage classes. Adding a new stage type is one line of registration — no engine changes.

```python
StageRegistry.register("yolo_detector", YOLODetectorStage)
StageRegistry.register("confidence_gate", GateStage)  # Phase 2
```

### PipelineEngine

Builds an executable pipeline from a JSON definition. Topological sort for execution order. Per-node timing and error capture. Returns full execution trace.

---

## 8. Node Types

### Implemented (Phase 1)

| Type | Category | Config Fields | Inputs | Outputs |
|------|----------|---------------|--------|---------|
| `image_input` | Input | (none) | Image bytes from request | Image metadata (width, height, format, size) |
| `yolo_detector` | Model | `model_filename`, `confidence_threshold`, `iou_threshold` | Image | Detections array with bboxes, classes, confidence |
| `ensemble` | Model | `models[]`, `strategy` (mean/max/weighted_average), `weights[]` | Image | Aggregated detections (grouped by IoU, scores combined) |
| `json_output` | Output | (none) | Pipeline context | Formatted JSON output |

### Planned (Phase 2 — Smart Routing & Rules)

| Type | Category | Purpose |
|------|----------|---------|
| `confidence_gate` | Logic | Route detections by score range: high passes through, mid goes to recheck, low is rejected |
| `class_filter` | Logic | Include/exclude detections by class name |
| `priority_assigner` | Logic | Map class + score + geofence combinations to priority levels |
| `dedup_filter` | Logic | Suppress duplicate detections within a time window using IoU |
| `rule_engine` | Logic | Configurable if/else conditions on any context field (time of day, geofence, class, score, count) |

### Planned (Phase 3 — Production Cutover)

| Type | Category | Purpose |
|------|----------|---------|
| `alert_output` | Output | Format alert JSON and POST to SeaScanner Cloud (same contract as Alerts Hub) |

---

## 9. PostgreSQL Database Schema

This service owns its own PostgreSQL instance running on the AI server. The database contains all pipeline-related data. These tables were previously in Fleet Ops PostgreSQL and have been moved here.

### 9.1 Tables

**`pipelines`** — Pipeline definitions

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Pipeline identifier |
| `name` | VARCHAR | Human-readable pipeline name |
| `description` | TEXT | Optional description |
| `definition` | JSONB | Nodes + edges JSON (the DAG) |
| `status` | VARCHAR | `draft`, `active`, `archived` |
| `current_version` | INTEGER | Current version number |
| `flow` | VARCHAR | Production flow identifier (e.g., `maritime_detection`, `cv_screening`) |
| `created_at` | TIMESTAMP | Creation time |
| `updated_at` | TIMESTAMP | Last modification time |

**`pipeline_versions`** — Version history snapshots

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Version record identifier |
| `pipeline_id` | UUID (FK -> pipelines) | Parent pipeline |
| `version` | INTEGER | Version number |
| `definition` | JSONB | Snapshot of nodes + edges at this version |
| `created_at` | TIMESTAMP | When this version was saved |
| `created_by` | VARCHAR | Who saved it (optional) |

**`pipeline_test_runs`** — Test execution records

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Test run identifier |
| `pipeline_id` | UUID (FK -> pipelines) | Pipeline that was tested |
| `pipeline_version` | INTEGER | Version used for the test |
| `image_filename` | VARCHAR | Name of the test image |
| `result` | JSONB | Full execution trace (node_results, final_output) |
| `execution_time_ms` | FLOAT | Total execution time |
| `success` | BOOLEAN | Whether execution completed without error |
| `created_at` | TIMESTAMP | When the test was run |

**`pipeline_deployments`** — Deploy/rollback audit trail

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Deployment record identifier |
| `pipeline_id` | UUID (FK -> pipelines) | Pipeline that was deployed |
| `pipeline_version` | INTEGER | Version that was deployed |
| `flow` | VARCHAR | Production flow this deployment applies to |
| `action` | VARCHAR | `publish`, `rollback` |
| `created_at` | TIMESTAMP | When the deployment occurred |
| `created_by` | VARCHAR | Who triggered it (optional) |

### 9.2 SQLAlchemy Models

These tables will be implemented as SQLAlchemy ORM models in a `models.py` or `db/models.py` file, using the async SQLAlchemy pattern consistent with Fleet Ops backend conventions.

### 9.3 Migration Strategy

Pipeline CRUD currently lives in Fleet Ops backend (`fleet-ops/backend/app/routers/pipelines.py`) with data in Fleet Ops PostgreSQL. The migration:

1. Add PostgreSQL + SQLAlchemy to this service
2. Create the tables in this service's database
3. Add CRUD endpoints to this service's API
4. Migrate existing pipeline data from Fleet Ops DB to this service's DB
5. Update Fleet Ops backend to proxy all pipeline operations to this service
6. Remove pipeline tables (`Pipeline`, `PipelineVersion`, `PipelineTestRun`) from Fleet Ops PostgreSQL
7. Remove pipeline business logic from Fleet Ops backend (keep only proxy routes)

---

## 10. API Contract

This service exposes the full pipeline API. Fleet Ops backend proxies ALL of these endpoints — Fleet Ops backend no longer has pipeline business logic, it is a passthrough.

### 10.1 Pipeline CRUD

**`GET /pipelines`** — List all pipelines

- **Response:**
```json
{
  "pipelines": [
    {
      "id": "uuid",
      "name": "Maritime Detection v3",
      "status": "active",
      "current_version": 5,
      "flow": "maritime_detection",
      "updated_at": "2026-03-20T10:00:00Z"
    }
  ]
}
```

**`POST /pipelines`** — Create new pipeline

- **Body:**
```json
{
  "name": "Maritime Detection v4",
  "description": "New pipeline with ensemble stage",
  "definition": { "nodes": [...], "edges": [...] }
}
```
- **Response:** Created pipeline object with `id`, `version: 1`, `status: "draft"`

**`GET /pipelines/{id}`** — Get pipeline with full definition

- **Response:** Full pipeline object including `definition` (nodes + edges JSON)

**`PUT /pipelines/{id}`** — Update pipeline (bumps version)

- **Body:** `{ "name": "...", "definition": { ... } }`
- **Response:** Updated pipeline object with incremented `current_version`. A new `pipeline_versions` snapshot is created automatically.

**`DELETE /pipelines/{id}`** — Archive pipeline

- Sets `status` to `"archived"`. Does not hard-delete.

**`GET /pipelines/{id}/versions`** — Version history

- **Response:** List of version snapshots with `version`, `definition`, `created_at`

### 10.2 Execution

**`POST /pipelines/{id}/test`**

Execute a saved pipeline against a test image. The service loads the pipeline definition from its own database.

- **Content-Type:** `multipart/form-data`
- **Fields:**
  - `image` — image file (JPEG, PNG)
- **Response:**
```json
{
  "success": true,
  "execution_time_ms": 127,
  "node_results": {
    "n1": { "type": "image_input", "width": 640, "height": 480, "execution_time_ms": 2 },
    "n2": { "type": "yolo_detector", "model": "best.pt", "detection_count": 3, "detections": [...], "execution_time_ms": 45 },
    "n3": { "type": "json_output", "output": {...}, "execution_time_ms": 0 }
  },
  "final_output": { "type": "json_output", "output": {...} }
}
```

A `pipeline_test_runs` record is created automatically.

**`POST /pipelines/{id}/publish`** — Set as active for a production flow

- **Body:** `{ "flow": "maritime_detection" }`
- Marks this pipeline version as active for the specified flow. Creates a `pipeline_deployments` record.

**`GET /pipelines/active/{flow}`** — Get active pipeline for a flow

- Returns the currently active pipeline definition for a production flow (e.g., `maritime_detection`).

### 10.3 Production (Phase 3)

**`POST /detect`** — Production endpoint

Same API contract as current SeaScanner Detection Service. Cron calls this. Runs the active pipeline for the appropriate flow and sends alerts to Cloud.

### 10.4 Models + Health

**`GET /models`**

List cached `.pt` model files on this server (pulled from models service on demand).

- **Response:**
```json
{
  "models": [
    { "filename": "best.pt", "size_mb": 12.34, "modified": 1710500000.0, "sha256": "abc123..." }
  ],
  "model_cache_dir": "./model_cache"
}
```

**`POST /models/fetch`**

Pre-warm: fetch a model from the models service into the local cache before a pipeline needs it.

- **Query params:** `filename` (required) — the model filename to fetch
- **Response:**
```json
{
  "filename": "best.pt",
  "size_bytes": 12935168,
  "sha256": "abc123..."
}
```
- **Errors:** 404 if model not found on models service, 502 if models service unreachable

**`GET /health`**

Health check with device, model cache, and database status.

- **Response:**
```json
{
  "status": "ok",
  "device": "cuda",
  "models_service_url": "http://localhost:8100",
  "model_cache_dir": "./model_cache",
  "loaded_models": ["best.pt"],
  "loaded_model_count": 1,
  "database": "connected"
}
```

### 10.5 Legacy Test Endpoint (backward compatibility)

**`POST /pipelines/test`** (without pipeline ID)

Execute an ad-hoc pipeline definition against a test image. Accepts multipart (image file + `pipeline_definition` JSON string as form field). This is the original Phase 1 endpoint and remains for backward compatibility.

- **Content-Type:** `multipart/form-data`
- **Fields:**
  - `image` — image file (JPEG, PNG)
  - `pipeline_definition` — JSON string of the pipeline definition
- **Response:** Same trace format as `POST /pipelines/{id}/test`

---

## 11. Implementation Phases

### Phase 1 — "It Works on One Image" (Complete)

Linear pipeline execution, test via Fleet Ops UI, see results at every stage.

**What was built:**
- FastAPI service with `/pipelines/test`, `/models`, `/health`
- Pipeline engine with topological sort, per-node timing, error handling
- 4 stage types: image_input, yolo_detector, ensemble, json_output
- Model caching (module-level dict, load once per model file)
- Fleet Ops backend CRUD + proxy integration
- Fleet Ops frontend pipeline editor with React Flow

**What was NOT built:**
- No production traffic (Cron still calls old SeaScanner Detection Service)
- No conditional logic (gates, routers, filters)
- No pipeline publishing or syncing
- No batch testing
- No own database (pipeline data lived in Fleet Ops PostgreSQL)

### Phase 2 — "Smart Routing & Rules"

DAG execution with conditional branches. Logic nodes replace Alerts Hub rule logic. Batch testing for validation.

**Scope for this service:**
- Conditional edge support — nodes can have `pass` and `reject` output branches
- 5 new stage types: confidence_gate, class_filter, priority_assigner, dedup_filter, rule_engine
- Full DAG execution with conditional routing (not just linear chains)
- Execution tracing that records which branch was taken at each gate

**Scope for Fleet Ops (separate repo):**
- 5 new node type UIs with config panels
- Conditional edge rendering (labeled pass/reject)
- Rule builder UI
- Batch test: run pipeline on N images, show aggregate results
- Pipeline comparison: same images through two pipeline versions side by side

### Phase 3 — "Production Cutover"

This service replaces SeaScanner Detection Service + Alerts Hub in production.

**Scope for this service:**
- `POST /detect` — same API contract as current SeaScanner Detection Service (Cron calls this)
- `alert_output` stage — formats and POSTs alerts to SeaScanner Cloud receiver
- Active pipeline caching (serve from in-memory cache, reload on publish)
- Hot-reload — when a new pipeline is published, reload without downtime
- Error handling — if pipeline fails, log error, don't crash, optionally fall back to previous version
- Execution metrics — latency per node, total pipeline time, alert rate, error rate

**Infrastructure changes:**
- Cron repoints to this service (same URL if possible, otherwise config change)
- Alerts Hub decommissioned
- Shadow mode available: run both old and new pipelines, compare outputs before full cutover

---

## 12. What Needs to Happen Next

### 12.1 Repo Setup (immediate)

1. Move code from `fleet-ops/pipeline-runtime/` to this repo
2. Fix imports — relative `..config` becomes absolute `config` (stages are no longer a sub-package of a larger repo)
3. Update `MODEL_DIR` default in `config.py` to `../fleet-ops-models-service/models` (sibling directory on AI server)
4. Add `deploy.sh` with three modes: `test` (run locally), `--install` (systemd setup), `--update` (pull + restart)
5. Add systemd service file (`fleet-ops-pipelines.service`)
6. Add `.gitignore`, `requirements.txt`, `README.md`

### 12.2 Add PostgreSQL + Data Ownership (new — critical path)

1. **Add PostgreSQL container** — Docker Compose or standalone PostgreSQL on the AI server for this service
2. **Add SQLAlchemy models** — `Pipeline`, `PipelineVersion`, `PipelineTestRun`, `PipelineDeployment` ORM models (see Section 9)
3. **Add database config** — `DATABASE_URL` env var in `config.py` (e.g., `postgresql+asyncpg://user:pass@localhost:5432/pipelines`)
4. **Add Alembic** — database migrations for schema management
5. **Add CRUD endpoints** — `GET/POST/PUT/DELETE /pipelines`, `GET /pipelines/{id}/versions`, `POST /pipelines/{id}/test` (with DB lookup), `POST /pipelines/{id}/publish`, `GET /pipelines/active/{flow}`
6. **Migrate existing data** — one-time script to copy pipeline data from Fleet Ops PostgreSQL to this service's database
7. **Update Fleet Ops backend** — convert pipeline router from business logic to thin proxy (all requests forwarded to this service)
8. **Remove pipeline tables from Fleet Ops DB** — drop `Pipeline`, `PipelineVersion`, `PipelineTestRun` models and tables from Fleet Ops backend

### 12.3 Phase 2 Implementation

Add logic stages. Each stage follows the same pattern: implement `PipelineStage`, register in `StageRegistry`, add config schema to Fleet Ops backend node type registry.

### 12.4 Phase 3 Implementation

Add production endpoints, alert output stage, pipeline caching, and hot-reload. Coordinate with Cron server team for cutover.

---

## 13. Design Decisions

| Decision | Rationale |
|----------|-----------|
| **This service owns its own PostgreSQL** | Resilience (runs independently if Fleet Ops is down), data ownership (service that executes pipelines owns definitions), decoupling (multiple UIs/CLIs can consume the API), future-proof (different flows are just different pipelines). |
| **Fleet Ops UI stays in Fleet Ops, not here** | Fleet Ops is the unified ops dashboard. UI needs data from multiple services. Micro-frontends add complexity. API is the boundary. |
| **Fleet Ops backend becomes thin proxy** | Single source of truth for pipeline data lives here. No dual-write, no sync issues, no stale cache. |
| **PostgreSQL over SQLite** | Production service handling live detection pipelines. Concurrent access from API requests. Needs real ACID, connection pooling, and production-grade durability. |
| **React Flow for editor** (not n8n) | Full control, matches Fleet Ops theme, n8n is workflow automation not ML inference. n8n itself uses React Flow under the hood. |
| **Generic pipeline engine** | Supports future use cases (CV screening, container tampering) without building new services. Open/closed principle. |
| **Config ownership in pipeline nodes** | Same model can run with different inference params in different pipeline stages. Model Hub stores files + advisory `recommended_config` only. |
| **Compatibility sandwich** | Never change Cron input or Cloud output contracts. Replace only the middle. This makes migration safe and reversible. |
| **Separate repo** | Deployed on AI server with different lifecycle, dependencies, and hardware than Fleet Ops. Needs ultralytics + PyTorch + CUDA. |
| **Pull-through model cache** | Models are fetched from the models service over HTTP on demand and cached locally (in-memory + disk). Avoids filesystem coupling, survives containerization and scaling. UI-driven model selection — the service only fetches models referenced by pipeline definitions. |
| **Module-level model cache** | YOLO models are expensive to load (seconds). Cache in process memory, reload only when model file changes. |
| **Topological sort execution** | Supports DAG structure needed for Phase 2 conditional routing. Linear chains are a special case of DAG. |

---

## 14. Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Web framework | FastAPI | Async-native, matches Fleet Ops stack, good for ML workloads |
| Server | uvicorn | Standard ASGI server for FastAPI |
| Database | PostgreSQL | Production-grade, ACID, JSONB for pipeline definitions, concurrent access |
| ORM | SQLAlchemy (async) | Consistent with Fleet Ops backend, async support via asyncpg |
| Migrations | Alembic | Schema versioning and migration management |
| Model inference | ultralytics (YOLO) | Current model format, direct access to PyTorch |
| Image processing | Pillow (PIL) | Image decoding, dimension reading |
| HTTP client | httpx | Model fetching from models service (pull-through cache), future alert output to SeaScanner Cloud (Phase 3) |
| Form parsing | python-multipart | Required by FastAPI for file upload endpoints |
| Runtime | Python 3.11+ | Match type union syntax (`str | None`), Fleet Ops standard |
| GPU | CUDA (via PyTorch) | Available on AI server, explicitly configured via `PIPELINE_RUNTIME_DEVICE` env var |

---

## 15. Alerts Hub Rule Mapping

For Phase 2-3, every current Alerts Hub rule must be representable as a pipeline node. This table maps current rules to planned node types:

| Current Alerts Hub Rule | Pipeline Node Equivalent |
|-------------------------|--------------------------|
| Ignore detections below confidence X | `confidence_gate` |
| Only alert on specific classes in specific geofences | `class_filter` + `rule_engine` |
| Assign priority based on class + score + geofence | `priority_assigner` |
| Suppress duplicates within N minutes | `dedup_filter` |
| Format and send alert to SeaScanner Cloud | `alert_output` |

---

## 16. Example Pipeline — Current Maritime Detection (Target State)

This shows how the full SeaScanner Detection Service + Alerts Hub logic would look as a single pipeline after Phase 3:

```
[Image Input]
     |
     v
[YOLO Detector]
  model: yolov8n-maritime-v3.4.pt
  confidence_threshold: 0.25
     |
     v
[Confidence Gate]
  high: > 0.8 -----> [pass through]
  mid: 0.4-0.8 ----> [Ensemble] -> [merge]
  low: < 0.4 ------> [reject / drop]
     |
     v
[Class Filter]
  include: [boat, person, vessel]
     |
     v
[Priority Assigner]
  person + score > 0.8 -> critical
  person + score > 0.6 -> high
  boat + score > 0.7   -> high
  default              -> medium
     |
     v
[Dedup Filter]
  window: 5 minutes
  iou_threshold: 0.5
     |
     v
[Alert Output]
  target: SeaScanner Cloud (10.10.9.12:7000)
  format: alerts_hub_v1
```

---

## 17. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Pipeline engine adds latency vs. hardcoded service | Slower detection, delayed alerts | Benchmark in Phase 1. Pre-load models. Profile DAG overhead (topological sort is O(V+E), negligible). |
| GPU memory exhaustion from too many cached models | Service crashes | Pre-load only active pipeline's models. Evict models on pipeline switch. Monitor memory. |
| Production cutover breaks alert flow | Clients don't receive alerts | Shadow mode: run both pipelines, compare outputs. Gradual cutover vessel by vessel. |
| Corrupted pipeline definition in production | Bad alerts or no alerts | Version history + one-click rollback. Validate definition before accepting publish. Require successful test run before publish. |
| Network partition between Training and AI servers | Can't publish new pipelines | Pipeline def stored in this service's own DB. Production continues with active pipeline indefinitely. No dependency on Fleet Ops at runtime. |
| PostgreSQL on AI server adds operational overhead | Another database to manage | Docker Compose for easy setup. Automated backups. Small dataset (pipeline defs are tiny JSON docs). |
| Data migration from Fleet Ops DB | Downtime, data loss | One-time migration script with validation. Run in maintenance window. Keep Fleet Ops tables as read-only backup until verified. |

---

## 18. Open Questions

| # | Question | Status |
|---|----------|--------|
| 1 | Exact API contract of current SeaScanner Detection Service `/detect` endpoint (request/response format) | Needs investigation before Phase 3 |
| 2 | Exact alert JSON format that Alerts Hub sends to SeaScanner Cloud | Needs investigation before Phase 3 |
| 3 | AI server GPU specs and available VRAM | Affects model loading strategy (preload all vs. load on demand) |
| 4 | Can this service reuse the same port/URL as current Detection Service? | If yes, Cron needs zero config change at cutover |
| 5 | Full inventory of Alerts Hub rules | All rules must be expressible as pipeline nodes before Phase 3 |
| 6 | Model preloading strategy | Load all on startup (fast inference, high memory) vs. load on demand (slower first run, lower memory) |
| 7 | PostgreSQL container setup on AI server | Docker Compose vs. standalone install. Resource allocation. Backup strategy. |

---

## 19. Related Resources

| Resource | Location |
|----------|----------|
| Full Pipeline Builder PRD (all components) | `fleet-ops/docs/prd-pipeline-builder.md` |
| Original runtime implementation | `fleet-ops/pipeline-runtime/` |
| Fleet Ops backend pipeline router | `fleet-ops/backend/app/routers/pipelines.py` |
| Fleet Ops frontend pipeline editor | `fleet-ops/frontend/src/pages/PipelineEditor.tsx` |
| Fleet Ops frontend pipeline list | `fleet-ops/frontend/src/pages/Pipelines.tsx` |
| Pull-Through Model Cache PRD | `docs/prd-pull-through-model-cache.md` |
| Model Hub PRD | `fleet-ops/docs/prd-model-hub.md` |
| Models service repo | `fleet-ops-models-service/` (sibling on AI server) |
| GitHub | `https://github.com/daniel-friedman-ms/fleet-ops-pipelines-service` |
