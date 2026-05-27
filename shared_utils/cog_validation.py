"""
Validation module - handles COG validation and data integrity checks.
Single responsibility: Data and format validation.
"""

import os
import tempfile
import numpy as np
import rasterio
from rio_cogeo.cogeo import cog_validate
from rio_cogeo.cogeo import cog_info


def validate_cog(file_path):
    """
    Validate if a file is a proper Cloud Optimized GeoTIFF.

    Args:
        file_path: Path to the file to validate

    Returns:
        tuple: (is_valid, validation_details)
    """
    try:
        # Use rio-cogeo for validation
        is_valid = cog_validate(file_path, quiet=True)[0]

        # Get detailed info if needed
        info = cog_info(file_path)

        validation_details = {
            'valid': is_valid,
            'errors': [],
            'warnings': []
        }

        if not is_valid:
            validation_details['errors'].append("File is not a valid COG")

        return is_valid, validation_details

    except Exception as e:
        return False, {'valid': False, 'errors': [str(e)], 'warnings': []}


def check_cog_with_warnings(file_path, verbose=True):
    """
    Check COG validity and print warnings.

    Args:
        file_path: Path to the file to validate
        verbose: Print validation messages

    Returns:
        bool: True if valid COG
    """
    if verbose:
        print(f"   [VALIDATE] Checking COG validity...")

    is_valid, validation_details = validate_cog(file_path)

    if is_valid:
        if verbose:
            print(f"   [VALIDATE] ✅ Valid COG")
    else:
        if verbose:
            print(f"   [VALIDATE] ⚠️ COG validation warnings")
            if 'errors' in validation_details:
                for error in validation_details['errors']:
                    print(f"      - {error}")
            if 'warnings' in validation_details:
                for warning in validation_details['warnings']:
                    print(f"      - {warning}")

    return is_valid


def is_s3_file_cog(s3_client, bucket, key, verbose=False):
    """
    Check if a file in S3 is already a valid Cloud Optimized GeoTIFF.

    Downloads the file temporarily to validate its COG structure.

    Args:
        s3_client: boto3 S3 client
        bucket: S3 bucket name
        key: S3 key (file path within bucket)
        verbose: Print validation messages

    Returns:
        tuple: (is_valid, validation_details)
            - is_valid (bool): True if file is a valid COG
            - validation_details (dict): Details about validation including:
                - valid: bool
                - errors: list of error messages
                - warnings: list of warning messages
                - file_size_mb: float (file size in MB)
    """
    temp_file = None
    try:
        if verbose:
            print(f"   [COG-CHECK] Downloading file from S3 for validation...")

        # Get file size first
        try:
            response = s3_client.head_object(Bucket=bucket, Key=key)
            file_size_bytes = response['ContentLength']
            file_size_mb = file_size_bytes / (1024 * 1024)
        except Exception as e:
            return False, {
                'valid': False,
                'errors': [f"Failed to get file info: {str(e)}"],
                'warnings': [],
                'file_size_mb': 0
            }

        # Create temporary file
        suffix = os.path.splitext(key)[1] or '.tif'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir='/tmp') as tmp:
            temp_file = tmp.name

        # Download file
        try:
            s3_client.download_file(bucket, key, temp_file)
        except Exception as e:
            return False, {
                'valid': False,
                'errors': [f"Failed to download file: {str(e)}"],
                'warnings': [],
                'file_size_mb': file_size_mb
            }

        if verbose:
            print(f"   [COG-CHECK] Downloaded {file_size_mb:.2f} MB, validating...")

        # Validate the COG
        is_valid, validation_details = validate_cog(temp_file)
        validation_details['file_size_mb'] = file_size_mb

        if verbose:
            if is_valid:
                print(f"   [COG-CHECK] ✅ File is a valid COG")
            else:
                print(f"   [COG-CHECK] ❌ File is NOT a valid COG")
                for error in validation_details.get('errors', []):
                    print(f"      - {error}")

        return is_valid, validation_details

    except Exception as e:
        if verbose:
            print(f"   [COG-CHECK] ❌ Error during validation: {str(e)}")
        return False, {
            'valid': False,
            'errors': [f"Validation error: {str(e)}"],
            'warnings': [],
            'file_size_mb': 0
        }

    finally:
        # Clean up temporary file
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except:
                pass


