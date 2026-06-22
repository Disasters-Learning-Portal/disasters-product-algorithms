#!/usr/bin/env python3
"""
GeoTIFF Analysis Tool - Analyze min/max values and no-data configuration.
Provides comprehensive analysis of GeoTIFF files for optimal no-data value selection.
"""

import os
import sys
import argparse
import numpy as np
import rasterio
from rasterio.windows import Window
import boto3
from pathlib import Path
import tempfile
import json
from typing import Dict, List, Tuple, Optional, Union

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_utils.s3_operations import initialize_s3_client, get_file_size_from_s3


def analyze_geotiff(file_path: str, sample_size: int = None) -> Dict:
    """
    Analyze a GeoTIFF file and return statistics including min/max values.

    Args:
        file_path: Path to the GeoTIFF file
        sample_size: Optional number of pixels to sample for large files

    Returns:
        Dictionary containing analysis results
    """
    results = {
        'file': file_path,
        'file_size_mb': os.path.getsize(file_path) / (1024 * 1024),
        'bands': []
    }

    with rasterio.open(file_path) as src:
        results['width'] = src.width
        results['height'] = src.height
        results['crs'] = str(src.crs)
        results['dtype'] = str(src.dtypes[0])
        results['nodata_current'] = src.nodata
        results['band_count'] = src.count

        # Analyze each band
        for band_idx in range(1, src.count + 1):
            band_stats = analyze_band(src, band_idx, sample_size)
            results['bands'].append(band_stats)

        # Suggest optimal no-data value
        results['suggested_nodata'] = suggest_nodata_value(
            results['dtype'],
            results['bands'],
            results['nodata_current']
        )

        # Validate current no-data
        if results['nodata_current'] is not None:
            results['nodata_conflicts'] = check_nodata_conflicts(
                results['bands'],
                results['nodata_current']
            )

    return results


