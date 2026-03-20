import os

MODEL_DIR = os.environ.get("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "ModelsHubService", "models"))  # Deprecated — use MODEL_CACHE_DIR
MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", os.path.join(os.path.dirname(__file__), "model_cache"))
MODELS_SERVICE_URL = os.environ.get("MODELS_SERVICE_URL", "http://localhost:8100")
PORT = int(os.environ.get("PIPELINE_RUNTIME_PORT", "8200"))
DEVICE = os.environ.get("PIPELINE_RUNTIME_DEVICE", "cpu")  # "cpu" or "cuda" — explicitly configured, never auto-detected
DATABASE_URL = os.environ.get("PIPELINE_DB_URL", "postgresql+asyncpg://pipeline:pipeline@localhost:5432/pipelines")
