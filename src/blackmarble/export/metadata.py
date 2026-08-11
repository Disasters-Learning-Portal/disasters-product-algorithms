"""Metadata creation - ONLY WHAT'S USED."""

from datetime import datetime
from typing import Any


def create_metadata(
    data_sources: dict[str, Any],
    processing_steps: list[str] | None = None,
    indices_calculated: list[str] | None = None,
    date_processed: datetime | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    crs: str | None = None,
    resolution: float | None = None,
    processing_date: datetime | None = None,
    enhancement_applied: str | None = None,
    road_types: list[str] | None = None,
) -> dict[str, Any]:
    """Create metadata dictionary for COG.

    Args:
        data_sources: Dictionary describing data sources used
        processing_steps: List of processing steps applied
        indices_calculated: List of indices calculated
        date_processed: Date when processing occurred
        bbox: Bounding box of the output
        crs: Coordinate reference system
        resolution: Spatial resolution
        processing_date: Deprecated, use date_processed
        enhancement_applied: Enhancement method applied
        road_types: Types of roads included

    Returns:
        Metadata dictionary
    """
    # Use date_processed if provided, otherwise fall back to processing_date or now
    process_date = date_processed or processing_date or datetime.now()

    metadata = {
        "creation_date": process_date.isoformat(),
        "producer": "Black Marble Pipeline",
        "data_sources": str(data_sources),
    }

    if processing_steps:
        metadata["processing_steps"] = ",".join(processing_steps)

    if bbox:
        metadata["bbox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    if crs:
        metadata["crs"] = crs

    if resolution is not None:
        metadata["resolution"] = str(resolution)

    if indices_calculated:
        metadata["indices"] = ",".join(indices_calculated)

    if enhancement_applied:
        metadata["enhancement"] = enhancement_applied

    if road_types:
        metadata["road_types"] = ",".join(road_types)

    return metadata
