#!/usr/bin/env python3
"""
Batch GeoTIFF Analyzer - Analyze multiple GeoTIFF files and generate reports.
"""

import os
import sys
import argparse
import glob
import pandas as pd
from pathlib import Path
from typing import List, Dict
import json
from datetime import datetime
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_utils.geotiff_analyzer import analyze_geotiff, analyze_s3_geotiff
from shared_utils.s3_operations import initialize_s3_client, list_s3_files


def analyze_batch_local(file_paths: List[str], max_workers: int = 4) -> List[Dict]:
    """
    Analyze multiple local GeoTIFF files in parallel.

    Args:
        file_paths: List of file paths
        max_workers: Number of parallel workers

    Returns:
        List of analysis results
    """
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_file = {
            executor.submit(analyze_geotiff, file_path): file_path
            for file_path in file_paths
        }

        # Process results with progress bar
        with tqdm(total=len(file_paths), desc="Analyzing files") as pbar:
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"Error analyzing {file_path}: {e}")
                    results.append({
                        'file': file_path,
                        'error': str(e)
                    })
                pbar.update(1)

    return results


def analyze_batch_s3(bucket: str, prefix: str, suffix: str = '.tif',
                    max_workers: int = 4, limit: int = None) -> List[Dict]:
    """
    Analyze multiple S3 GeoTIFF files.

    Args:
        bucket: S3 bucket name
        prefix: S3 prefix path
        suffix: File suffix to filter
        max_workers: Number of parallel workers
        limit: Optional limit on number of files

    Returns:
        List of analysis results
    """
    # Initialize S3 client
    s3_client, _ = initialize_s3_client(bucket)

    # List files
    keys = list_s3_files(s3_client, bucket, prefix, suffix)

    if limit:
        keys = keys[:limit]

    print(f"Found {len(keys)} files to analyze")

    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_key = {
            executor.submit(analyze_s3_geotiff, bucket, key): key
            for key in keys
        }

        # Process results
        with tqdm(total=len(keys), desc="Analyzing S3 files") as pbar:
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"Error analyzing {key}: {e}")
                    results.append({
                        'file': f"s3://{bucket}/{key}",
                        'error': str(e)
                    })
                pbar.update(1)

    return results


def generate_summary_statistics(results: List[Dict]) -> Dict:
    """
    Generate summary statistics from batch analysis results.

    Args:
        results: List of analysis results

    Returns:
        Summary statistics dictionary
    """
    summary = {
        'total_files': len(results),
        'successful': 0,
        'failed': 0,
        'data_types': {},
        'nodata_values': {},
        'min_values': [],
        'max_values': [],
        'file_sizes_mb': [],
        'recommendations': {}
    }

    for result in results:
        if 'error' in result:
            summary['failed'] += 1
            continue

        summary['successful'] += 1

        # Data type statistics
        dtype = result.get('dtype', 'unknown')
        summary['data_types'][dtype] = summary['data_types'].get(dtype, 0) + 1

        # No-data value statistics
        nodata = str(result.get('nodata_current', 'None'))
        summary['nodata_values'][nodata] = summary['nodata_values'].get(nodata, 0) + 1

        # File size
        summary['file_sizes_mb'].append(result.get('file_size_mb', 0))

        # Min/max across all bands
        for band in result.get('bands', []):
            if band['statistics']['min'] is not None:
                summary['min_values'].append(band['statistics']['min'])
                summary['max_values'].append(band['statistics']['max'])

        # Collect recommendations
        if 'suggested_nodata' in result:
            suggested = result['suggested_nodata']['value']
            key = f"{dtype}:{suggested}"
            summary['recommendations'][key] = summary['recommendations'].get(key, 0) + 1

    # Calculate overall statistics
    if summary['min_values']:
        summary['overall_min'] = min(summary['min_values'])
        summary['overall_max'] = max(summary['max_values'])
        summary['avg_file_size_mb'] = sum(summary['file_sizes_mb']) / len(summary['file_sizes_mb'])

    return summary


def create_detailed_report(results: List[Dict]) -> pd.DataFrame:
    """
    Create a detailed DataFrame report from analysis results.

    Args:
        results: List of analysis results

    Returns:
        Pandas DataFrame with detailed information
    """
    rows = []

    for result in results:
        row = {
            'file': result.get('file', ''),
            'size_mb': result.get('file_size_mb', 0),
            'width': result.get('width', 0),
            'height': result.get('height', 0),
            'bands': result.get('band_count', 0),
            'dtype': result.get('dtype', ''),
            'current_nodata': result.get('nodata_current', None),
            'has_error': 'error' in result
        }

        if 'error' in result:
            row['error'] = result['error']
        else:
            # Add min/max for each band
            for i, band in enumerate(result.get('bands', [])):
                if band['statistics']['min'] is not None:
                    row[f'band_{i+1}_min'] = band['statistics']['min']
                    row[f'band_{i+1}_max'] = band['statistics']['max']
                    row[f'band_{i+1}_mean'] = band['statistics']['mean']
                    row[f'band_{i+1}_nodata_pct'] = band['nodata_percentage']

            # Add suggestions
            if 'suggested_nodata' in result:
                row['suggested_nodata'] = result['suggested_nodata']['value']
                row['suggestion_reason'] = result['suggested_nodata']['reasoning']

            # Add conflicts
            if 'nodata_conflicts' in result:
                row['has_nodata_conflict'] = result['nodata_conflicts']['has_conflicts']

        rows.append(row)

    return pd.DataFrame(rows)


