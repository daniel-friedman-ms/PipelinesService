"""Pipeline execution engine — PipelineContext, PipelineStage ABC, StageRegistry, PipelineEngine."""

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    """Carries data through the pipeline. Accumulates results — each node adds, nothing is replaced."""

    # Input
    image: bytes | None = None
    image_metadata: dict = field(default_factory=dict)

    # Detection results (accumulated)
    detections: list[dict] = field(default_factory=list)

    # Aggregated scores
    scores: list[float] = field(default_factory=list)

    # Per-node results (for debugging / test mode)
    stage_results: dict[str, Any] = field(default_factory=dict)

    # Generic pipeline output — any terminal stage writes here
    output: dict = field(default_factory=dict)

    # Generic routing decisions from logic nodes (node_id -> decision data)
    decisions: dict[str, Any] = field(default_factory=dict)


class PipelineStage(ABC):
    """Base interface — all pipeline nodes implement this."""

    node_id: str = ""

    @abstractmethod
    async def process(self, ctx: PipelineContext) -> PipelineContext:
        ...

    @property
    @abstractmethod
    def stage_type(self) -> str:
        ...


class StageRegistry:
    """Factory registry — maps type strings to stage classes."""

    _stages: dict[str, type[PipelineStage]] = {}

    @classmethod
    def register(cls, type_name: str, stage_class: type[PipelineStage]):
        cls._stages[type_name] = stage_class

    @classmethod
    def create(cls, type_name: str, config: dict) -> PipelineStage:
        stage_class = cls._stages.get(type_name)
        if not stage_class:
            raise ValueError(f"Unknown stage type: {type_name}. Registered: {list(cls._stages.keys())}")
        return stage_class(config)

    @classmethod
    def registered_types(cls) -> list[str]:
        return list(cls._stages.keys())


class PipelineEngine:
    """Builds and executes a pipeline from a JSON definition."""

    def __init__(self, nodes: dict[str, PipelineStage], execution_order: list[str]):
        self.nodes = nodes
        self.execution_order = execution_order

    @classmethod
    def from_definition(cls, definition: dict) -> "PipelineEngine":
        """Build an executable pipeline from a JSON definition."""
        nodes: dict[str, PipelineStage] = {}
        for node_def in definition.get("nodes", []):
            stage = StageRegistry.create(node_def["type"], node_def.get("config", {}))
            stage.node_id = node_def["id"]
            nodes[node_def["id"]] = stage

        # Build adjacency and compute execution order (topological sort)
        edges = definition.get("edges", [])
        execution_order = cls._topological_sort(
            node_ids=list(nodes.keys()),
            edges=edges,
        )

        return cls(nodes=nodes, execution_order=execution_order)

    @staticmethod
    def _topological_sort(node_ids: list[str], edges: list[dict]) -> list[str]:
        """Topological sort of the pipeline DAG."""
        from collections import defaultdict, deque

        in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
        adj: dict[str, list[str]] = defaultdict(list)

        for edge in edges:
            src, tgt = edge["source"], edge["target"]
            if src in in_degree and tgt in in_degree:
                adj[src].append(tgt)
                in_degree[tgt] += 1

        queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(node_ids):
            raise ValueError("Pipeline contains a cycle — must be a DAG")

        return order

    async def execute(self, image: bytes, metadata: dict | None = None) -> dict:
        """Execute the pipeline and return full execution trace."""
        ctx = PipelineContext(image=image, image_metadata=metadata or {})
        start = time.perf_counter()

        for node_id in self.execution_order:
            stage = self.nodes[node_id]
            node_start = time.perf_counter()
            try:
                ctx = await stage.process(ctx)
            except Exception as e:
                logger.error(f"Stage {node_id} ({stage.stage_type}) failed: {e}")
                ctx.stage_results[node_id] = {"error": str(e)}
                return {
                    "success": False,
                    "execution_time_ms": int((time.perf_counter() - start) * 1000),
                    "node_results": ctx.stage_results,
                    "final_output": None,
                    "error": f"Stage {node_id} failed: {e}",
                }
            node_elapsed = int((time.perf_counter() - node_start) * 1000)
            if node_id in ctx.stage_results and isinstance(ctx.stage_results[node_id], dict):
                ctx.stage_results[node_id]["execution_time_ms"] = node_elapsed

        total_ms = int((time.perf_counter() - start) * 1000)

        return {
            "success": True,
            "execution_time_ms": total_ms,
            "node_results": ctx.stage_results,
            "final_output": ctx.stage_results.get(self.execution_order[-1]) if self.execution_order else None,
        }