def summarize_raster(
    path: str,
    band: int = 1,
    nodata: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute basic statistics for a single band of a GeoTIFF.

    Parameters
    ----------
    path : str
        Absolute path to the GeoTIFF.
    band : int
        1-indexed band number (rasterio convention).
    nodata : float, optional
        Override the file's stored nodata value. If None, uses the
        nodata recorded in the dataset metadata.

    Returns
    -------
    dict with keys: min, max, mean, nodata_count, valid_count
    """
    with rasterio.open(path) as src:
        data = src.read(band, masked=False)
        nd = nodata if nodata is not None else src.nodata

    if nd is not None:
        mask = data != nd
        valid = data[mask]
    else:
        valid = data.ravel()

    if valid.size == 0:
        return {
            "min": float("nan"),
            "max": float("nan"),
            "mean": float("nan"),
            "nodata_count": int(data.size),
            "valid_count": 0,
        }

    return {
        "min": float(valid.min()),
        "max": float(valid.max()),
        "mean": float(valid.mean()),
        "nodata_count": int(data.size - valid.size),
        "valid_count": int(valid.size),
    }


def analyze_band(src, band_idx: int, sample_size: int = None) -> Dict:
    """
    Analyze a single band of a raster.

    Args:
        src: Rasterio dataset
        band_idx: Band index (1-based)
        sample_size: Optional sampling size for large files

    Returns:
        Dictionary with band statistics
    """
    stats = {
        'band': band_idx,
        'statistics': {}
    }

    # Read data efficiently
    if sample_size and (src.width * src.height) > sample_size:
        # Sample the data for large files
        data = sample_band_data(src, band_idx, sample_size)
    else:
        # Read entire band for smaller files
        data = src.read(band_idx)

    # Handle no-data values
    if src.nodata is not None:
        valid_data = data[data != src.nodata]
        stats['nodata_count'] = np.sum(data == src.nodata)
        stats['nodata_percentage'] = (stats['nodata_count'] / data.size) * 100
    else:
        valid_data = data.flatten()
        stats['nodata_count'] = 0
        stats['nodata_percentage'] = 0

    # Calculate statistics on valid data only
    if valid_data.size > 0:
        stats['statistics'] = {
            'min': float(np.min(valid_data)),
            'max': float(np.max(valid_data)),
            'mean': float(np.mean(valid_data)),
            'std': float(np.std(valid_data)),
            'median': float(np.median(valid_data)),
            'valid_pixels': int(valid_data.size),
            'total_pixels': int(data.size)
        }

        # Find unique values for small datasets
        if valid_data.size < 1000000:  # Less than 1M pixels
            unique_vals = np.unique(valid_data)
            if len(unique_vals) < 100:
                stats['unique_values'] = unique_vals.tolist()

        # Check for special values
        if np.issubdtype(valid_data.dtype, np.floating):
            stats['has_nan'] = bool(np.any(np.isnan(valid_data)))
            stats['has_inf'] = bool(np.any(np.isinf(valid_data)))
    else:
        stats['statistics'] = {
            'min': None,
            'max': None,
            'mean': None,
            'std': None,
            'median': None,
            'valid_pixels': 0,
            'total_pixels': int(data.size)
        }

    return stats


def sample_band_data(src, band_idx: int, sample_size: int) -> np.ndarray:
    """
    Sample data from a band for efficient processing of large files.

    Args:
        src: Rasterio dataset
        band_idx: Band index
        sample_size: Number of pixels to sample

    Returns:
        Sampled data array
    """
    # Calculate sampling interval
    total_pixels = src.width * src.height
    interval = max(1, total_pixels // sample_size)

    # Create sampling windows
    sampled_data = []
    for i in range(0, src.height, int(np.sqrt(interval))):
        for j in range(0, src.width, int(np.sqrt(interval))):
            window = Window(j, i, 1, 1)
            pixel = src.read(band_idx, window=window)
            sampled_data.append(pixel)

    return np.array(sampled_data).flatten()


def suggest_nodata_value(dtype: str, bands: List[Dict], current_nodata: Optional[float]) -> Dict:
    """
    Suggest an optimal no-data value based on data type and actual data values.

    Args:
        dtype: Data type string
        bands: List of band statistics
        current_nodata: Current no-data value

    Returns:
        Dictionary with suggested no-data value and reasoning
    """
    suggestion = {
        'value': None,
        'reasoning': '',
        'alternatives': []
    }

    # Collect all valid values across bands
    all_mins = [b['statistics']['min'] for b in bands if b['statistics']['min'] is not None]
    all_maxs = [b['statistics']['max'] for b in bands if b['statistics']['max'] is not None]

    if not all_mins or not all_maxs:
        suggestion['reasoning'] = 'No valid data found in file'
        return suggestion

    data_min = min(all_mins)
    data_max = max(all_maxs)

    # Type-specific suggestions
    if dtype == 'uint8':
        if data_min > 0:
            suggestion['value'] = 0
            suggestion['reasoning'] = 'Using 0 (data starts at {})'.format(data_min)
        elif data_max < 255:
            suggestion['value'] = 255
            suggestion['reasoning'] = 'Using 255 (data ends at {})'.format(data_max)
        else:
            # Find unused value
            suggestion['value'] = find_unused_value(bands, 0, 255)
            suggestion['reasoning'] = 'Found unused value in data range'
        suggestion['alternatives'] = [0, 255]

    elif dtype == 'uint16':
        if data_min > 0:
            suggestion['value'] = 0
            suggestion['reasoning'] = 'Using 0 (data starts at {})'.format(data_min)
        elif data_max < 65535:
            suggestion['value'] = 65535
            suggestion['reasoning'] = 'Using 65535 (data ends at {})'.format(data_max)
        else:
            suggestion['value'] = find_unused_value(bands, 0, 65535)
            suggestion['reasoning'] = 'Found unused value in data range'
        suggestion['alternatives'] = [0, 65535]

    elif dtype == 'int8':
        if data_min > -128:
            suggestion['value'] = -128
            suggestion['reasoning'] = 'Using minimum int8 value'
        elif data_max < 127:
            suggestion['value'] = 127
            suggestion['reasoning'] = 'Using maximum int8 value'
        else:
            suggestion['value'] = find_unused_value(bands, -128, 127)
            suggestion['reasoning'] = 'Found unused value in data range'
        suggestion['alternatives'] = [-128, 127, 0]

    elif dtype == 'int16':
        if data_min > -32768:
            suggestion['value'] = -32768
            suggestion['reasoning'] = 'Using minimum int16 value'
        elif data_max < 32767 and data_min > -9999:
            suggestion['value'] = -9999
            suggestion['reasoning'] = 'Using standard no-data value -9999'
        else:
            suggestion['value'] = find_unused_value(bands, -32768, 32767)
            suggestion['reasoning'] = 'Found unused value in data range'
        suggestion['alternatives'] = [-32768, -9999, 32767]

    elif 'float' in dtype:
        # For float types, prefer values outside data range
        if data_min > -9999:
            suggestion['value'] = -9999
            suggestion['reasoning'] = 'Using -9999 (outside data range)'
        elif data_min > -99999:
            suggestion['value'] = -99999
            suggestion['reasoning'] = 'Using -99999 (outside data range)'
        else:
            # Use NaN for float types as last resort
            suggestion['value'] = float('nan')
            suggestion['reasoning'] = 'Using NaN for float type'
        suggestion['alternatives'] = [-9999, -99999, float('nan')]

    else:
        # Default for other types
        suggestion['value'] = -9999
        suggestion['reasoning'] = 'Using default -9999'
        suggestion['alternatives'] = [-9999, 0]

    return suggestion


def find_unused_value(bands: List[Dict], min_val: int, max_val: int) -> Optional[int]:
    """
    Find an unused integer value within the specified range.

    Args:
        bands: List of band statistics
        min_val: Minimum possible value
        max_val: Maximum possible value

    Returns:
        Unused value or None
    """
    # Collect unique values if available
    all_uniques = set()
    for band in bands:
        if 'unique_values' in band:
            all_uniques.update(band['unique_values'])

    # Try common no-data values first
    common_values = [0, -9999, -1, 255, 65535, -32768, 32767]
    for val in common_values:
        if min_val <= val <= max_val and val not in all_uniques:
            return val

    # If no common value works, try to find any unused value
    if all_uniques:
        for val in range(min_val, min(max_val + 1, min_val + 1000)):
            if val not in all_uniques:
                return val

    return None


def check_nodata_conflicts(bands: List[Dict], nodata_value: float) -> Dict:
    """
    Check if the no-data value conflicts with actual data.

    Args:
        bands: List of band statistics
        nodata_value: No-data value to check

    Returns:
        Dictionary with conflict information
    """
    conflicts = {
        'has_conflicts': False,
        'conflicting_bands': [],
        'details': []
    }

    for band in bands:
        stats = band['statistics']
        if stats['min'] is not None and stats['max'] is not None:
            # Check if no-data falls within data range
            if stats['min'] <= nodata_value <= stats['max']:
                conflicts['has_conflicts'] = True
                conflicts['conflicting_bands'].append(band['band'])
                conflicts['details'].append(
                    f"Band {band['band']}: no-data {nodata_value} is within data range [{stats['min']:.2f}, {stats['max']:.2f}]"
                )

    return conflicts


def validate_nodata_value(dtype: str, nodata_value: float) -> Dict:
    """
    Validate if a no-data value is appropriate for the data type.

    Args:
        dtype: Data type string
        nodata_value: No-data value to validate

    Returns:
        Dictionary with validation results
    """
    validation = {
        'valid': True,
        'warnings': [],
        'errors': []
    }

    # Check type compatibility
    if dtype == 'uint8':
        if not (0 <= nodata_value <= 255):
            validation['valid'] = False
            validation['errors'].append(f"No-data {nodata_value} out of range for uint8 [0, 255]")

    elif dtype == 'uint16':
        if not (0 <= nodata_value <= 65535):
            validation['valid'] = False
            validation['errors'].append(f"No-data {nodata_value} out of range for uint16 [0, 65535]")

    elif dtype == 'int8':
        if not (-128 <= nodata_value <= 127):
            validation['valid'] = False
            validation['errors'].append(f"No-data {nodata_value} out of range for int8 [-128, 127]")

    elif dtype == 'int16':
        if not (-32768 <= nodata_value <= 32767):
            validation['valid'] = False
            validation['errors'].append(f"No-data {nodata_value} out of range for int16 [-32768, 32767]")

    elif dtype == 'int32':
        if not (-2147483648 <= nodata_value <= 2147483647):
            validation['valid'] = False
            validation['errors'].append(f"No-data {nodata_value} out of range for int32")

    # Add warnings for common issues
    if dtype in ['uint8', 'uint16'] and nodata_value not in [0, 255, 65535]:
        validation['warnings'].append(f"Consider using 0 or max value for unsigned types")

    return validation


def analyze_s3_geotiff(bucket: str, key: str, sample_size: int = None) -> Dict:
    """
    Analyze a GeoTIFF file from S3.

    Args:
        bucket: S3 bucket name
        key: S3 key path
        sample_size: Optional sampling size

    Returns:
        Analysis results dictionary
    """
    # Initialize S3 client
    s3_client = boto3.client('s3')

    # Get file size
    response = s3_client.head_object(Bucket=bucket, Key=key)
    file_size_mb = response['ContentLength'] / (1024 * 1024)

    # Download to temp file for analysis in local directory
    temp_dir = os.environ.get('COG_TEMP_DIR', '/tmp')
    os.makedirs(temp_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.tif', delete=False, dir=temp_dir) as tmp_file:
        try:
            print(f"Downloading {key} for analysis...")
            s3_client.download_file(bucket, key, tmp_file.name)

            # Analyze the downloaded file
            results = analyze_geotiff(tmp_file.name, sample_size)
            results['file'] = f"s3://{bucket}/{key}"
            results['file_size_mb'] = file_size_mb

        finally:
            # Clean up temp file
            os.unlink(tmp_file.name)

    return results


def format_analysis_report(results: Dict) -> str:
    """
    Format analysis results into a readable report.

    Args:
        results: Analysis results dictionary

    Returns:
        Formatted report string
    """
    report = []
    report.append("=" * 80)
    report.append("GEOTIFF ANALYSIS REPORT")
    report.append("=" * 80)

    # File info
    report.append(f"\nFile: {results['file']}")
    report.append(f"Size: {results['file_size_mb']:.2f} MB")
    report.append(f"Dimensions: {results['width']} x {results['height']}")
    report.append(f"Bands: {results['band_count']}")
    report.append(f"Data Type: {results['dtype']}")
    report.append(f"CRS: {results['crs']}")
    report.append(f"Current No-data: {results['nodata_current']}")

    # Band statistics
    report.append("\n" + "-" * 40)
    report.append("BAND STATISTICS")
    report.append("-" * 40)

    for band in results['bands']:
        stats = band['statistics']
        report.append(f"\nBand {band['band']}:")
        if stats['min'] is not None:
            report.append(f"  Min: {stats['min']:.6f}")
            report.append(f"  Max: {stats['max']:.6f}")
            report.append(f"  Mean: {stats['mean']:.6f}")
            report.append(f"  Std Dev: {stats['std']:.6f}")
            report.append(f"  Valid Pixels: {stats['valid_pixels']:,} / {stats['total_pixels']:,}")
        else:
            report.append("  No valid data")

        if band['nodata_percentage'] > 0:
            report.append(f"  No-data pixels: {band['nodata_percentage']:.2f}%")

    # No-data suggestion
    report.append("\n" + "-" * 40)
    report.append("NO-DATA RECOMMENDATION")
    report.append("-" * 40)

    suggestion = results['suggested_nodata']
    report.append(f"\nSuggested Value: {suggestion['value']}")
    report.append(f"Reasoning: {suggestion['reasoning']}")
    if suggestion['alternatives']:
        report.append(f"Alternatives: {suggestion['alternatives']}")

    # Conflicts
    if 'nodata_conflicts' in results and results['nodata_conflicts']['has_conflicts']:
        report.append("\n⚠️  WARNING: Current no-data value conflicts with actual data!")
        for detail in results['nodata_conflicts']['details']:
            report.append(f"  {detail}")

    report.append("\n" + "=" * 80)

    return "\n".join(report)


def main():
    """Command-line interface for the GeoTIFF analyzer."""
    parser = argparse.ArgumentParser(
        description='Analyze GeoTIFF files for min/max values and no-data configuration'
    )
    parser.add_argument('input', help='Input file path or S3 URI (s3://bucket/key)')
    parser.add_argument('--sample-size', type=int, default=1000000,
                       help='Number of pixels to sample for large files (default: 1000000)')
    parser.add_argument('--json', action='store_true',
                       help='Output results as JSON')
    parser.add_argument('--validate-nodata', type=float,
                       help='Validate a specific no-data value')

    args = parser.parse_args()

    # Determine if input is S3 or local
    if args.input.startswith('s3://'):
        # Parse S3 URI
        parts = args.input.replace('s3://', '').split('/', 1)
        if len(parts) != 2:
            print("Error: Invalid S3 URI format. Use s3://bucket/key")
            sys.exit(1)

        bucket, key = parts
        results = analyze_s3_geotiff(bucket, key, args.sample_size)
    else:
        # Local file
        if not os.path.exists(args.input):
            print(f"Error: File not found: {args.input}")
            sys.exit(1)

        results = analyze_geotiff(args.input, args.sample_size)

    # Validate specific no-data if requested
    if args.validate_nodata is not None:
        validation = validate_nodata_value(results['dtype'], args.validate_nodata)
        results['validation'] = validation

    # Output results
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(format_analysis_report(results))

        if 'validation' in results:
            print("\nNO-DATA VALIDATION:")
            print(f"Value: {args.validate_nodata}")
            print(f"Valid: {results['validation']['valid']}")
            if results['validation']['errors']:
                print("Errors:")
                for error in results['validation']['errors']:
                    print(f"  ❌ {error}")
            if results['validation']['warnings']:
                print("Warnings:")
                for warning in results['validation']['warnings']:
                    print(f"  ⚠️  {warning}")


if __name__ == '__main__':
    main()