# PipelinesService

Pipeline execution and management service for FleetOps. Runs on the AI server (`10.125.17.219:8200`).

Owns all pipeline data (definitions, versions, test runs, deployments) in its own PostgreSQL database. FleetOps is a UI consumer that proxies requests to this service's API.

See [PRD.md](PRD.md) for full architecture and design context.

## Setup

```bash
# Clone
git clone https://github.com/daniel-friedman-ms/PipelinesService.git
cd PipelinesService

# Test mode (local run)
bash deploy.sh

# Install as systemd service
sudo bash deploy.sh --install

# Update (git pull + restart)
sudo bash deploy.sh --update
```

### PostgreSQL

This service requires a PostgreSQL database. Tables are created automatically on startup.

```bash
# Example: create database and user
sudo -u postgres psql -c "CREATE USER pipeline WITH PASSWORD 'pipeline';"
sudo -u postgres psql -c "CREATE DATABASE pipelines OWNER pipeline;"
```

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `MODELS_SERVICE_URL` | `http://localhost:8100` | Base URL of the models service for fetching model files |
| `MODEL_CACHE_DIR` | `./model_cache` | Local directory for cached model files |
| `PIPELINE_RUNTIME_PORT` | `8200` | Service port |
| `PIPELINE_RUNTIME_DEVICE` | `cpu` | Inference device (`cpu` or `cuda`) |
| `PIPELINE_DB_URL` | `postgresql+asyncpg://pipeline:pipeline@localhost:5432/pipelines` | PostgreSQL connection string |

## API

### Pipeline CRUD

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipelines` | List pipelines (`?status=`, `?flow=` filters) |
| `POST` | `/pipelines` | Create pipeline |
| `GET` | `/pipelines/{id}` | Get pipeline with definition |
| `PUT` | `/pipelines/{id}` | Update pipeline (bumps version) |
| `DELETE` | `/pipelines/{id}` | Archive pipeline (soft delete) |
| `GET` | `/pipelines/{id}/versions` | Version history |

### Execution

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/test` | Test ad-hoc pipeline definition |
| `POST` | `/pipelines/{id}/test` | Test saved pipeline |
| `GET` | `/pipelines/{id}/test-runs` | Test run history |

### Deployment

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/pipelines/{id}/publish` | Publish pipeline to a flow |
| `GET` | `/pipelines/active/{flow}` | Get active pipeline for flow |

### Infrastructure

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/pipeline-nodes/types` | Node type registry |
| `GET` | `/models` | List cached `.pt` model files |
| `POST` | `/models/fetch?filename=X` | Pre-warm: fetch model from models service into local cache |
| `GET` | `/health` | Health check |

## systemd

```bash
sudo systemctl status PipelinesService
sudo systemctl restart PipelinesService
sudo journalctl -u PipelinesService -f
```
