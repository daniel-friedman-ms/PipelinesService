"""Fleet Ops Pipelines Service — pipeline CRUD, execution, and deployment."""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from config import MODEL_CACHE_DIR, MODELS_SERVICE_URL, PORT, DEVICE
from database import get_session, init_db
from engine import PipelineEngine
from models import Pipeline, PipelineVersion, PipelineTestRun, PipelineDeployment

# Register all stage types on import
import stages  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ── Lifespan ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(MODEL_CACHE_DIR, exist_ok=True)
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Fleet Ops Pipelines Service", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Schemas ────────────────────────────────────────────────────────


class PipelineCreate(BaseModel):
    name: str
    description: Optional[str] = None
    definition: dict  # {nodes: [...], edges: [...]}
    flow: Optional[str] = None
    created_by: Optional[str] = None


class PipelineUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: dict  # {nodes: [...], edges: [...]}
    flow: Optional[str] = None
    created_by: Optional[str] = None


class PublishRequest(BaseModel):
    flow: str
    deployed_by: Optional[str] = None
    notes: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _pipeline_to_dict(p: Pipeline, include_definition: bool = True) -> dict:
    d = {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "current_version": p.current_version,
        "status": p.status,
        "flow": p.flow,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "created_by": p.created_by,
    }
    if include_definition:
        d["definition"] = p.definition
    return d


def _test_run_to_dict(t: PipelineTestRun) -> dict:
    return {
        "id": t.id,
        "pipeline_id": t.pipeline_id,
        "pipeline_version": t.pipeline_version,
        "image_filename": t.image_filename,
        "result": t.result,
        "execution_time_ms": t.execution_time_ms,
        "success": t.success,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# ── Node Type Registry ─────────────────────────────────────────────────────

NODE_TYPES = [
    {
        "type": "image_input",
        "label": "Image Input",
        "category": "input",
        "config_schema": {},
        "inputs": [],
        "outputs": ["image"],
    },
    {
        "type": "yolo_detector",
        "label": "YOLO Detector",
        "category": "model",
        "config_schema": {
            "model_filename": {
                "type": "string",
                "label": "Model Filename",
                "placeholder": "e.g. yolov8n.pt",
                "required": True,
            },
            "confidence_threshold": {
                "type": "number",
                "label": "Confidence Threshold",
                "default": 0.25,
                "min": 0,
                "max": 1,
                "step": 0.05,
            },
            "iou_threshold": {
                "type": "number",
                "label": "IoU Threshold",
                "default": 0.45,
                "min": 0,
                "max": 1,
                "step": 0.05,
            },
        },
        "inputs": ["image"],
        "outputs": ["detections"],
    },
    {
        "type": "ensemble",
        "label": "Ensemble",
        "category": "model",
        "config_schema": {
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "label": "Model Filenames",
                "default": [],
            },
            "strategy": {
                "type": "enum",
                "label": "Aggregation Strategy",
                "options": ["mean", "max", "weighted_average"],
                "default": "mean",
            },
            "weights": {
                "type": "array",
                "items": {"type": "number"},
                "label": "Weights (for weighted_average)",
                "default": [],
            },
        },
        "inputs": ["image"],
        "outputs": ["detections"],
    },
    {
        "type": "json_output",
        "label": "JSON Output",
        "category": "output",
        "config_schema": {},
        "inputs": ["detections"],
        "outputs": [],
    },
]


# ── Node Type Registry Endpoint ─────────────────────────────────────────────


@app.get("/pipeline-nodes/types")
async def get_node_types():
    """Return available node types with their config schemas."""
    return {"types": NODE_TYPES}


# ── Models + Health ─────────────────────────────────────────────────────────


@app.get("/models")
async def list_models():
    """List cached model files on this server."""
    if not os.path.isdir(MODEL_CACHE_DIR):
        return {"models": [], "model_cache_dir": MODEL_CACHE_DIR, "error": f"Directory not found: {MODEL_CACHE_DIR}"}

    models = []
    for filename in sorted(os.listdir(MODEL_CACHE_DIR)):
        if not filename.endswith(".pt"):
            continue
        filepath = os.path.join(MODEL_CACHE_DIR, filename)
        stat = os.stat(filepath)

        # Read SHA-256 from sidecar if available
        sha_path = filepath + ".sha256"
        sha256 = None
        if os.path.isfile(sha_path):
            sha256 = open(sha_path).read().strip()

        models.append({
            "filename": filename,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": stat.st_mtime,
            "sha256": sha256,
        })

    return {"models": models, "model_cache_dir": MODEL_CACHE_DIR}