def check_and_fix_nan_values(data, nodata_value, dtype, band_idx=None, verbose=False):
    """
    Check for NaN values and fix them.

    Args:
        data: numpy array with data
        nodata_value: Value to use for nodata
        dtype: Data type of the array
        band_idx: Band index for reporting
        verbose: Print messages

    Returns:
        tuple: (fixed_data, had_nan)
    """
    had_nan = False

    # Check for NaN values (only for float dtypes)
    if np.issubdtype(dtype, np.floating):
        nan_count = np.isnan(data).sum()
        if nan_count > 0:
            had_nan = True
            if verbose:
                band_str = f"band {band_idx}" if band_idx else "data"
                print(f"   [NAN] Found {nan_count} NaN values in {band_str}")

            # Replace NaN with nodata value
            data = np.where(np.isnan(data), nodata_value, data)

            if verbose:
                print(f"   [NAN] Replaced NaN values with {nodata_value}")

    # Check for infinity values
    if np.issubdtype(dtype, np.floating):
        inf_count = np.isinf(data).sum()
        if inf_count > 0:
            had_nan = True
            if verbose:
                print(f"   [INF] Found {inf_count} infinity values")

            # Replace infinity with nodata value
            data = np.where(np.isinf(data), nodata_value, data)

    return data, had_nan


def validate_data_integrity(data, expected_shape=None, expected_dtype=None, verbose=True):
    """
    Validate data integrity with comprehensive checks.

    Args:
        data: numpy array to validate
        expected_shape: Expected shape tuple
        expected_dtype: Expected data type
        verbose: Print validation messages

    Returns:
        dict: Validation results
    """
    results = {
        'valid': True,
        'issues': [],
        'stats': {}
    }

    # Check shape
    if expected_shape and data.shape != expected_shape:
        results['valid'] = False
        results['issues'].append(f"Shape mismatch: expected {expected_shape}, got {data.shape}")

    # Check dtype
    if expected_dtype and data.dtype != expected_dtype:
        results['issues'].append(f"Dtype mismatch: expected {expected_dtype}, got {data.dtype}")

    # Calculate statistics
    results['stats']['shape'] = data.shape
    results['stats']['dtype'] = str(data.dtype)
    results['stats']['min'] = float(np.nanmin(data))
    results['stats']['max'] = float(np.nanmax(data))
    results['stats']['mean'] = float(np.nanmean(data))
    results['stats']['has_nan'] = bool(np.isnan(data).any())
    results['stats']['has_inf'] = bool(np.isinf(data).any())

    # Check for common issues
    if results['stats']['has_nan']:
        results['issues'].append("Data contains NaN values")

    if results['stats']['has_inf']:
        results['issues'].append("Data contains infinity values")

    # Check if all values are the same
    if np.all(data == data.flat[0]):
        results['issues'].append("All values are identical")

    if verbose and results['issues']:
        print(f"   [VALIDATE] Data validation issues found:")
        for issue in results['issues']:
            print(f"      - {issue}")

    return results


def validate_nodata_value(file_path, expected_nodata, verbose=True):
    """
    Validate that a COG file has the correct nodata value set.

    Args:
        file_path: Path to the COG file
        expected_nodata: Expected nodata value
        verbose: Print validation messages

    Returns:
        dict: Validation results with keys:
            - nodata_matches: True if nodata matches expected
            - file_nodata: Actual nodata value in file
            - expected_nodata: Expected nodata value
            - sample_values: Sample of actual values in the file
    """
    try:
        with rasterio.open(file_path) as src:
            file_nodata = src.nodata

            # Read a small sample of data to check actual values
            sample_size = min(512, src.width, src.height)
            sample_data = src.read(1, window=((0, sample_size), (0, sample_size)))

            # Check for presence of both old and new nodata values
            unique_values = np.unique(sample_data)

            results = {
                'nodata_matches': file_nodata == expected_nodata,
                'file_nodata': file_nodata,
                'expected_nodata': expected_nodata,
                'sample_unique_values': unique_values[:10].tolist(),  # First 10 unique values
                'sample_has_expected_nodata': expected_nodata in sample_data if expected_nodata is not None else False
            }

            if verbose:
                print(f"   [NODATA-VALIDATE] File nodata: {file_nodata}")
                print(f"   [NODATA-VALIDATE] Expected nodata: {expected_nodata}")

                if results['nodata_matches']:
                    print(f"   [NODATA-VALIDATE] ✅ Nodata value matches")
                else:
                    print(f"   [NODATA-VALIDATE] ❌ Nodata mismatch!")

                # Check if we find the expected nodata in the data
                if expected_nodata is not None:
                    count_expected = np.sum(np.isclose(sample_data, expected_nodata, rtol=1e-9))
                    print(f"   [NODATA-VALIDATE] Found {count_expected} pixels with expected nodata value in sample")

            return results

    except Exception as e:
        if verbose:
            print(f"   [NODATA-VALIDATE] ❌ Error validating: {e}")
        return {
            'nodata_matches': False,
            'file_nodata': None,
            'expected_nodata': expected_nodata,
            'error': str(e)
        }