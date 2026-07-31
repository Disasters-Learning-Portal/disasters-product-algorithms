"""Target configuration structure for the new functional pipeline.

This module shows the ideal configuration design for the reorganized
Black Marble pipeline, with clear separation of concerns across phases.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Self

from blackmarble.typing import BBox


@dataclass
class LandsatAcquisitionConfig:
    """Configuration for time-window based Landsat acquisition."""

    # Time window settings
    window_days: int = 7  # Search ±7 days from each target date (balanced approach)

    # Date-grouped selection settings
    # Design: With 12 dates × ±7 day windows throughout the year, we ensure catching
    # opportunities to find good coverage. Therefore we prioritize:
    # 1. Temporal consistency (tolerance_days=12) - all tiles from within 12 days
    # 2. Spatial completeness - prefer dates with more tile coverage
    # 3. Quality optimization - among available dates, pick the one with lowest cloud cover
    tolerance_days: float = 12.0  # Max days between scenes in same group (balanced grouping)


@dataclass
class AcquisitionConfig:
    """Configuration for data acquisition phase."""

    # Landsat settings
    cloud_cover_max: float = 50.0  # More permissive to accept T2 scenes
    composite_months: int = (
        12  # Total number of months for temporal composite (including target date)
    )

    # Landsat time-window acquisition configuration
    landsat_acquisition: LandsatAcquisitionConfig = field(default_factory=LandsatAcquisitionConfig)

    # OSM settings
    osm_source: Literal["overpass", "layercake"] = "overpass"
    road_types: list[str] = field(
        default_factory=lambda: ["motorway", "trunk", "primary", "secondary"]
    )
    osm_buffer_meters: float | dict[str, float] | None = (
        None  # Buffer in meters (float) or dict by road type
    )


@dataclass
class PreparationConfig:
    """Configuration for data preparation phase."""

    # Landsat scaling (matches existing config.py)
    landsat_scale_factor: float = 0.0000275
    landsat_add_offset: float = -0.2
    valid_dn_min: int = 7273
    valid_dn_max: int = 43636

    # Quality masking
    mask_clouds: bool = True
    mask_shadows: bool = True
    mask_snow: bool = True
    mask_water: bool = False
    qa_dilation_radius: int = 3
    qa_cloud_strategy: Literal["conservative", "moderate", "permissive"] = "moderate"


@dataclass
class AnalysisConfig:
    """Configuration for analysis phase."""

    # Index calculations (matches existing config.py)
    ndvi_epsilon: float = 1e-6
    ndwi_epsilon: float = 1e-6

    # NDUI parameters (matches existing config.py)
    ntl_floor: float = 0.1
    ntl_ceiling: float = 10.0
    ndui_floor: float = 0.02
    ndui_epsilon: float = 1e-6

    # Temporal compositing
    min_valid_observations: int = 1

    # OSM road rasterization (fractional coverage)
    osm_sub_pixels: int = 5  # Sub-pixels per dimension (5x5 = 25 total)

    # NTL enhancement uses multi-scale Gaussian field-based enhancement

    # Urban field computation (used when approach="urban_field")
    urban_field_sigmas: list[float] = field(
        default_factory=lambda: [2.0, 5.0, 10.0]
    )  # Gaussian scales in pixels
    urban_field_weights: list[float] = field(
        default_factory=lambda: [0.2, 0.3, 0.3, 0.2]
    )  # Direct, local, block, district
    ntl_enhancement_mode: Literal["multiplicative", "additive", "hybrid"] = "multiplicative"

    # Enhancement strength parameters
    enhancement_multiplicative_factor: float = (
        0.3  # Multiplicative mode: max enhancement factor - 1 (e.g., 0.3 = up to 1.3x)
    )


@dataclass
class EnhancementConfig:
    """Configuration for enhancement phase."""

    # Contrast enhancement
    contrast_method: Literal["linear", "percentile"] = "linear"
    contrast_percentiles: tuple[float, float] = (2.0, 98.0)
    contrast_output_range: tuple[float, float] = (0.0, 1.0)

    # Colormap settings (matches existing config.py display range)
    colormap: Literal["inferno", "viridis", "plasma", "grayscale"] = "inferno"
    colormap_range: tuple[float, float] = (0.3, 0.8)
    zero_as_black: bool = True


@dataclass
class ExportConfig:
    """Configuration for export phase."""

    # COG settings
    compress: str = "deflate"
    tiled: bool = True
    overview_levels: list[int] = field(default_factory=lambda: [2, 4, 8, 16])

    # Output formats
    generate_urban_mask: bool = False
    generate_indices_file: bool = False
    generate_web_image: bool = True
    generate_wgs84: bool = False
    web_image_quality: int = 85

    # Validation
    validate_before_export: bool = True
    generate_metadata: bool = True


@dataclass
class MemoryConfig:
    """Configuration for memory management during processing."""

    # Memory optimization settings
    delete_bands_after_indices: bool = True  # Delete band arrays after calculating indices


@dataclass
class PipelineConfig:
    """Main configuration class for the target pipeline."""

    # Processing parameters
    bbox: BBox
    date: datetime
    output_path: str

    # Phase configurations
    acquisition: AcquisitionConfig = field(default_factory=AcquisitionConfig)
    preparation: PreparationConfig = field(default_factory=PreparationConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    enhancement: EnhancementConfig = field(default_factory=EnhancementConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    # Global settings
    max_workers: int = 4
    min_valid_pixel_percentage: float = 10.0

    # Diagnostics
    save_diagnostics: bool = False

    @classmethod
    def for_bm_parity(cls, bbox: BBox, date: datetime, output_path: str) -> Self:
        """Create configuration that exactly matches legacy bm.py behavior.

        This uses the parameters that ensure parity with the legacy docker/bm.py implementation.

        Args:
            bbox: Bounding box for processing
            date: Target date
            output_path: Output file path

        Returns:
            PipelineConfig configured for bm.py parity
        """
        return cls(
            bbox=bbox,
            date=date,
            output_path=output_path,
            enhancement=EnhancementConfig(
                contrast_method="linear",
                colormap="inferno",
            ),
            analysis=AnalysisConfig(ntl_floor=0.1, ntl_ceiling=10.0, ndui_floor=0.02),
            export=ExportConfig(generate_web_image=True, validate_before_export=False),
        )

    @classmethod
    def for_enhanced_quality(cls, bbox: BBox, date: datetime, output_path: str) -> Self:
        """Create configuration for enhanced quality output.

        This enables advanced features that improve output quality beyond
        what bm.py produces, suitable for publication or high-quality visualization.

        Args:
            bbox: Bounding box for processing
            date: Target date
            output_path: Output file path

        Returns:
            PipelineConfig configured for enhanced quality
        """
        return cls(
            bbox=bbox,
            date=date,
            output_path=output_path,
            enhancement=EnhancementConfig(
                contrast_method="percentile",
            ),
            export=ExportConfig(
                generate_urban_mask=True,
                generate_indices_file=True,
                validate_before_export=True,
            ),
        )
