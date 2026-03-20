"""JSON Output stage — formats pipeline results into clean output."""

from engine import PipelineStage, PipelineContext


class JSONOutputStage(PipelineStage):

    def __init__(self, config: dict):
        self.config = config

    @property
    def stage_type(self) -> str:
        return "json_output"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        output = {
            "detection_count": len(ctx.detections),
            "detections": ctx.detections,
            "metadata": ctx.image_metadata,
        }

        if ctx.decisions:
            output["decisions"] = ctx.decisions

        if ctx.output:
            output["output"] = ctx.output

        ctx.stage_results[self.node_id] = {
            "type": "json_output",
            "output": output,
        }

        return ctx
