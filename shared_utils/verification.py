#!/usr/bin/env python3
"""
Verification and Visualization Module - Verify COG transformation accuracy.
Compares input and output files to ensure data integrity and proper no-data handling.
"""

import os
import sys
import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
import seaborn as sns
import tempfile
from datetime import datetime
import json
from typing import Dict, Tuple, Optional, List
import boto3
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_utils.s3_operations import initialize_s3_client


def download_s3_file(bucket: str, key: str, s3_client=None) -> str:
    """
    Download an S3 file to a temporary location.

    Args:
        bucket: S3 bucket name
        key: S3 key
        s3_client: Optional S3 client

    Returns:
        Path to downloaded file
    """
    if s3_client is None:
        s3_client = boto3.client('s3')

    # Create temp file in /tmp
    temp_dir = os.environ.get('COG_TEMP_DIR', '/tmp')
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(suffix='.tif', delete=False, dir=temp_dir)
    temp_path = temp_file.name
    temp_file.close()

    # Download
    s3_client.download_file(bucket, key, temp_path)

    return temp_path


def compare_geotiffs(input_path: str, output_path: str, band: int = 1,
                    sample_size: Optional[int] = None) -> Dict:
    """
    Compare two GeoTIFF files for verification.

    Args:
        input_path: Path to input file
        output_path: Path to output (COG) file
        band: Band number to compare
        sample_size: Optional sampling for large files

    Returns:
        Dictionary with comparison results
    """
    results = {
        'input_file': input_path,
        'output_file': output_path,
        'band': band,
        'timestamp': datetime.now().isoformat()
    }

    with rasterio.open(input_path) as src_in, rasterio.open(output_path) as src_out:
        # File metadata comparison
        results['input_metadata'] = {
            'width': src_in.width,
            'height': src_in.height,
            'dtype': str(src_in.dtypes[0]),
            'crs': str(src_in.crs),
            'nodata': src_in.nodata,
            'compression': src_in.compression,
            'bands': src_in.count
        }

        results['output_metadata'] = {
            'width': src_out.width,
            'height': src_out.height,
            'dtype': str(src_out.dtypes[0]),
            'crs': str(src_out.crs),
            'nodata': src_out.nodata,
            'compression': src_out.compression,
            'bands': src_out.count,
            'is_tiled': src_out.is_tiled,
            'blockxsize': src_out.block_shapes[0][1] if src_out.is_tiled else None,
            'blockysize': src_out.block_shapes[0][0] if src_out.is_tiled else None
        }

        # Read data for comparison
        if sample_size and (src_in.width * src_in.height) > sample_size:
            # Sample for large files
            data_in, data_out = sample_data_for_comparison(src_in, src_out, band, sample_size)
        else:
            # Read full band
            data_in = src_in.read(band)
            data_out = src_out.read(band)

        # Calculate statistics
        results['statistics'] = calculate_comparison_statistics(
            data_in, data_out,
            src_in.nodata, src_out.nodata
        )

        # Check data integrity
        results['verification'] = verify_data_integrity(
            data_in, data_out,
            src_in.nodata, src_out.nodata
        )

    return results


