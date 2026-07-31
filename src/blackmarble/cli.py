#!/usr/bin/env python3
import logging
import os
import re
import traceback
import typing
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from .config import PipelineConfig
from .pipeline import pipeline


app = typer.Typer(help="Command-line interface for the VEDA Black Marble processing pipeline.")


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    """
    Parses a string into 4 floats. Supports commas, spaces, or mixed separators.
    Example: "min_x, min_y, max_x, max_y" or "min_x min_y max_x max_y"
    """
    try:
        # Use regex to find all numbers (integers or floats, including negatives)
        # This naturally handles any mix of spaces, commas, or semicolons
        coords = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", value)]

        if len(coords) != 4:
            raise ValueError(f"Expected 4 coordinates, found {len(coords)}")

        min_x, min_y, max_x, max_y = coords

        if min_x >= max_x or min_y >= max_y:
            raise ValueError("min values must be less than max values.")

        return (min_x, min_y, max_x, max_y)
    except Exception as e:
        raise typer.BadParameter("Invalid bbox") from e


@app.command()
def run(
    bbox: Annotated[
        str,
        typer.Option(
            "--bbox",
            help='Bounding box as "min_x,min_y,max_x,max_y" or "min_x min_y max_x max_y"',
            parser=parse_bbox,
        ),
    ],
    date: Annotated[
        datetime,
        typer.Option("--date", help="Target date for data in YYYY-MM-DD format."),
    ],
    earthdata_token: Annotated[
        str | None,
        typer.Option(
            "--earthdata-token",
            "-t",
            help="NASA Earthdata token for VIIRS downloads. "
            "If not provided, reads from EARTHDATA_TOKEN environment variable.",
        ),
    ] = None,
    output_path: Annotated[
        str,
        typer.Option(
            "--output-path",
            "-o",
            help="Output path for the final COG file.",
        ),
    ] = "black_marble_output.tif",
    data_dir: Annotated[
        str,
        typer.Option(
            "--data-dir",
            help="Base directory for data, cache, temp, and diagnostics outputs.",
        ),
    ] = "./data",
    config_preset: Annotated[
        str,
        typer.Option(
            "--config",
            "-c",
            help="Configuration preset: 'default', 'high_quality', or 'fast'.",
        ),
    ] = "default",
    save_diagnostics: Annotated[
        bool,
        typer.Option(
            "--save-diagnostics",
            "-d",
            help="Save diagnostic intermediate outputs to <data-dir>/diagnostics/",
        ),
    ] = False,
    wgs84: Annotated[
        bool,
        typer.Option(
            "--wgs84",
            "-w",
            help="Also export output in EPSG:4326 (WGS84) projection",
        ),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            "-l",
            help="Logging level: DEBUG, INFO, WARNING, ERROR",
        ),
    ] = "INFO",
    osm_source: Annotated[
        str,
        typer.Option(
            "--osm-source",
            help="OSM backend source: overpass or layercake.",
        ),
    ] = "overpass",
):
    """
    Runs the Black Marble processing pipeline for a given area and date.

    This uses the functional API pipeline with features like:
    - Temporal compositing of Landsat scenes
    - VIIRS nighttime lights integration
    - OSM road enhancement
    - Urban index calculation (NDUI)
    - Cloud-optimized GeoTIFF output
    """

    # Configure logging
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        typer.echo(f"Error: Invalid log level '{log_level}'", err=True)
        raise typer.Exit(code=1)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Suppress noisy INFO messages from AWS libraries
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    # supress logs from earthaccess, rasterio, and rasterio
    logging.getLogger("earthaccess").setLevel(logging.WARNING)
    logging.getLogger("rasterio").setLevel(logging.ERROR)

    bbox_parsed = typing.cast(tuple[float, float, float, float], bbox)

    # Resolve token from CLI option first, then explicit environment fallback
    resolved_earthdata_token = earthdata_token or str(os.getenv("EARTHDATA_TOKEN")).strip()

    # Set as environment variable for downstream compatibility
    if resolved_earthdata_token:
        os.environ["EARTHDATA_TOKEN"] = resolved_earthdata_token.strip()
    else:
        typer.echo(
            "Error: EARTHDATA_TOKEN env var not set or --earthdata-token not provided.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Ensure output directory exists (only for local paths)
    if not output_path.startswith("s3://"):
        output_path_path = Path(output_path)
        output_path_path.parent.mkdir(parents=True, exist_ok=True)

    # Create configuration based on preset
    match config_preset:
        case "high_quality":
            config = PipelineConfig.for_enhanced_quality(
                bbox=bbox_parsed, date=date, output_path=output_path
            )
        case "fast":
            # For fast processing, use basic mode with minimal enhancements
            config = PipelineConfig.for_bm_parity(
                bbox=bbox_parsed, date=date, output_path=output_path
            )
            # Disable optional outputs for speed
            config.export.generate_urban_mask = False
            config.export.generate_indices_file = False
            config.export.validate_before_export = False
        case "default":
            config = PipelineConfig.for_bm_parity(
                bbox=bbox_parsed, date=date, output_path=output_path
            )
        case _:
            typer.echo(
                f"Error: Unknown config preset '{config_preset}'. "
                f"Use 'default', 'high_quality', or 'fast'.",
                err=True,
            )
            raise typer.Exit(code=1)

    # Enable diagnostics if requested
    if save_diagnostics:
        config.save_diagnostics = True
        typer.echo(f"📊 Diagnostic outputs will be saved to {data_dir}/diagnostics/")
        # Create diagnostics directory
        os.makedirs(f"{data_dir}/diagnostics", exist_ok=True)

    # Enable WGS84 export if requested
    if wgs84:
        config.export.generate_wgs84 = True
        typer.echo("🌍 WGS84 (EPSG:4326) export enabled")

    # Configure NTL enhancement - always use multiplicative mode
    config.analysis.ntl_enhancement_mode = "multiplicative"
    osm_source_normalized = osm_source.strip().lower()
    if osm_source_normalized not in {"overpass", "layercake"}:
        typer.echo(
            f"Error: Unknown --osm-source '{osm_source}'. Use 'overpass' or 'layercake'.",
            err=True,
        )
        raise typer.Exit(code=1)
    config.acquisition.osm_source = typing.cast(typing.Literal["overpass", "layercake"], osm_source_normalized)

    typer.echo("🌃 NTL enhancement: Urban field (multiplicative mode)")
    typer.echo(f"   Urban field scales: {config.analysis.urban_field_sigmas} pixels")

    # Display processing info
    typer.echo("\nStarting Black Marble pipeline:")
    typer.echo(f"  Bounding Box: {bbox_parsed}")
    typer.echo(f"  Date: {date.strftime('%Y-%m-%d')}")
    typer.echo(f"  Config Preset: {config_preset}")
    typer.echo("  Road Rasterization: Fractional (5x5)")
    typer.echo(f"  OSM Source: {config.acquisition.osm_source}")
    typer.echo("  NTL Enhancement: multiplicative")
    typer.echo(f"  Output Path: {output_path}")

    try:
        typer.echo("\nRunning pipeline...")

        result = pipeline(
            bbox=bbox_parsed,
            date=date,
            output_path=output_path,
            data_dir=data_dir,
            config=config,
        )

        typer.echo("\n✅ Pipeline completed successfully!")
        typer.echo(f"📁 Output: {result['output_path']}")

        # Display statistics if available
        stats = result.get("statistics", {})
        if stats:
            typer.echo("\n📊 Statistics:")
            if stats.get("urban_area_km2") is not None:
                typer.echo(f"  Urban area: {stats['urban_area_km2']:.2f} km²")
            if stats.get("mean_radiance") is not None:
                typer.echo(f"  Mean radiance: {stats['mean_radiance']:.4f} nW/cm²/sr")

        # Display validation info
        validation = result.get("validation", {})
        if validation and not validation.get("valid", True):
            typer.echo(f"\n⚠️  Validation warnings: {validation.get('errors', [])}")

    except Exception as e:
        typer.echo(f"\n❌ Pipeline failed: {str(e)}", err=True)
        typer.echo(f"Traceback:\n{traceback.format_exc()}", err=True)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
