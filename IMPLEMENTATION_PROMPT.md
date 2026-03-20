# Implementation Prompt — PipelinesService

## Context

You are implementing `PipelinesService`, the Pipeline Runtime Service that executes ML inference pipelines on an AI server (`10.125.17.219:8200`).

**Read `PRD.md` in this repo first** — it has full context on what this service is, the architecture, core abstractions, API contract, and database schema.

**Cross-reference:** This service is part of a three-repo ecosystem:
- `FleetOps` — UI + orchestration. See `docs/prd-pipeline-builder.md` for the full Pipeline Builder PRD. The FleetOps backend (`backend/app/routers/pipelines.py`) currently has pipeline CRUD + DB tables that need to be **converted to thin proxies** pointing to THIS service's API.
- `ModelsHubService` — model file storage on the same AI server. Models are at `../ModelsHubService/models/` (sibling directory).
- `PipelinesService` (THIS REPO) — pipeline execution, definitions, and state. **Owns its own PostgreSQL database.**

---

## What We Have (Current State)

### Working code in `FleetOps/pipeline-runtime/` (to be migrated here)

A fully working Pipeline Runtime exists inside the FleetOps repo. These files need to be moved to this repo with import fixes:

**`pipeline-runtime/main.py`** — FastAPI app:
- `POST /pipelines/test` — receives image (multipart) + pipeline_definition (JSON form field), executes pipeline, returns `{ success, execution_time_ms, node_results, final_output }`
- `GET /models` — lists `.pt` files with `{ filename, size_mb, modified }`
- `GET /health` — status, device, loaded models

**`pipeline-runtime/engine.py`** — Core pipeline engine:
- `PipelineContext` dataclass — carries image, detections, scores, stage_results through pipeline
- `PipelineStage` ABC — base interface with `process()` and `stage_type`
- `StageRegistry` — factory pattern, maps type strings to stage classes
- `PipelineEngine` — builds from JSON definition, topological sort, DAG execution with per-node timing

**`pipeline-runtime/config.py`** — `MODEL_DIR`, `PORT=8200`, `DEVICE` (cpu/cuda)

**`pipeline-runtime/stages/`** — 4 stage types:
- `__init__.py` — registers all stages
- `image_input.py` — reads image dimensions
- `yolo_detector.py` — loads YOLO model (with caching), runs inference, returns detections
- `ensemble.py` — multi-model fan-out, IoU-based grouping, mean/max/weighted_average aggregation
- `json_output.py` — formats final output

**`pipeline-runtime/requirements.txt`** — fastapi, uvicorn, python-multipart, ultralytics, Pillow

### FleetOps backend already has pipeline CRUD (will become proxy)

`FleetOps/backend/app/routers/pipelines.py` currently:
- Has full CRUD endpoints (create, read, update, delete, list pipelines)
- Stores pipeline data in FleetOps PostgreSQL (Pipeline, PipelineVersion, PipelineTestRun, PipelineDeployment models)
- Proxies test requests to `http://10.125.17.219:8200/pipelines/test`
- Has a node type registry (image_input, yolo_detector, ensemble, json_output)

**This CRUD logic and DB ownership must move to THIS service.** FleetOps backend will be converted to a thin proxy (see the FleetOps implementation prompt).

### This repo is currently empty (just .git)

---

## What We Want (Target State)

A standalone service that:
1. **Owns all pipeline data** in its own PostgreSQL (definitions, versions, test runs, active state)
2. **Provides full CRUD API** for pipelines (create, read, update, delete, versions)
3. **Executes pipelines** against images (test mode now, production `/detect` in Phase 3)
4. **Is fully independent** — FleetOps going down does NOT affect production pipeline execution
5. **Runs as a systemd service** on the AI server with `deploy.sh`

---

## How To Implement

### Step 1: Migrate core pipeline engine from FleetOps/pipeline-runtime/

Copy these files from `FleetOps/pipeline-runtime/` into this repo, fixing imports:

**`config.py`** — Update:
```python
import os

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "ModelsHubService", "models"))
PORT = int(os.environ.get("PIPELINE_RUNTIME_PORT", "8200"))
DEVICE = os.environ.get("PIPELINE_RUNTIME_DEVICE", "cpu")
DATABASE_URL = os.environ.get("PIPELINE_DB_URL", "postgresql+asyncpg://pipeline:pipeline@localhost:5432/pipelines")
```

**`engine.py`** — Copy as-is. The imports are already `from config import ...` (no relative imports needed at top level). Actually check: the original uses `from ..config` in stages, but `engine.py` itself only uses stdlib. Copy and verify.

**`stages/__init__.py`** — Fix imports. Original uses `from ..engine import StageRegistry`. Change to `from engine import StageRegistry`. Same for stage file imports.

**`stages/image_input.py`** — Fix `from ..engine import ...` → `from engine import ...`

**`stages/yolo_detector.py`** — Fix imports:
- `from ..config import MODEL_DIR, DEVICE` → `from config import MODEL_DIR, DEVICE`
- `from ..engine import PipelineStage, PipelineContext` → `from engine import PipelineStage, PipelineContext`

**`stages/ensemble.py`** — Fix imports similarly. Also imports `from .yolo_detector import _get_model` — this stays as relative within stages package.

**`stages/json_output.py`** — Fix imports similarly.

### Step 2: Add PostgreSQL database