def save_reports(results: List[Dict], output_dir: str, prefix: str = 'batch_analysis'):
    """
    Save analysis reports to files.

    Args:
        results: Analysis results
        output_dir: Output directory
        prefix: File prefix
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # Save detailed JSON
    json_path = os.path.join(output_dir, f'{prefix}_{timestamp}.json')
    with open(json_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Saved JSON report: {json_path}")

    # Save CSV report
    df = create_detailed_report(results)
    csv_path = os.path.join(output_dir, f'{prefix}_{timestamp}.csv')
    df.to_csv(csv_path, index=False)
    print(f"Saved CSV report: {csv_path}")

    # Save summary
    summary = generate_summary_statistics(results)
    summary_path = os.path.join(output_dir, f'{prefix}_{timestamp}_summary.txt')

    with open(summary_path, 'w') as f:
        f.write("BATCH ANALYSIS SUMMARY\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Total Files: {summary['total_files']}\n")
        f.write(f"Successful: {summary['successful']}\n")
        f.write(f"Failed: {summary['failed']}\n")
        f.write(f"\nData Types:\n")
        for dtype, count in summary['data_types'].items():
            f.write(f"  {dtype}: {count}\n")
        f.write(f"\nCurrent No-data Values:\n")
        for nodata, count in summary['nodata_values'].items():
            f.write(f"  {nodata}: {count}\n")

        if 'overall_min' in summary:
            f.write(f"\nOverall Statistics:\n")
            f.write(f"  Min Value: {summary['overall_min']:.6f}\n")
            f.write(f"  Max Value: {summary['overall_max']:.6f}\n")
            f.write(f"  Avg File Size: {summary['avg_file_size_mb']:.2f} MB\n")

        f.write(f"\nRecommended No-data Values:\n")
        for key, count in summary['recommendations'].items():
            dtype, value = key.split(':')
            f.write(f"  {dtype} -> {value}: {count} files\n")

    print(f"Saved summary: {summary_path}")


def print_summary(results: List[Dict]):
    """Print a summary of the batch analysis."""
    summary = generate_summary_statistics(results)

    print("\n" + "=" * 60)
    print("BATCH ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"\nTotal Files: {summary['total_files']}")
    print(f"Successful: {summary['successful']}")
    print(f"Failed: {summary['failed']}")

    print("\nData Types:")
    for dtype, count in summary['data_types'].items():
        print(f"  {dtype}: {count}")

    print("\nCurrent No-data Values:")
    for nodata, count in summary['nodata_values'].items():
        print(f"  {nodata}: {count}")

    if 'overall_min' in summary:
        print("\nOverall Statistics:")
        print(f"  Min Value: {summary['overall_min']:.6f}")
        print(f"  Max Value: {summary['overall_max']:.6f}")
        print(f"  Avg File Size: {summary['avg_file_size_mb']:.2f} MB")

    print("\nRecommended No-data Values:")
    for key, count in summary['recommendations'].items():
        dtype, value = key.split(':')
        print(f"  {dtype} -> {value}: {count} files")

    print("\n" + "=" * 60)


def main():
    """Command-line interface for batch analysis."""
    parser = argparse.ArgumentParser(
        description='Batch analyze GeoTIFF files for statistics and no-data configuration'
    )
    parser.add_argument('input', help='Input directory path or S3 URI (s3://bucket/prefix)')
    parser.add_argument('--pattern', default='*.tif',
                       help='File pattern for local files (default: *.tif)')
    parser.add_argument('--output-dir', default='./analysis_reports',
                       help='Output directory for reports (default: ./analysis_reports)')
    parser.add_argument('--workers', type=int, default=4,
                       help='Number of parallel workers (default: 4)')
    parser.add_argument('--limit', type=int,
                       help='Limit number of files to analyze')
    parser.add_argument('--no-save', action='store_true',
                       help='Do not save reports to files')

    args = parser.parse_args()

    # Process input
    if args.input.startswith('s3://'):
        # S3 batch analysis
        parts = args.input.replace('s3://', '').split('/', 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ''

        results = analyze_batch_s3(
            bucket, prefix,
            suffix='.tif',
            max_workers=args.workers,
            limit=args.limit
        )
    else:
        # Local batch analysis
        if not os.path.exists(args.input):
            print(f"Error: Directory not found: {args.input}")
            sys.exit(1)

        # Find files
        pattern = os.path.join(args.input, '**', args.pattern)
        file_paths = glob.glob(pattern, recursive=True)

        if args.limit:
            file_paths = file_paths[:args.limit]

        print(f"Found {len(file_paths)} files matching pattern")

        if not file_paths:
            print("No files found!")
            sys.exit(1)

        results = analyze_batch_local(file_paths, max_workers=args.workers)

    # Print summary
    print_summary(results)

    # Save reports
    if not args.no_save:
        save_reports(results, args.output_dir)


if __name__ == '__main__':
    main()