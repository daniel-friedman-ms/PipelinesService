"""Image Input stage — entry point for pipeline, puts image into context."""

import io
from PIL import Image

from engine import PipelineStage, PipelineContext


class ImageInputStage(PipelineStage):

    def __init__(self, config: dict):
        self.config = config

    @property
    def stage_type(self) -> str:
        return "image_input"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.image:
            raise ValueError("No image provided to ImageInput stage")

        # Read image dimensions
        img = Image.open(io.BytesIO(ctx.image))
        width, height = img.size

        ctx.stage_results[self.node_id] = {
            "type": "image_input",
            "width": width,
            "height": height,
            "format": img.format or "unknown",
            "size_bytes": len(ctx.image),
        }

        return ctx
