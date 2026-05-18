# shared_utils API Reference

Complete reference for all functions in the `shared_utils` package.

## Table of Contents

- [High-Level Interfaces](#high-level-interfaces)
  - [notebook_helpers](#notebook_helpers) - SimpleProcessor and quick_process
  - [main_processor](#main_processor) - S3-based COG conversion pipeline
  - [cog_processing](#cog_processing) - Single-file S3 processing
- [COG Creation & Validation](#cog-creation--validation)
  - [cog_metadata](#cog_metadata) - COG creation with embedded metadata tags
  - [cog_utils](#cog_utils) - Local COG conversion and utilities
  - [gdal_cog_processor](#gdal_cog_processor) - GDAL-native COG creation
  - [cog_validation](#cog_validation) - COG validation and integrity checks
- [S3 Operations](#s3-operations)
  - [s3_operations](#s3_operations) - AWS S3 client and file operations
  - [test_upload](#test_upload) - S3 upload permission testing
- [Analysis & Verification](#analysis--verification)
  - [geotiff_analyzer](#geotiff_analyzer) - Single-file GeoTIFF analysis
  - [batch_analyzer](#batch_analyzer) - Batch analysis of multiple files
  - [verification](#verification) - Compare input vs output files
- [Data Handling](#data-handling)
  - [compression](#compression) - Nodata, predictor, and compression utilities
  - [reprojection](#reprojection) - CRS reprojection and overview creation
  - [file_naming](#file_naming) - Filename parsing and generation
- [Infrastructure](#infrastructure)
  - [profiles](#profiles) - Compression profiles by file size
  - [chunk_configs](#chunk_configs) - Chunk processing configurations
  - [memory_management](#memory_management) - Memory monitoring and optimization
  - [error_handling](#error_handling) - Error recovery and temp file cleanup
  - [log_utils](#log_utils) - Logging and status reporting
- [Legacy / Geospatial Tools](#legacy--geospatial-tools)
  - [geotools](#geotools) - GDAL-based raster utilities

---

## High-Level Interfaces

### notebook_helpers

Simplified interface for Jupyter notebooks. This is the recommended starting point.

#### `SimpleProcessor(config)`

Main class for processing disaster imagery. Handles S3 connection, file discovery, categorization, and COG conversion.

**Config dictionary keys:**

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `event_name` | str | Yes | - | Event identifier (e.g., `'202510_Flood_AK'`) |
| `bucket` | str | Yes | - | S3 bucket name |
| `source_path` | str | Yes | - | S3 prefix for source files |
| `destination_base` | str | Yes | - | S3 prefix for output files |
| `target_crs` | str/None | No | `'EPSG:3857'` | Target CRS. `None` / `'None'` / `''` = keep original. Default is Web Mercator to dodge the WGS 84 ensemble + lat-first axis bug in `rio_stac`. |
| `resampling` | str/None | No | `None` | Warp resampling (`'near'`, `'bilinear'`, `'cubic'`, `'average'`). `None` = auto-detect from data. |
| `clip_to_webmerc` | bool/None | No | `None` | Clip output to Web Mercator's ±85° lat domain. `None` = auto-detect via `needs_webmerc_clip()`. |
| `stream_from_s3` | bool | No | `True` | Probe `/vsis3/` first; fall back to `/tmp` download. Set False to force download. |
| `overwrite` | bool | No | `False` | Overwrite existing files |
| `verify` | bool | No | `True` | Verify results after processing |
| `categorization_patterns` | dict | No | built-in | Regex patterns for file categorization. Forwarded to `shared_utils.file_naming.categorize_file`. |
| `filename_creators` | dict | No | built-in | Functions to generate output filenames |
| `output_dirs` | dict | No | built-in | Category-to-directory mapping |
| `nodata_values` | dict | No | built-in | Category-specific nodata values |
| `save_results` | bool | No | `True` | Save results CSV |

**Methods:**

```python
processor = SimpleProcessor(config)
processor.connect_to_s3() -> bool
processor.discover_files() -> int          # Returns number of files found
processor.preview_processing()             # Print what will be processed
processor.process_all() -> pd.DataFrame    # Process all files, return results
```

#### `quick_process(config) -> pd.DataFrame`

One-function wrapper that runs the full pipeline: connect, discover, preview, process.

```python
from shared_utils.notebook_helpers import quick_process

results = quick_process({
    'event_name': '202510_Flood_AK',
    'bucket': 'nasa-disasters',
    'source_path': 'drcs_activations/202510_Flood_AK/sentinel2',
    'destination_base': 'drcs_activations_new',
    'target_crs': 'EPSG:4326',   # or None to keep original CRS
    'overwrite': False,
})
```

---

### main_processor

Primary entry point for S3-to-S3 COG conversion with full optimization pipeline.

#### `convert_to_cog(...)`

```python
convert_to_cog(
    name: str,                    # S3 key of source file
    bucket: str,                  # Source S3 bucket
    cog_filename: str,            # Output filename
    cog_data_bucket: str,         # Destination S3 bucket
    cog_data_prefix: str,         # Destination S3 prefix
    s3_client,                    # Boto3 S3 client
    *,
    cog_profile=None,             # (deprecated, accepted for backwards compat)
    local_output_dir=None,        # Save local copy if set
    chunk_config=None,            # (deprecated, accepted for backwards compat)
    manual_nodata=None,           # Override nodata value
    overwrite=False,              # Overwrite existing S3 files
    skip_validation=False,        # Skip COG validation step
    target_crs='EPSG:3857',       # Target CRS, None = keep original
    resampling=None,              # Warp resampling, None = auto-detect
    clip_to_webmerc=None,         # ±85° lat clip, None = auto-detect
    stream_from_s3=True,          # Probe /vsis3 first; fall back to download
)
```

**Implementation**: thin S3 orchestrator (~216 lines). The function:
1. Checks the destination S3 key (skip unless `overwrite=True`).
2. Tries `/vsis3/{bucket}/{name}` streaming when `stream_from_s3=True`; falls back to a one-shot download to `/tmp/data_download/{name}`.
3. Delegates the actual warp + COG creation to `shared_utils.cog_utils.convert_to_cog` (the engine).
4. Optionally validates the output, then uploads to S3.

The chunked / GDAL-driver / rio-cogeo branching that used to live here was removed when the function was unified onto the `cog_utils` engine — `gdalwarp` + `rio cogeo create` already handle their own chunking via `BLOCKSIZE` and `NUM_THREADS=ALL_CPUS`. Pass-through kwargs (`cog_profile`, `chunk_config`) are accepted but ignored.

---

### cog_processing

Simplified single-file S3 processing.

#### `process_single_file(...)`

```python
process_single_file(
    s3_client,                     # Boto3 S3 client
    bucket: str,                   # S3 bucket
    source_key: str,               # Source S3 key
    dest_key: str,                 # Destination S3 key
    nodata=None,                   # Nodata value (auto-detect if None)
    verify=True,                   # Validate output COG
    check_source_is_cog=True,      # Check if source is already COG
    skip_if_source_is_cog=True,    # Skip processing if already COG
    verbose=True
)
```

---

## COG Creation & Validation

### cog_metadata

Create Cloud Optimized GeoTIFFs with embedded metadata tags. Supports both in-memory (GDAL vsimem) and on-disk workflows.

#### `create_cog_with_metadata(input_data, metadata, ...) -> Union[bytes, str]`

```python
create_cog_with_metadata(
    input_data: Union[bytes, str],     # GeoTIFF bytes or file path
    metadata: Dict[str, str],          # Arbitrary key-value metadata tags
    output_path: Optional[str] = None, # File path output (None = return bytes)
    preserve_compression: bool = True, # Keep source compression settings
    compression_override: Optional[Dict] = None,  # Override compress/predictor/level
    blockxsize: int = 512,
    blockysize: int = 512,
    overview_level: int = 4,
    overview_resampling: str = 'average',
    web_optimized: bool = True,
    add_mask: bool = False,
    quiet: bool = False,
) -> Union[bytes, str]
```

Automatically adds `PROCESSING_DATE` if not present in metadata dict.

#### `read_compression_settings(input_data) -> Dict[str, Any]`

Read compression settings from a GeoTIFF. Returns `{'compress': str, 'predictor': int, 'level': int}`.

#### `validate_cog_in_memory(file_bytes, filename="temp.tif") -> Tuple[bool, dict]`

Validate COG structure from in-memory bytes. Returns `(is_valid, info_dict)` with compression, blocksize, overviews, errors, warnings.

---

### cog_utils

Local COG conversion utilities (no S3 required).

#### `convert_to_cog(...) -> str`

```python
convert_to_cog(
    input_tif: str,                         # Input GeoTIFF path OR /vsi* URI
    output_cog: str = None,                 # Output path (None = replace input)
    nodata: Optional[Union[int, float]] = None,  # Nodata (auto-detect if None)
    dst_crs: str = 'EPSG:3857',             # Target CRS (None = keep original)
    resampling_method: str = None,          # 'near'/'bilinear'/'cubic'/'average'; None = auto
    clip_to_webmerc: bool = None,           # ±85° lat clip; None = auto-detect
    compression: str = 'ZSTD',
    compression_level: int = 22,
    overview_levels: int = 5,
    quiet: bool = False,
    backend: str = 'rio'                    # 'rio' or 'gdal'
) -> str                                    # Returns path to created COG
```

The engine: subprocess `gdalwarp` (with `NUM_THREADS=ALL_CPUS`) + `rio cogeo create`.
Default `dst_crs` is EPSG:3857 (see CLAUDE.md / .clinerules.md for the airflow ensemble bug).
Accepts `/vsis3/`, `/vsicurl/`, etc. — useful when called from `main_processor` in streaming mode.

#### `validate_cog(cog_path) -> Tuple[bool, dict]`

Validate a file is a proper COG. Returns `(is_valid, details_dict)`.

#### `set_nodata_value(dtype, manual_nodata=None) -> Union[int, float]`

Auto-select nodata value based on data type. Returns appropriate value (e.g., `0` for uint8, `-9999.0` for float32).

#### `validate_nodata_for_dtype(nodata, dtype) -> dict`

Check if a nodata value is valid for the data type. Returns `{'valid': bool, 'message': str}`.

#### `determine_resampling_method(src_path) -> Tuple[str, str]`

Auto-detect resampling method. Returns `(resampling, overview_resampling)` — e.g., `('bilinear', 'average')` for continuous data, `('nearest', 'mode')` for categorical.

#### `get_compression_profile(compression, compression_level, dtype, file_size_gb) -> dict`

Get compression configuration dictionary for COG creation.

#### `get_final_filename(original_path, event_name=None, tif_only=False) -> str`

Predict the final filename after COG conversion and event renaming.

#### `rename_with_event(file_path, event_name, quiet=False) -> str`

Rename a file to include event name prefix and formatted date suffix.

---

### gdal_cog_processor

GDAL-native COG creation via subprocess. Maximum performance.

#### `create_cog_gdal(...) -> bool`

```python
create_cog_gdal(
    input_path: str,
    output_path: str,
    nodata: Optional[float] = None,
    compress: str = 'ZSTD',
    compress_level: int = 22,
    blocksize: int = 512,
    reproject_to_4326: bool = True,    # Ignored if target_crs is set
    target_crs: Optional[str] = None,  # None = keep original CRS
    verbose: bool = True
) -> bool
```

Uses two-stage process: `gdalwarp` (reproject) then `gdal_translate` (COG creation).

#### `process_file_optimized(...) -> bool`

```python
process_file_optimized(
    input_path: str,
    output_path: str,
    nodata: Optional[float] = None,
    file_size_gb: float = 0,
    reproject: bool = True,            # Ignored if target_crs is set
    target_crs: Optional[str] = None,  # None = keep original CRS
    verbose: bool = True
) -> bool
```

Auto-selects compression level by file size: < 1 GB = level 22, < 3 GB = level 15, larger = level 9.

#### `validate_cog_gdal(file_path) -> Tuple[bool, str]`

Validate COG using `gdalinfo`. Returns `(is_valid, message)`.

---

### cog_validation

COG validation and data integrity checks.

#### `validate_cog(file_path) -> Tuple[bool, dict]`

Full COG validation. Returns `(is_valid, details)`.

#### `check_cog_with_warnings(file_path, verbose=True) -> bool`

Validate COG and print warnings. Returns `True` if valid.

#### `is_s3_file_cog(s3_client, bucket, key, verbose=False) -> Tuple[bool, dict]`

Check if an S3 file is already a valid COG (downloads header only).

#### `check_and_fix_nan_values(data, nodata_value, dtype, band_idx=None, verbose=False)`

Check for NaN values and fix them.

#### `validate_nodata_value(file_path, expected_nodata, verbose=True)`

Validate that a COG file has the correct nodata value set.

#### `validate_data_integrity(data, expected_shape=None, expected_dtype=None, verbose=True)`

Comprehensive data integrity checks.

---

## S3 Operations

### s3_operations

AWS S3 client management and file operations.

#### `initialize_s3_client(bucket_name='nasa-disasters', verbose=True) -> Tuple[client, fs_read]`

Initialize S3 client with automatic credential detection. Tries STS assume-role first (if `aws_credentials.py` exists), then falls back to default credentials.

#### `list_s3_files(s3_client, bucket, prefix, suffix='.tif') -> List[str]`

List all files matching prefix and suffix.

#### `check_s3_file_exists(s3_client, bucket, key) -> bool`

Check if a file exists in S3.

#### `get_file_size_from_s3(s3_client, bucket, key) -> float`

Get file size in GB.

#### `download_from_s3(s3_client, bucket, key, local_path, verbose=True) -> bool`

Download file from S3.

#### `upload_to_s3(s3_client, local_path, bucket, key, verbose=True) -> bool`

Upload file to S3. Uses multipart upload for files > 100 MB.

#### `setup_vsi_credentials(s3_client) -> dict`

Setup GDAL VSI credentials for S3 streaming (avoids downloading files).

#### `check_s3_cog_status(s3_client, bucket, key, verbose=False)`

Check if S3 file exists and whether it's already a valid COG.

---

### test_upload

#### `test_s3_upload(bucket='nasa-disasters', prefix='_test_uploads') -> bool`

Verify S3 upload permissions by writing and cleaning up a test object.

---

## Analysis & Verification

### geotiff_analyzer

Analyze GeoTIFF files for metadata, statistics, and nodata recommendations.

#### `summarize_raster(path, band=1, nodata=None) -> Dict[str, float]`

Lightweight per-band stats: `min`, `max`, `mean`, `nodata_count`, `valid_count`. Pass `nodata=...` to override the file's recorded sentinel. Returns NaN-valued stats for all-nodata input (instead of raising). Also exposed as the `summarize_raster` CLI (`raster_tools/`).

#### `analyze_geotiff(file_path, sample_size=None) -> Dict`

Full analysis: CRS, bounds, bands, statistics, nodata, compression.

#### `analyze_s3_geotiff(bucket, key, sample_size=None) -> Dict`

Same as `analyze_geotiff` but reads from S3.

#### `suggest_nodata_value(dtype, bands, current_nodata) -> Dict`

Suggest optimal nodata value based on data type and actual data values.

#### `format_analysis_report(results) -> str`

Format analysis dict into readable text report.

---

### batch_analyzer

Parallel analysis of multiple GeoTIFF files.

#### `analyze_batch_local(file_paths, max_workers=4) -> List[Dict]`

Analyze multiple local files in parallel.

#### `analyze_batch_s3(bucket, prefix, suffix='.tif', max_workers=4, limit=None) -> List[Dict]`

Analyze multiple S3 files.

#### `generate_summary_statistics(results) -> Dict`

Generate summary stats from batch results.

#### `create_detailed_report(results) -> pd.DataFrame`

Create DataFrame report from batch results.

---

### verification

Compare input and output files to verify processing integrity.

#### `compare_geotiffs(input_path, output_path, band=1, sample_size=None) -> Dict`

Compare two GeoTIFF files. Returns statistics on value differences, nodata handling, CRS changes.

#### `verify_s3_files(input_bucket, input_key, output_bucket, output_key, save_dir, s3_client=None) -> Dict`

Download and compare S3 files.

#### `create_comparison_plots(input_path, output_path, comparison_results, save_dir, band=1) -> List[str]`

Generate visualization plots.

#### `create_verification_report(verification_results, save_path)`

Create summary report from multiple verification results.

---

## Data Handling

### compression

Nodata value management, predictor selection, and compression configuration.

#### `set_nodata_value(ds) -> float`

Set nodata based on data type for a dataset object (GDAL-style).

#### `set_nodata_value_src(src, manual_nodata=None) -> float`

Set nodata for a rasterio source. Uses `manual_nodata` if provided, otherwise auto-detects.

#### `get_predictor_for_dtype(dtype) -> int`

Returns predictor: `1` (none) for uint8, `2` (horizontal) for integers, `3` (floating point) for floats.

#### `validate_nodata_for_dtype(nodata_value, dtype) -> dict`

Validate nodata is appropriate for the dtype.

#### `remap_nodata_value(data, original_nodata, new_nodata, dtype)`

Remap nodata values in an array.

#### `get_compression_config(file_size_gb=0, dtype='float32') -> dict`

Optimal compression config by file size and dtype.

---

### reprojection

Pure-rasterio warp helpers plus the **`needs_webmerc_clip()`** helper that decides when a source raster needs a ±85° latitude clip to stay inside Web Mercator's valid domain.

```python
from shared_utils.reprojection import (
    needs_webmerc_clip,        # (src_or_path, dst_crs) -> bool — True iff source lat bounds exceed ±85.05° AND dst_crs ≈ EPSG:3857
    WEBMERC_VALID_LAT,         # 85.05112878
    WEBMERC_EXTENT_M,          # 20037508.342789244
    WEBMERC_EPSGS,             # {'EPSG:3857', 'EPSG:900913', 'EPSG:102100', 'EPSG:102113'}
)
```

Returns False for the 99% regional-raster case → no behavior change for sensor pipelines.
Returns True for global Mollweide, polar stereographic, and similar world-extent sources targeting Web Mercator. `cog_utils.convert_to_cog` consults this automatically when `clip_to_webmerc=None`.

CRS reprojection and COG overview creation.

#### `calculate_transform_parameters(src, dst_crs='EPSG:4326') -> Tuple[transform, width, height]`

Calculate reprojection transform parameters.

#### `process_whole_file(src, dst, src_crs, dst_crs, transform, width, height, src_nodata, dst_nodata=None)`

Reproject entire file at once (for files < 1.5 GB).

#### `process_with_fixed_chunks(src, dst, src_crs, dst_crs, transform, width, height, chunk_size, src_nodata, chunk_config, initial_memory, dst_nodata=None)`

Reproject with fixed chunk size (memory-safe for large files).

#### `add_cog_overviews(file_path, verbose=True)`

Add overviews to make a GeoTIFF a valid COG.

#### `calculate_overview_factors(width, height) -> List[int]`

Calculate appropriate overview factors based on image dimensions.

---

### file_naming

**Single source of truth for filename transforms and categorization.** Pure Python (no GDAL dep) so it can be imported from any notebook style — CLI subprocess, Python API, or class wrappers. Both legacy (`extract_date_from_filename`, `create_cog_filename`, `parse_filename_components`) and new unified (`extract_datetime_from_filename`, `categorize_file`, `create_output_filename`) helpers live here; the legacy set is preserved for backwards compatibility with unit tests and `shared_utils_reference.ipynb`.

New code should use the unified helpers:

```python
from shared_utils.file_naming import (
    DATETIME_PATTERNS,           # list of (regex, granularity) pairs, ordered most-specific-first
    extract_datetime_from_filename,  # -> (matched_str, 'hour'|'day') | (None, None)
    categorize_file,                  # (filename, {regex: subdir}) -> subdir | 'uncategorized'
    create_output_filename,           # (path, event, categories=None) -> '{event}_{stem}_{date}_{granularity}.tif'
    no_change,                        # passthrough builder for sub-products like AVIRIS
)
```

`create_output_filename` auto-normalizes 8-digit `YYYYMMDD` to hyphenated `YYYY-MM-DD` so the output matches the legacy operator-facing convention.

Filename parsing, date extraction, and standardized naming.

#### `extract_date_from_filename(filename) -> str`

Extract date string (YYYYMMDD) from filename.

#### `convert_date(date_str) -> str`

Convert `YYYYMMDD` to `YYYY-MM-DD`.

#### `parse_filename_components(filepath) -> dict`

Parse filename into components: `date`, `satellite`, `product`, `location`.

#### `create_cog_filename(original_path, event_name, custom_suffix='day') -> str`

Generate standardized COG filename: `{event}_{stem}_{date}_{suffix}.tif`.

#### `create_output_path(base_dir, target_dir, filename) -> str`

Create full output path from components.

---

## Infrastructure

### profiles

Compression and processing profiles scaled by file size.

#### `select_profile_by_size(file_size_gb) -> dict`

Auto-select profile: standard (< 3 GB), large (3-7 GB), or ultra-large (> 7 GB).

#### `get_compression_profile(dtype='float32', file_size_gb=0) -> dict`

Get compression settings tuned for data type and size.

#### `get_standard_profile() -> dict` / `get_large_file_profile()` / `get_ultra_large_profile()`

Explicit profile getters.

---

### chunk_configs

Chunk processing configurations for memory management.

#### `get_chunk_config(file_size_gb=0, memory_limit_mb=500) -> dict`

Auto-select chunk config by file size. Returns dict with `chunk_size`, `adaptive_chunks`, `use_streaming`, etc.

#### `get_fixed_chunk_config(chunk_size=256, memory_limit_mb=250) -> dict`

Fixed chunk size (prevents striping artifacts).

#### `get_adaptive_chunk_config(memory_limit_mb=500) -> dict`

Adaptive chunks that adjust based on available memory.

#### `get_memory_safe_config() -> dict`

Conservative config for memory-constrained environments.

---

### memory_management

Memory monitoring and optimization.

#### `get_memory_usage() -> float`

Current process memory in MB.

#### `get_available_memory_mb() -> float`

Available system memory in MB.

#### `calculate_optimal_chunk_size(width, height, bands, dtype_size, target_memory_mb=500) -> int`

Calculate chunk size that fits within memory target.

#### `monitor_memory(threshold_mb=1000, force_gc=True) -> dict`

Monitor memory and trigger GC if above threshold.

#### `get_dtype_size(dtype_str) -> int`

Data type size in bytes.

---

### error_handling

Error recovery and cleanup utilities.

#### `cleanup_temp_files(*file_paths) -> int`

Remove temporary files. Returns count of files removed.

#### `setup_temp_directory(preferred_dir=None) -> str`

Create and return temp directory path.

#### `handle_chunk_error(error, chunk_info, verbose=True) -> str`

Handle chunk processing errors with diagnostic info.

#### `retry_with_download(func, *args, **kwargs)`

Retry a function with download mode if streaming fails.

#### `create_error_report(errors_list) -> dict`

Create summary report from a list of errors.

---

### log_utils

Logging, status reporting, and batch reports.

#### `setup_logger(log_file=None)`

Setup logging configuration.

#### `print_status(title, status_dict)`

Print formatted status report.

#### `print_summary(results)`

Print processing summary from results DataFrame.

#### `create_batch_report(file_list, results_df) -> dict`

Create detailed batch processing report.

---

## Legacy / Geospatial Tools

### geotools

Low-level GDAL/osgeo raster utilities. Requires `osgeo` (GDAL Python bindings).

#### `bytescale(arr, cmin=0, cmax=1, low=0, high=255)`

Scale array values to byte range (0-255).

#### `match_geotiff(srcfile, matchfile, outfile)`

Match one GeoTIFF's extent/resolution to another.

#### `get_geo(f, band=1)`

Read GeoTIFF: returns `(array, geotransform, projection)`.

#### `dump_geotiff_float(filename, arr, projref, in_geo)`

Write float32 GeoTIFF.

#### `dump_geotiff_byte(filename, arr, projref, in_geo)`

Write uint8 GeoTIFF.

#### `dump_geotiff_rgb(filename, r, g, b, projref, in_geo)`

Write 3-band RGB GeoTIFF.
