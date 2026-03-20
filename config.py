import os

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "fleet-ops-models-service", "models"))
PORT = int(os.environ.get("PIPELINE_RUNTIME_PORT", "8200"))
DEVICE = os.environ.get("PIPELINE_RUNTIME_DEVICE", "cpu")  # "cpu" or "cuda" — explicitly configured, never auto-detected
DATABASE_URL = os.environ.get("PIPELINE_DB_URL", "postgresql+asyncpg://pipeline:pipeline@localhost:5432/pipelines")