**`database.py`** — New file. Async SQLAlchemy + asyncpg setup:
```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_session():
    async with async_session() as session:
        yield session

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

**`models.py`** — New file. SQLAlchemy models (migrated from FleetOps):
```python
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, DateTime, JSON, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base

class Pipeline(Base):
    __tablename__ = "pipelines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    __table_args__ = (UniqueConstraint("pipeline_id", "version", name="uq_pipeline_version"),)

class PipelineTestRun(Base):
    __tablename__ = "pipeline_test_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer)
    image_source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result: Mapped[dict] = mapped_column(JSON)
    execution_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PipelineDeployment(Base):
    __tablename__ = "pipeline_deployments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    deployed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    deployed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    previous_pipeline_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="SET NULL"), nullable=True)
    previous_pipeline_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
```

### Step 3: Add CRUD endpoints to main.py

Expand `main.py` to include pipeline CRUD. Reference `FleetOps/backend/app/routers/pipelines.py` for the exact patterns — the CRUD logic there is what moves here. The endpoints this service must expose:

```
GET    /pipelines                    — list all pipelines (optional ?status= filter)
POST   /pipelines                   — create new pipeline (name, description, definition, created_by)
GET    /pipelines/{id}              — get pipeline with full definition
PUT    /pipelines/{id}              — update pipeline (bumps version, saves version snapshot)
DELETE /pipelines/{id}              — archive pipeline (soft delete, set status="archived")
GET    /pipelines/{id}/versions     — list version history

POST   /pipelines/{id}/test         — test pipeline with image (EXISTING — already works)
GET    /pipelines/{id}/test-runs    — list test run history

GET    /pipeline-nodes/types        — return node type registry (MOVE from FleetOps)

GET    /models                      — list .pt files (EXISTING — already works)
GET    /health                      — health check (EXISTING — already works)
```

The node type registry (the `NODE_TYPES` list with config schemas for image_input, yolo_detector, ensemble, json_output) should move from FleetOps backend to here. See `FleetOps/backend/app/routers/pipelines.py` lines 74-149 for the exact data.

The `POST /pipelines/{id}/test` endpoint should now also save a `PipelineTestRun` record to the local DB (currently FleetOps does this).

### Step 4: Add deploy infrastructure

**`deploy.sh`** — Same 3-mode pattern as ModelsHubService:
```bash
SERVICE_NAME="PipelinesService"
```
Modes: `bash deploy.sh` (test), `sudo bash deploy.sh --install` (systemd), `sudo bash deploy.sh --update` (git pull + restart). Include venv setup and pip install.

**`PipelinesService.service`** — systemd unit template:
```ini
[Unit]
Description=FleetOps Pipelines Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/placeholder
ExecStart=/placeholder
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Step 5: Add supporting files

**`.gitignore`**:
```
venv/
__pycache__/
*.pyc
*.pt
```

**`requirements.txt`** — expanded from original:
```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
python-multipart>=0.0.9
ultralytics>=8.1.0
Pillow>=10.0.0
httpx>=0.27.0
sqlalchemy[asyncio]>=2.0.0
asyncpg>=0.29.0
```

**`README.md`** — Service overview covering:
- What it is and how it fits (reference PRD.md)
- Setup: clone, deploy.sh, PostgreSQL setup
- API reference (all endpoints)
- Configuration (env vars: MODEL_DIR, PORT, DEVICE, PIPELINE_DB_URL)
- systemd management commands

### Step 6: Wire up DB initialization in main.py

Add a startup event to initialize the database:
```python
@app.on_event("startup")
async def startup():
    from database import init_db
    await init_db()
```

---

## File Structure (Final)

```
PipelinesService/
├── main.py                              # FastAPI app — CRUD + test + models + health
├── config.py                            # Env-based config (MODEL_DIR, PORT, DEVICE, DATABASE_URL)
├── database.py                          # Async SQLAlchemy engine + session + Base
├── models.py                            # SQLAlchemy models (Pipeline, PipelineVersion, etc.)
├── engine.py                            # PipelineContext, PipelineStage, StageRegistry, PipelineEngine
├── stages/
│   ├── __init__.py                      # Registers all stage types
│   ├── image_input.py                   # ImageInputStage
│   ├── yolo_detector.py                 # YOLODetectorStage (with model caching)
│   ├── ensemble.py                      # EnsembleStage (IoU grouping, 3 aggregation strategies)
│   └── json_output.py                   # JSONOutputStage
├── requirements.txt                     # All dependencies including SQLAlchemy + asyncpg
├── deploy.sh                            # 3-mode deploy (test / --install / --update)
├── PipelinesService.service  # systemd unit template
├── .gitignore
├── README.md
└── PRD.md                               # Already exists
```

---

## Verification

After implementing:
1. `python -c "from engine import PipelineEngine; print('OK')"` — imports work
2. `python -c "from models import Pipeline; print('OK')"` — DB models importable
3. `python -c "from database import Base; print(Base.metadata.tables.keys())"` — tables registered
4. Start service: `python main.py` — should start on port 8200
5. `curl http://localhost:8200/health` — returns status
6. `curl http://localhost:8200/pipeline-nodes/types` — returns node type registry
7. `curl http://localhost:8200/pipelines` — returns `{ count: 0, items: [] }`
8. `grep -r "from \.\." stages/` — should return nothing (no relative parent imports)
