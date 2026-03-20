"""YOLO Detector stage — loads ultralytics model and runs inference."""

import io
import logging

from PIL import Image

from config import DEVICE
from engine import PipelineStage, PipelineContext

logger = logging.getLogger(__name__)

# Module-level model cache to avoid reloading on every test run
_model_cache: dict[str, object] = {}


async def get_model(filename: str):
    """Load a YOLO model, fetching from models service if not cached locally."""
    from model_resolver import ensure_model_on_disk

    path, changed = await ensure_model_on_disk(filename)

    # Evict stale in-memory cache if the file was re-downloaded
    if changed and filename in _model_cache:
        del _model_cache[filename]

    if filename not in _model_cache:
        from ultralytics import YOLO

        logger.info(f"Loading model {filename} on device={DEVICE}")
        model = YOLO(path)
        _model_cache[filename] = model

    return _model_cache[filename]


class YOLODetectorStage(PipelineStage):

    def __init__(self, config: dict):
        self.model_filename = config.get("model_filename", "")
        self.confidence_threshold = config.get("confidence_threshold", 0.25)
        self.iou_threshold = config.get("iou_threshold", 0.45)

    @property
    def stage_type(self) -> str:
        return "yolo_detector"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not self.model_filename:
            raise ValueError("model_filename is required for YOLO Detector")
        if not ctx.image:
            raise ValueError("No image in pipeline context")

        model = await get_model(self.model_filename)
        img = Image.open(io.BytesIO(ctx.image))

        results = model.predict(
            img,
            conf=self.confidence_threshold,
            iou=self.iou_threshold,
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
                    "detection_id": f"det_{len(detections)}",
                    "bbox": {
                        "x_min": round(x1),
                        "y_min": round(y1),
                        "x_max": round(x2),
                        "y_max": round(y2),
                    },
                    "class_name": cls_name,
                    "confidence": round(conf, 6),
                })

        ctx.detections = detections
        ctx.scores = [d["confidence"] for d in detections]

        ctx.stage_results[self.node_id] = {
            "type": "yolo_detector",
            "model": self.model_filename,
            "device": DEVICE,
            "confidence_threshold": self.confidence_threshold,
            "iou_threshold": self.iou_threshold,
            "detection_count": len(detections),
            "detections": detections,
        }

        return ctx
