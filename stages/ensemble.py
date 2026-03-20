"""Ensemble stage — runs multiple models on the same image and aggregates results."""

import io
import logging

from PIL import Image

from config import DEVICE
from engine import PipelineStage, PipelineContext
from .yolo_detector import _get_model

logger = logging.getLogger(__name__)


class EnsembleStage(PipelineStage):

    def __init__(self, config: dict):
        self.model_filenames: list[str] = config.get("models", [])
        self.strategy: str = config.get("strategy", "mean")
        self.weights: list[float] = config.get("weights", [])

    @property
    def stage_type(self) -> str:
        return "ensemble"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not self.model_filenames:
            raise ValueError("At least one model is required for Ensemble stage")
        if not ctx.image:
            raise ValueError("No image in pipeline context")

        img = Image.open(io.BytesIO(ctx.image))
        all_model_results = []

        for filename in self.model_filenames:
            model = _get_model(filename)
            results = model.predict(
                img,
                conf=0.1,  # Low threshold — ensemble decides final confidence
                device=DEVICE,
                verbose=False,
            )

            detections = []
            for result in results:
                boxes = result.boxes
                if boxes is None:
                    continue
                for i in range(len(boxes)):
                    x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                    conf = float(boxes.conf[i])
                    cls_id = int(boxes.cls[i])
                    cls_name = result.names.get(cls_id, str(cls_id))
                    detections.append({
                        "bbox": {"x_min": round(x1), "y_min": round(y1), "x_max": round(x2), "y_max": round(y2)},
                        "class_name": cls_name,
                        "confidence": round(conf, 6),
                    })

            all_model_results.append({
                "model": filename,
                "detection_count": len(detections),
                "detections": detections,
            })

        # Aggregate detections
        aggregated = self._aggregate(all_model_results)

        ctx.detections = aggregated
        ctx.scores = [d["confidence"] for d in aggregated]

        ctx.stage_results[self.node_id] = {
            "type": "ensemble",
            "strategy": self.strategy,
            "model_count": len(self.model_filenames),
            "models": self.model_filenames,
            "per_model_results": [
                {"model": r["model"], "detection_count": r["detection_count"]}
                for r in all_model_results
            ],
            "aggregated_detection_count": len(aggregated),
            "detections": aggregated,
        }

        return ctx

    def _aggregate(self, model_results: list[dict]) -> list[dict]:
        """Aggregate detections from multiple models."""
        if not model_results:
            return []

        if len(model_results) == 1:
            dets = model_results[0]["detections"]
            for i, d in enumerate(dets):
                d["detection_id"] = f"det_{i}"
            return dets

        # Collect all detections across models, track which model produced them
        all_dets = []
        for mr in model_results:
            for det in mr["detections"]:
                all_dets.append({**det, "_model": mr["model"]})

        if not all_dets:
            return []

        # Group by IoU overlap — detections that overlap significantly are the same object
        groups = self._group_by_iou(all_dets, iou_threshold=0.5)

        # Aggregate each group
        weights = self.weights if self.weights and len(self.weights) == len(self.model_filenames) else None
        weight_map = dict(zip(self.model_filenames, weights)) if weights else None

        aggregated = []
        for group in groups:
            if self.strategy == "max":
                best = max(group, key=lambda d: d["confidence"])
                det = {
                    "detection_id": f"det_{len(aggregated)}",
                    "bbox": best["bbox"],
                    "class_name": best["class_name"],
                    "confidence": best["confidence"],
                }
            elif self.strategy == "weighted_average" and weight_map:
                total_w = sum(weight_map.get(d["_model"], 1.0) for d in group)
                avg_conf = sum(d["confidence"] * weight_map.get(d["_model"], 1.0) for d in group) / total_w
                ref = group[0]
                det = {
                    "detection_id": f"det_{len(aggregated)}",
                    "bbox": ref["bbox"],
                    "class_name": ref["class_name"],
                    "confidence": round(avg_conf, 6),
                }
            else:  # mean
                avg_conf = sum(d["confidence"] for d in group) / len(group)
                ref = group[0]
                det = {
                    "detection_id": f"det_{len(aggregated)}",
                    "bbox": ref["bbox"],
                    "class_name": ref["class_name"],
                    "confidence": round(avg_conf, 6),
                }

            aggregated.append(det)

        return aggregated

    @staticmethod
    def _group_by_iou(detections: list[dict], iou_threshold: float) -> list[list[dict]]:
        """Group detections by IoU overlap."""
        used = [False] * len(detections)
        groups: list[list[dict]] = []

        for i, det_a in enumerate(detections):
            if used[i]:
                continue
            group = [det_a]
            used[i] = True

            for j in range(i + 1, len(detections)):
                if used[j]:
                    continue
                if det_a["class_name"] != detections[j]["class_name"]:
                    continue
                if _compute_iou(det_a["bbox"], detections[j]["bbox"]) >= iou_threshold:
                    group.append(detections[j])
                    used[j] = True

            groups.append(group)

        return groups


def _compute_iou(box_a: dict, box_b: dict) -> float:
    """Compute IoU between two bounding boxes."""
    x1 = max(box_a["x_min"], box_b["x_min"])
    y1 = max(box_a["y_min"], box_b["y_min"])
    x2 = min(box_a["x_max"], box_b["x_max"])
    y2 = min(box_a["y_max"], box_b["y_max"])

    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0

    area_a = (box_a["x_max"] - box_a["x_min"]) * (box_a["y_max"] - box_a["y_min"])
    area_b = (box_b["x_max"] - box_b["x_min"]) * (box_b["y_max"] - box_b["y_min"])
    union = area_a + area_b - inter

    return inter / union if union > 0 else 0.0
