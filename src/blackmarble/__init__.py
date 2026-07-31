"""
blackmarble: package for Black Marble processing.
This package provides a functional API for processing NASA Black Marble data,
including Landsat temporal compositing, VIIRS nighttime lights integration,
OSM road enhancement, and urban index calculation.
"""

__version__ = "0.1.0"

# Import submodules so they're available
from . import acquire, analyze, enhance, export, prepare  # noqa: F401


__all__ = ["acquire", "analyze", "enhance", "export", "prepare"]

# Note: We don't import pipeline functions here to avoid circular imports.
# Users should import them directly:
# from blackmarble.pipeline import black_marble_pipeline, black_marble_pipeline_sync
# from blackmarble.config import PipelineConfig