@app.post("/models/fetch")
async def fetch_model_to_cache(filename: str = Query(..., description="Model filename to fetch")):
    """Pre-warm: fetch a model from the models service into the local cache."""
    from model_resolver import fetch_model

    try:
        result = await fetch_model(filename)
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch model: {e}")


@app.get("/health")
async def health():
    """Health check with device, model cache, and database info."""
    from stages.yolo_detector import _model_cache

    db_status = "unknown"
    try:
        from database import async_session
        async with async_session() as session:
            await session.execute(select(1))
            db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "ok",
        "device": DEVICE,
        "models_service_url": MODELS_SERVICE_URL,
        "model_cache_dir": MODEL_CACHE_DIR,
        "loaded_models": list(_model_cache.keys()),
        "loaded_model_count": len(_model_cache),
        "database": db_status,
    }


# ── Legacy Ad-Hoc Pipeline Test ────────────────────────────────────────────


@app.post("/pipelines/test")
async def test_pipeline_adhoc(
    image: UploadFile = File(...),
    pipeline_definition: str = Form(...),
):
    """Execute an ad-hoc pipeline definition against a test image. Returns full execution trace."""
    try:
        definition = json.loads(pipeline_definition)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid pipeline definition JSON: {e}")

    if "nodes" not in definition or "edges" not in definition:
        raise HTTPException(400, "Pipeline definition must contain 'nodes' and 'edges'")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "Empty image file")

    try:
        engine = PipelineEngine.from_definition(definition)
    except Exception as e:
        raise HTTPException(400, f"Failed to build pipeline: {e}")

    result = await engine.execute(image_bytes)
    return result


# ── Flow/Deploy Endpoints ──────────────────────────────────────────────────


@app.get("/pipelines/active/{flow}")
async def get_active_pipeline(
    flow: str,
    session: AsyncSession = Depends(get_session),
):
    """Get the currently active pipeline for a flow."""
    result = await session.execute(
        select(Pipeline)
        .where(Pipeline.flow == flow, Pipeline.status == "active")
    )
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, f"No active pipeline for flow: {flow}")
    return _pipeline_to_dict(pipeline)


# ── Pipeline CRUD ───────────────────────────────────────────────────────────


