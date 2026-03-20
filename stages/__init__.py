"""Register all pipeline stage types."""

from engine import StageRegistry
from .image_input import ImageInputStage
from .yolo_detector import YOLODetectorStage
from .ensemble import EnsembleStage
from .json_output import JSONOutputStage

StageRegistry.register("image_input", ImageInputStage)
StageRegistry.register("yolo_detector", YOLODetectorStage)
StageRegistry.register("ensemble", EnsembleStage)
StageRegistry.register("json_output", JSONOutputStage)
