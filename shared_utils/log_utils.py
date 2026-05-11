"""
Logging module - handles progress tracking and status reporting.
Single responsibility: Logging and progress reporting.
"""

import sys
from datetime import datetime
import pandas as pd


def setup_logger(log_file=None):
    """
    Setup logging configuration.

    Args:
        log_file: Optional log file path

    Returns:
        dict: Logger configuration
    """
    config = {
        'start_time': datetime.now(),
        'log_file': log_file,
        'events': []
    }

    if log_file:
        print(f"   [LOG] Logging to: {log_file}")

    return config


def log_progress(logger, message, level='INFO'):
    """
    Log a progress message.

    Args:
        logger: Logger configuration
        message: Message to log
        level: Log level (INFO, WARNING, ERROR)

    Returns:
        None
    """
    timestamp = datetime.now()
    event = {
        'timestamp': timestamp,
        'level': level,
        'message': message
    }

    # Add to events list
    if logger:
        logger['events'].append(event)

    # Print to console
    print(f"[{level}] {message}")

    # Write to file if configured
    if logger and logger.get('log_file'):
        try:
            with open(logger['log_file'], 'a') as f:
                f.write(f"{timestamp.isoformat()} [{level}] {message}\n")
        except:
            pass


def print_status(title, status_dict):
    """
    Print a formatted status report.

    Args:
        title: Title for the status report
        status_dict: Dictionary of status items

    Returns:
        None
    """
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")

    for key, value in status_dict.items():
        print(f"  {key}: {value}")

    print(f"{'='*60}\n")


def print_summary(results):
    """
    Print processing summary.

    Args:
        results: Processing results (DataFrame or dict)

    Returns:
        None
    """
    print("\n" + "="*60)
    print("PROCESSING SUMMARY")
    print("="*60)

    if isinstance(results, pd.DataFrame):
        if not results.empty:
            # Count by status
            if 'status' in results.columns:
                status_counts = results['status'].value_counts()
                print("\nFiles by Status:")
                for status, count in status_counts.items():
                    print(f"  {status}: {count}")

            # Processing time
            if 'processing_time_s' in results.columns:
                total_time = results['processing_time_s'].sum()
                avg_time = results['processing_time_s'].mean()
                print(f"\nProcessing Time:")
                print(f"  Total: {total_time/60:.1f} minutes")
                print(f"  Average: {avg_time:.1f} seconds per file")

            # Memory usage
            if 'peak_memory_mb' in results.columns:
                max_memory = results['peak_memory_mb'].max()
                avg_memory = results['peak_memory_mb'].mean()
                print(f"\nMemory Usage:")
                print(f"  Maximum: {max_memory:.1f} MB")
                print(f"  Average: {avg_memory:.1f} MB")

    elif isinstance(results, dict):
        for key, value in results.items():
            print(f"  {key}: {value}")

    print("="*60)


def create_batch_report(file_list, results_df):
    """
    Create a detailed batch processing report.

    Args:
        file_list: List of processed files
        results_df: DataFrame with processing results

    Returns:
        dict: Batch report
    """
    report = {
        'total_files': len(file_list),
        'processed': 0,
        'failed': 0,
        'skipped': 0,
        'success_rate': 0.0,
        'total_time_minutes': 0.0,
        'files_failed': [],
        'files_skipped': []
    }

    if results_df is not None and not results_df.empty:
        if 'status' in results_df.columns:
            report['processed'] = len(results_df[results_df['status'] == 'success'])
            report['failed'] = len(results_df[results_df['status'] == 'failed'])
            report['skipped'] = len(results_df[results_df['status'] == 'skipped'])

            if report['total_files'] > 0:
                report['success_rate'] = (report['processed'] / report['total_files']) * 100

            # Get failed files
            failed_df = results_df[results_df['status'] == 'failed']
            if not failed_df.empty and 'original_file' in failed_df.columns:
                report['files_failed'] = failed_df['original_file'].tolist()

            # Get skipped files
            skipped_df = results_df[results_df['status'] == 'skipped']
            if not skipped_df.empty and 'original_file' in skipped_df.columns:
                report['files_skipped'] = skipped_df['original_file'].tolist()

        if 'processing_time_s' in results_df.columns:
            report['total_time_minutes'] = results_df['processing_time_s'].sum() / 60

    return report