@app.get("/pipelines")
async def list_pipelines(
    status: Optional[str] = None,
    flow: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """List all pipelines, optionally filtered by status and/or flow."""
    q = select(Pipeline).order_by(desc(Pipeline.updated_at))
    if status:
        q = q.where(Pipeline.status == status)
    if flow:
        q = q.where(Pipeline.flow == flow)

    result = await session.execute(q)
    pipelines = result.scalars().all()
    return {
        "count": len(pipelines),
        "items": [_pipeline_to_dict(p, include_definition=False) for p in pipelines],
    }


@app.post("/pipelines")
async def create_pipeline(
    body: PipelineCreate,
    session: AsyncSession = Depends(get_session),
):
    """Create a new pipeline definition."""
    pipeline_id = str(uuid.uuid4())
    now = datetime.utcnow()

    pipeline = Pipeline(
        id=pipeline_id,
        name=body.name,
        description=body.description,
        current_version=1,
        status="draft",
        flow=body.flow,
        definition=body.definition,
        created_at=now,
        updated_at=now,
        created_by=body.created_by,
    )
    session.add(pipeline)

    # Save initial version snapshot
    version = PipelineVersion(
        id=str(uuid.uuid4()),
        pipeline_id=pipeline_id,
        version=1,
        definition=body.definition,
        created_at=now,
        created_by=body.created_by,
    )
    session.add(version)

    await session.commit()
    await session.refresh(pipeline)
    return _pipeline_to_dict(pipeline)


@app.get("/pipelines/{pipeline_id}")
async def get_pipeline(
    pipeline_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get a single pipeline with its full definition."""
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")
    return _pipeline_to_dict(pipeline)


@app.put("/pipelines/{pipeline_id}")
async def update_pipeline(
    pipeline_id: str,
    body: PipelineUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Update a pipeline definition. Bumps version and saves snapshot."""
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")
    if pipeline.status == "archived":
        raise HTTPException(409, "Cannot update an archived pipeline")

    now = datetime.utcnow()
    new_version = pipeline.current_version + 1

    # Update pipeline
    if body.name is not None:
        pipeline.name = body.name
    if body.description is not None:
        pipeline.description = body.description
    if body.flow is not None:
        pipeline.flow = body.flow
    pipeline.definition = body.definition
    pipeline.current_version = new_version
    pipeline.updated_at = now

    # Save version snapshot
    version = PipelineVersion(
        id=str(uuid.uuid4()),
        pipeline_id=pipeline_id,
        version=new_version,
        definition=body.definition,
        created_at=now,
        created_by=body.created_by,
    )
    session.add(version)

    await session.commit()
    await session.refresh(pipeline)
    return _pipeline_to_dict(pipeline)


@app.delete("/pipelines/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Soft-delete a pipeline by setting status to archived."""
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    pipeline.status = "archived"
    pipeline.updated_at = datetime.utcnow()
    await session.commit()
    return {"archived": pipeline_id}


# ── Pipeline Version History ────────────────────────────────────────────────


@app.get("/pipelines/{pipeline_id}/versions")
async def list_pipeline_versions(
    pipeline_id: str,
    session: AsyncSession = Depends(get_session),
):
    """List all saved versions of a pipeline."""
    result = await session.execute(
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id)
        .order_by(desc(PipelineVersion.version))
    )
    versions = result.scalars().all()
    return {
        "count": len(versions),
        "items": [
            {
                "id": v.id,
                "pipeline_id": v.pipeline_id,
                "version": v.version,
                "definition": v.definition,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "created_by": v.created_by,
            }
            for v in versions
        ],
    }


# ── Pipeline Testing (local execution) ──────────────────────────────────────


@app.post("/pipelines/{pipeline_id}/test")
async def test_pipeline(
    pipeline_id: str,
    image: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Execute a saved pipeline against a test image. Saves test run record."""
    # Load pipeline from DB
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    # Read image
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(400, "Empty image file")

    # Build and execute pipeline locally
    try:
        engine = PipelineEngine.from_definition(pipeline.definition)
    except Exception as e:
        raise HTTPException(400, f"Failed to build pipeline: {e}")

    test_result = await engine.execute(image_bytes)

    # Save test run record
    run = PipelineTestRun(
        id=str(uuid.uuid4()),
        pipeline_id=pipeline_id,
        pipeline_version=pipeline.current_version,
        image_filename=image.filename,
        result=test_result,
        execution_time_ms=test_result.get("execution_time_ms"),
        success=test_result.get("success", False),
        created_at=datetime.utcnow(),
    )
    session.add(run)
    await session.commit()

    return test_result


@app.get("/pipelines/{pipeline_id}/test-runs")
async def list_test_runs(
    pipeline_id: str,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    """List test run history for a pipeline."""
    result = await session.execute(
        select(PipelineTestRun)
        .where(PipelineTestRun.pipeline_id == pipeline_id)
        .order_by(desc(PipelineTestRun.created_at))
        .limit(limit)
    )
    runs = result.scalars().all()
    return {"count": len(runs), "items": [_test_run_to_dict(r) for r in runs]}


# ── Pipeline Publishing ─────────────────────────────────────────────────────


@app.post("/pipelines/{pipeline_id}/publish")
async def publish_pipeline(
    pipeline_id: str,
    body: PublishRequest,
    session: AsyncSession = Depends(get_session),
):
    """Publish a pipeline as active for a flow. Deactivates the previous active pipeline for that flow."""
    # Load pipeline
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")
    if pipeline.status == "archived":
        raise HTTPException(409, "Cannot publish an archived pipeline")

    # Deactivate any currently active pipeline for this flow
    active_result = await session.execute(
        select(Pipeline)
        .where(Pipeline.flow == body.flow, Pipeline.status == "active")
    )
    for active_pipeline in active_result.scalars().all():
        if active_pipeline.id != pipeline_id:
            active_pipeline.status = "draft"
            active_pipeline.updated_at = datetime.utcnow()

    # Activate this pipeline for the flow
    pipeline.status = "active"
    pipeline.flow = body.flow
    pipeline.updated_at = datetime.utcnow()

    # Create deployment record
    deployment = PipelineDeployment(
        id=str(uuid.uuid4()),
        pipeline_id=pipeline_id,
        pipeline_version=pipeline.current_version,
        flow=body.flow,
        action="publish",
        deployed_at=datetime.utcnow(),
        deployed_by=body.deployed_by,
        notes=body.notes,
    )
    session.add(deployment)

    await session.commit()
    await session.refresh(pipeline)
    return _pipeline_to_dict(pipeline)


# ── Entrypoint ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Pipelines Service on port {PORT}, device={DEVICE}, model_cache_dir={MODEL_CACHE_DIR}, models_service={MODELS_SERVICE_URL}")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