def sample_data_for_comparison(src_in, src_out, band: int, sample_size: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Sample data from both files for efficient comparison.
    Handles files with different dimensions (e.g., after reprojection).

    Args:
        src_in: Input rasterio dataset
        src_out: Output rasterio dataset
        band: Band to sample
        sample_size: Number of pixels to sample

    Returns:
        Tuple of sampled arrays
    """
    # For files with different dimensions (reprojected), just sample from each independently
    if src_in.shape != src_out.shape:
        # Sample input file
        total_pixels_in = src_in.width * src_in.height
        interval_in = max(1, total_pixels_in // sample_size)
        sampled_in = []

        for i in range(0, src_in.height, int(np.sqrt(interval_in))):
            for j in range(0, src_in.width, int(np.sqrt(interval_in))):
                window = Window(j, i, min(10, src_in.width - j), min(10, src_in.height - i))
                sampled_in.append(src_in.read(band, window=window))

        # Sample output file independently
        total_pixels_out = src_out.width * src_out.height
        interval_out = max(1, total_pixels_out // sample_size)
        sampled_out = []

        for i in range(0, src_out.height, int(np.sqrt(interval_out))):
            for j in range(0, src_out.width, int(np.sqrt(interval_out))):
                window = Window(j, i, min(10, src_out.width - j), min(10, src_out.height - i))
                sampled_out.append(src_out.read(band, window=window))

        return np.concatenate([s.flatten() for s in sampled_in]), \
               np.concatenate([s.flatten() for s in sampled_out])
    else:
        # For same-dimension files, sample the same locations
        total_pixels = src_in.width * src_in.height
        interval = max(1, total_pixels // sample_size)

        sampled_in = []
        sampled_out = []

        for i in range(0, src_in.height, int(np.sqrt(interval))):
            for j in range(0, src_in.width, int(np.sqrt(interval))):
                window = Window(j, i, min(10, src_in.width - j), min(10, src_in.height - i))
                sampled_in.append(src_in.read(band, window=window))
                sampled_out.append(src_out.read(band, window=window))

        return np.concatenate([s.flatten() for s in sampled_in]), \
               np.concatenate([s.flatten() for s in sampled_out])


def calculate_comparison_statistics(data_in: np.ndarray, data_out: np.ndarray,
                                   nodata_in: Optional[float], nodata_out: Optional[float]) -> Dict:
    """
    Calculate detailed statistics for comparison.

    Args:
        data_in: Input data array
        data_out: Output data array
        nodata_in: Input no-data value
        nodata_out: Output no-data value

    Returns:
        Dictionary with statistics
    """
    stats = {}

    # Handle no-data
    if nodata_in is not None:
        valid_in = data_in[data_in != nodata_in]
    else:
        valid_in = data_in.flatten()

    if nodata_out is not None:
        valid_out = data_out[data_out != nodata_out]
    else:
        valid_out = data_out.flatten()

    # Input statistics
    if valid_in.size > 0:
        stats['input'] = {
            'min': float(np.min(valid_in)),
            'max': float(np.max(valid_in)),
            'mean': float(np.mean(valid_in)),
            'std': float(np.std(valid_in)),
            'median': float(np.median(valid_in)),
            'valid_pixels': int(valid_in.size),
            'nodata_pixels': int(data_in.size - valid_in.size)
        }
    else:
        stats['input'] = {'valid_pixels': 0, 'nodata_pixels': int(data_in.size)}

    # Output statistics
    if valid_out.size > 0:
        stats['output'] = {
            'min': float(np.min(valid_out)),
            'max': float(np.max(valid_out)),
            'mean': float(np.mean(valid_out)),
            'std': float(np.std(valid_out)),
            'median': float(np.median(valid_out)),
            'valid_pixels': int(valid_out.size),
            'nodata_pixels': int(data_out.size - valid_out.size)
        }
    else:
        stats['output'] = {'valid_pixels': 0, 'nodata_pixels': int(data_out.size)}

    # Comparison metrics
    if valid_in.size > 0 and valid_out.size > 0:
        stats['differences'] = {
            'min_diff': float(stats['output']['min'] - stats['input']['min']),
            'max_diff': float(stats['output']['max'] - stats['input']['max']),
            'mean_diff': float(stats['output']['mean'] - stats['input']['mean']),
            'median_diff': float(stats['output']['median'] - stats['input']['median']),
            'valid_pixel_diff': int(valid_out.size - valid_in.size)
        }

        # Calculate correlation if sizes match
        if valid_in.size == valid_out.size:
            stats['correlation'] = float(np.corrcoef(valid_in.flatten(), valid_out.flatten())[0, 1])

    return stats


def analyze_reprojected_files(data_in: np.ndarray, data_out: np.ndarray,
                             nodata_in: Optional[float], nodata_out: Optional[float],
                             verification: Dict) -> Dict:
    """
    Analyze reprojected files where direct pixel comparison isn't possible.

    Args:
        data_in: Input data
        data_out: Output data (reprojected)
        nodata_in: Input no-data value
        nodata_out: Output no-data value
        verification: Existing verification dict

    Returns:
        Updated verification results
    """
    # Filter out no-data values
    if nodata_in is not None:
        valid_in = data_in[data_in != nodata_in]
    else:
        valid_in = data_in.flatten()

    if nodata_out is not None:
        valid_out = data_out[data_out != nodata_out]
    else:
        valid_out = data_out.flatten()

    if valid_in.size > 0 and valid_out.size > 0:
        # Compare statistical properties
        stats_in = {
            'min': np.min(valid_in),
            'max': np.max(valid_in),
            'mean': np.mean(valid_in),
            'std': np.std(valid_in)
        }

        stats_out = {
            'min': np.min(valid_out),
            'max': np.max(valid_out),
            'mean': np.mean(valid_out),
            'std': np.std(valid_out)
        }

        # Check for reasonable preservation of data range
        range_in = stats_in['max'] - stats_in['min']
        range_out = stats_out['max'] - stats_out['min']

        # Allow for some variation due to reprojection interpolation
        if abs(range_out - range_in) > 0.1 * range_in:  # More than 10% change
            verification['warnings'].append(
                f"Data range changed significantly: {range_in:.6f} -> {range_out:.6f}"
            )

        # Check for data corruption
        if np.any(np.isnan(valid_out)):
            verification['errors'].append("NaN values found in output")
            verification['passed'] = False

        if np.any(np.isinf(valid_out)):
            verification['errors'].append("Infinite values found in output")
            verification['passed'] = False

        # Add statistics to verification for reporting
        verification['statistics'] = {
            'input': stats_in,
            'output': stats_out
        }
    else:
        verification['warnings'].append("No valid data found for comparison")

    # Update status based on errors
    if verification['errors']:
        verification['status'] = 'FAILED'
        verification['passed'] = False
    else:
        verification['status'] = 'PASSED'

    return verification


def verify_data_integrity(data_in: np.ndarray, data_out: np.ndarray,
                         nodata_in: Optional[float], nodata_out: Optional[float]) -> Dict:
    """
    Verify data integrity between input and output.

    Args:
        data_in: Input data
        data_out: Output data
        nodata_in: Input no-data value
        nodata_out: Output no-data value

    Returns:
        Verification results
    """
    verification = {
        'passed': True,
        'status': 'PASSED',  # Initialize status field
        'warnings': [],
        'errors': []
    }

    # Check dimensions - Note: shapes may differ after reprojection
    if data_in.shape != data_out.shape:
        # This is expected for reprojected files, just add a warning
        verification['warnings'].append(f"Shape changed after reprojection: {data_in.shape} -> {data_out.shape}")
        # Don't fail verification for shape mismatch as it's expected with reprojection
        # Instead, we'll compare statistics rather than pixel-by-pixel
        verification['reprojected'] = True

        # For reprojected files, we can't do direct pixel comparison
        # So we'll compare overall statistics instead
        return analyze_reprojected_files(data_in, data_out, nodata_in, nodata_out, verification)

    # Check no-data handling
    if nodata_in is not None and nodata_out is not None:
        input_nodata_mask = (data_in == nodata_in)
        output_nodata_mask = (data_out == nodata_out)

        # Check if no-data regions are preserved
        nodata_preserved = np.sum(input_nodata_mask) == np.sum(output_nodata_mask)
        if not nodata_preserved:
            diff = np.sum(output_nodata_mask) - np.sum(input_nodata_mask)
            verification['warnings'].append(f"No-data pixel count changed by {diff}")

    # Check value ranges
    if nodata_in is not None:
        valid_in = data_in[data_in != nodata_in]
    else:
        valid_in = data_in

    if nodata_out is not None:
        valid_out = data_out[data_out != nodata_out]
    else:
        valid_out = data_out

    if valid_in.size > 0 and valid_out.size > 0:
        # Check for significant changes
        range_in = np.max(valid_in) - np.min(valid_in)
        range_out = np.max(valid_out) - np.min(valid_out)

        if abs(range_out - range_in) > 0.01 * range_in:  # More than 1% change
            verification['warnings'].append(
                f"Data range changed: {range_in:.6f} -> {range_out:.6f}"
            )

        # Check for data corruption (NaN or Inf)
        if np.any(np.isnan(valid_out)) and not np.any(np.isnan(valid_in)):
            verification['errors'].append("NaN values introduced in output")
            verification['passed'] = False

        if np.any(np.isinf(valid_out)) and not np.any(np.isinf(valid_in)):
            verification['errors'].append("Infinite values introduced in output")
            verification['passed'] = False

    # Overall assessment
    if not verification['errors']:
        verification['status'] = 'PASSED'
    else:
        verification['status'] = 'FAILED'

    return verification


def create_comparison_plots(input_path: str, output_path: str,
                          comparison_results: Dict,
                          save_dir: str, band: int = 1) -> List[str]:
    """
    Create visualization plots comparing input and output.

    Args:
        input_path: Path to input file
        output_path: Path to output file
        comparison_results: Results from compare_geotiffs
        save_dir: Directory to save plots
        band: Band to visualize

    Returns:
        List of saved plot paths
    """
    os.makedirs(save_dir, exist_ok=True)
    saved_plots = []

    # Set style
    plt.style.use('seaborn-v0_8-darkgrid')

    with rasterio.open(input_path) as src_in, rasterio.open(output_path) as src_out:
        # Read subset for visualization (max 1000x1000)
        # Handle different dimensions for reprojected files
        h_in = min(1000, src_in.height)
        w_in = min(1000, src_in.width)
        h_out = min(1000, src_out.height)
        w_out = min(1000, src_out.width)

        data_in = src_in.read(band, window=Window(0, 0, w_in, h_in))
        data_out = src_out.read(band, window=Window(0, 0, w_out, h_out))

        # 1. Side-by-side comparison plot
        fig, axes = plt.subplots(1, 3, figsize=(18, 6))

        # Prepare data for display (handle no-data)
        nodata_in = src_in.nodata
        nodata_out = src_out.nodata

        # Mask no-data for better visualization
        if nodata_in is not None:
            data_in_masked = np.ma.masked_equal(data_in, nodata_in)
        else:
            data_in_masked = data_in

        if nodata_out is not None:
            data_out_masked = np.ma.masked_equal(data_out, nodata_out)
        else:
            data_out_masked = data_out

        # Calculate common range for consistent coloring
        vmin = min(np.percentile(data_in_masked.compressed(), 2) if data_in_masked.compressed().size > 0 else 0,
                   np.percentile(data_out_masked.compressed(), 2) if data_out_masked.compressed().size > 0 else 0)
        vmax = max(np.percentile(data_in_masked.compressed(), 98) if data_in_masked.compressed().size > 0 else 1,
                   np.percentile(data_out_masked.compressed(), 98) if data_out_masked.compressed().size > 0 else 1)

        # Input image
        im1 = axes[0].imshow(data_in_masked, cmap='viridis', vmin=vmin, vmax=vmax)
        axes[0].set_title('Input (Original)', fontsize=12, weight='bold')
        axes[0].set_xlabel(f'No-data: {nodata_in}')
        axes[0].axis('off')

        # Output image
        im2 = axes[1].imshow(data_out_masked, cmap='viridis', vmin=vmin, vmax=vmax)
        axes[1].set_title('Output (COG)', fontsize=12, weight='bold')
        axes[1].set_xlabel(f'No-data: {nodata_out}')
        axes[1].axis('off')

        # Difference map
        if data_in_masked.shape == data_out_masked.shape:
            # Calculate difference only for valid pixels
            diff = np.zeros_like(data_in, dtype=np.float32)
            valid_mask = ~data_in_masked.mask & ~data_out_masked.mask
            diff[valid_mask] = data_out[valid_mask].astype(np.float32) - data_in[valid_mask].astype(np.float32)
            diff_masked = np.ma.masked_where(~valid_mask, diff)

            im3 = axes[2].imshow(diff_masked, cmap='RdBu_r', vmin=-np.abs(diff[valid_mask]).max() if valid_mask.any() else -1,
                                vmax=np.abs(diff[valid_mask]).max() if valid_mask.any() else 1)
            axes[2].set_title('Difference (Output - Input)', fontsize=12, weight='bold')
            axes[2].axis('off')
            plt.colorbar(im3, ax=axes[2], fraction=0.046, pad=0.04)
        else:
            # Different shapes (expected after reprojection)
            axes[2].text(0.5, 0.5,
                        f'Reprojected\nInput: {data_in_masked.shape}\nOutput: {data_out_masked.shape}',
                        ha='center', va='center', fontsize=12)
            axes[2].set_title('Difference (N/A - Reprojected)', fontsize=12, weight='bold')
            axes[2].axis('off')

        # Add colorbar for data plots
        plt.colorbar(im1, ax=axes[:2], fraction=0.046, pad=0.04)

        # Add overall title
        filename = os.path.basename(input_path)
        fig.suptitle(f'Verification: {filename}', fontsize=14, weight='bold')

        # Save plot
        plot_path = os.path.join(save_dir, f'comparison_{os.path.splitext(filename)[0]}.png')
        plt.tight_layout()
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        plt.close()
        saved_plots.append(plot_path)

        # 2. Statistics comparison plot
        fig, axes = plt.subplots(2, 2, figsize=(12, 10))

        stats = comparison_results['statistics']

        # Min/Max comparison
        if 'input' in stats and 'output' in stats:
            if stats['input'].get('min') is not None and stats['output'].get('min') is not None:
                categories = ['Min', 'Max', 'Mean', 'Median']
                input_values = [stats['input']['min'], stats['input']['max'],
                              stats['input']['mean'], stats['input']['median']]
                output_values = [stats['output']['min'], stats['output']['max'],
                               stats['output']['mean'], stats['output']['median']]

                x = np.arange(len(categories))
                width = 0.35

                axes[0, 0].bar(x - width/2, input_values, width, label='Input', color='steelblue')
                axes[0, 0].bar(x + width/2, output_values, width, label='Output', color='coral')
                axes[0, 0].set_xlabel('Statistic')
                axes[0, 0].set_ylabel('Value')
                axes[0, 0].set_title('Statistical Comparison')
                axes[0, 0].set_xticks(x)
                axes[0, 0].set_xticklabels(categories)
                axes[0, 0].legend()
                axes[0, 0].grid(True, alpha=0.3)

        # Histogram comparison
        if data_in_masked.compressed().size > 0 and data_out_masked.compressed().size > 0:
            axes[0, 1].hist(data_in_masked.compressed(), bins=50, alpha=0.7,
                           label='Input', color='steelblue', density=True)
            axes[0, 1].hist(data_out_masked.compressed(), bins=50, alpha=0.7,
                           label='Output', color='coral', density=True)
            axes[0, 1].set_xlabel('Pixel Value')
            axes[0, 1].set_ylabel('Density')
            axes[0, 1].set_title('Value Distribution')
            axes[0, 1].legend()
            axes[0, 1].grid(True, alpha=0.3)

        # No-data analysis
        if 'input' in stats and 'output' in stats:
            nodata_data = [stats['input'].get('nodata_pixels', 0),
                          stats['output'].get('nodata_pixels', 0)]
            valid_data = [stats['input'].get('valid_pixels', 0),
                         stats['output'].get('valid_pixels', 0)]

            x = ['Input', 'Output']
            width = 0.35

            axes[1, 0].bar(x, valid_data, width, label='Valid', color='green', alpha=0.7)
            axes[1, 0].bar(x, nodata_data, width, bottom=valid_data, label='No-data', color='red', alpha=0.7)
            axes[1, 0].set_ylabel('Pixel Count')
            axes[1, 0].set_title('Valid vs No-data Pixels')
            axes[1, 0].legend()
            axes[1, 0].grid(True, alpha=0.3)

        # Verification status
        verification = comparison_results['verification']
        status_text = f"Status: {verification['status']}\n"
        if verification['warnings']:
            status_text += "\nWarnings:\n"
            for warning in verification['warnings'][:3]:
                status_text += f"• {warning}\n"
        if verification['errors']:
            status_text += "\nErrors:\n"
            for error in verification['errors'][:3]:
                status_text += f"• {error}\n"

        axes[1, 1].text(0.1, 0.5, status_text, fontsize=11, verticalalignment='center',
                       transform=axes[1, 1].transAxes)
        axes[1, 1].set_title('Verification Results')
        axes[1, 1].axis('off')

        # Add verification badge
        if verification['status'] == 'PASSED':
            axes[1, 1].add_patch(mpatches.Rectangle((0.7, 0.4), 0.25, 0.2,
                                                   transform=axes[1, 1].transAxes,
                                                   facecolor='green', alpha=0.3))
            axes[1, 1].text(0.825, 0.5, '✓ PASSED', fontsize=14, weight='bold',
                          color='green', ha='center', va='center',
                          transform=axes[1, 1].transAxes)
        else:
            axes[1, 1].add_patch(mpatches.Rectangle((0.7, 0.4), 0.25, 0.2,
                                                   transform=axes[1, 1].transAxes,
                                                   facecolor='red', alpha=0.3))
            axes[1, 1].text(0.825, 0.5, '✗ FAILED', fontsize=14, weight='bold',
                          color='red', ha='center', va='center',
                          transform=axes[1, 1].transAxes)

        # Overall title
        fig.suptitle(f'Statistical Analysis: {filename}', fontsize=14, weight='bold')

        # Save plot
        stats_plot_path = os.path.join(save_dir, f'statistics_{os.path.splitext(filename)[0]}.png')
        plt.tight_layout()
        plt.savefig(stats_plot_path, dpi=150, bbox_inches='tight')
        plt.close()
        saved_plots.append(stats_plot_path)

    return saved_plots


def verify_s3_files(input_bucket: str, input_key: str,
                   output_bucket: str, output_key: str,
                   save_dir: str, s3_client=None) -> Dict:
    """
    Verify S3 files by downloading and comparing them.

    Args:
        input_bucket: Input file bucket
        input_key: Input file key
        output_bucket: Output file bucket
        output_key: Output file key
        save_dir: Directory to save results
        s3_client: Optional S3 client

    Returns:
        Verification results
    """
    # Download files
    print(f"Downloading files for verification...")
    input_path = download_s3_file(input_bucket, input_key, s3_client)
    output_path = download_s3_file(output_bucket, output_key, s3_client)

    try:
        # Compare files
        print(f"Comparing files...")
        results = compare_geotiffs(input_path, output_path)

        # Create plots
        print(f"Creating visualization plots...")
        plot_paths = create_comparison_plots(input_path, output_path, results, save_dir)
        results['plots'] = plot_paths

        # Save results JSON
        results_path = os.path.join(save_dir, f'verification_{os.path.basename(input_key)}.json')
        with open(results_path, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        results['results_file'] = results_path

        print(f"✅ Verification complete. Results saved to {save_dir}")

    finally:
        # Clean up temp files
        os.unlink(input_path)
        os.unlink(output_path)

    return results


def create_verification_report(verification_results: List[Dict], save_path: str):
    """
    Create a summary report from multiple verification results.

    Args:
        verification_results: List of verification results
        save_path: Path to save report
    """
    report = {
        'timestamp': datetime.now().isoformat(),
        'total_files': len(verification_results),
        'passed': 0,
        'failed': 0,
        'warnings': [],
        'errors': [],
        'files': []
    }

    for result in verification_results:
        # Defensive error handling for missing keys
        try:
            # Check if verification exists and has required fields
            if 'verification' in result:
                verification = result['verification']
                status = verification.get('status', 'UNKNOWN')
                warnings = verification.get('warnings', [])
                errors = verification.get('errors', [])
            else:
                # Handle case where verification is missing
                status = 'ERROR'
                warnings = []
                errors = ['Verification data missing']

            file_info = {
                'input': result.get('input_file', 'Unknown'),
                'output': result.get('output_file', 'Unknown'),
                'status': status,
                'warnings': warnings,
                'errors': errors
            }

            # Add statistics if available (for reprojected files)
            if 'verification' in result and 'statistics' in result['verification']:
                file_info['statistics'] = result['verification']['statistics']

            report['files'].append(file_info)

            if status == 'PASSED':
                report['passed'] += 1
            else:
                report['failed'] += 1

            report['warnings'].extend(warnings)
            report['errors'].extend(errors)

        except Exception as e:
            # Handle any unexpected errors gracefully
            print(f"Warning: Error processing verification result: {e}")
            report['files'].append({
                'input': result.get('input_file', 'Unknown'),
                'output': result.get('output_file', 'Unknown'),
                'status': 'ERROR',
                'warnings': [],
                'errors': [f'Error processing result: {str(e)}']
            })
            report['failed'] += 1

    # Remove duplicates
    report['warnings'] = list(set(report['warnings']))
    report['errors'] = list(set(report['errors']))

    # Save report
    with open(save_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    # Print summary
    print("\n" + "=" * 60)
    print("VERIFICATION REPORT SUMMARY")
    print("=" * 60)
    print(f"Total Files: {report['total_files']}")
    print(f"Passed: {report['passed']}")
    print(f"Failed: {report['failed']}")
    print(f"Success Rate: {(report['passed'] / report['total_files'] * 100):.1f}%")

    if report['warnings']:
        print("\nCommon Warnings:")
        for warning in report['warnings'][:5]:
            print(f"  • {warning}")

    if report['errors']:
        print("\nCommon Errors:")
        for error in report['errors'][:5]:
            print(f"  • {error}")

    print(f"\nFull report saved to: {save_path}")
    print("=" * 60)