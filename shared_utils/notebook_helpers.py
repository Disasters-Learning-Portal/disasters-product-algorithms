"""
Simplified helper functions for Jupyter notebooks.
Makes COG processing user-friendly with minimal configuration.
"""

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import pandas as pd

# Add parent directory to path for imports
module_path = Path(__file__).parent.parent.resolve()
if str(module_path) not in sys.path:
    sys.path.insert(0, str(module_path))


class SimpleProcessor:
    """
    Simplified processor for disaster imagery conversion.
    Handles all complexity internally.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize processor with simple configuration.

        Args:
            config: Simple configuration dictionary
        """
        self.config = config
        self.s3_client = None
        self.files_to_process = {}
        self.results = []

        # Import required modules
        self._import_modules()

    def _import_modules(self):
        """Import all required modules."""
        try:
            # Core modules
            from shared_utils.s3_operations import (
                initialize_s3_client,
                check_s3_file_exists,
                list_s3_files,
                get_file_size_from_s3
            )
            from shared_utils.main_processor import convert_to_cog
            from shared_utils.log_utils import print_status

            # Store references
            self.initialize_s3_client = initialize_s3_client
            self.check_s3_file_exists = check_s3_file_exists
            self.list_s3_files = list_s3_files
            self.get_file_size_from_s3 = get_file_size_from_s3
            self.convert_to_cog = convert_to_cog
            self.print_status = print_status

            print("✅ All modules loaded successfully")

        except ImportError as e:
            print(f"⚠️ Import error: {e}")
            print("Please ensure you're running from the disasters-aws-conversion directory")
            raise

    def connect_to_s3(self) -> bool:
        """
        Connect to S3 bucket.

        Returns:
            True if successful
        """
        print("\n🌐 Connecting to S3...")
        self.s3_client, _ = self.initialize_s3_client(
            bucket_name=self.config['bucket'],
            verbose=False
        )

        if self.s3_client:
            print("✅ Connected to S3 successfully")
            return True
        else:
            print("❌ Failed to connect to S3")
            return False

    def discover_files(self) -> int:
        """
        Discover and categorize files automatically.

        Returns:
            Number of files found
        """
        print(f"\n🔍 Searching for files in: {self.config['source_path']}")

        # List all TIF files
        keys = self.list_s3_files(
            self.s3_client,
            self.config['bucket'],
            self.config['source_path'],
            suffix='.tif'
        )

        if not keys:
            print("⚠️ No .tif files found")
            return 0

        print(f"✅ Found {len(keys)} files")

        # Auto-detect product types
        self.files_to_process = self._categorize_files(keys)

        # Show summary
        print("\n📊 File Categories:")
        for category, files in self.files_to_process.items():
            print(f"  • {category}: {len(files)} files")

        return len(keys)

    def _categorize_files(self, file_list: List[str]) -> Dict[str, List[str]]:
        """
        Automatically categorize files by product type.
        Uses notebook-provided patterns first, then falls back to defaults.

        Args:
            file_list: List of file paths

        Returns:
            Dictionary of categorized files
        """
        categories = {}

        # Use notebook-provided patterns if available, otherwise use defaults
        user_patterns = self.config.get('categorization_patterns', {})

        # Default patterns as fallback
        default_patterns = {
            'trueColor': r'trueColor|truecolor|true_color',
            'colorInfrared': r'colorInfrared|colorIR|color_infrared',
            'naturalColor': r'naturalColor|natural_color',
            'NDVI': r'NDVI|ndvi',
            'MNDWI': r'MNDWI|mndwi',
            'SAR': r'SAR|sar|Sentinel-1|sentinel1',
            'optical': r'B\d+\.tif|band\d+',
        }

        # Merge patterns - user patterns take precedence
        patterns = {**default_patterns, **user_patterns}

        # Track uncategorized files for warning
        uncategorized_files = []

        # Categorize each file
        for file_path in file_list:
            filename = os.path.basename(file_path)
            categorized = False

            for category, pattern in patterns.items():
                if re.search(pattern, filename, re.IGNORECASE):
                    if category not in categories:
                        categories[category] = []
                    categories[category].append(file_path)
                    categorized = True
                    break

            # Track files that don't match any pattern
            if not categorized:
                uncategorized_files.append(filename)

        # Warn about uncategorized files
        if uncategorized_files:
            print(f"\n⚠️ Warning: {len(uncategorized_files)} files don't match any category pattern:")
            for f in uncategorized_files[:5]:  # Show first 5
                print(f"   • {f}")
            if len(uncategorized_files) > 5:
                print(f"   ... and {len(uncategorized_files) - 5} more")
            print("\n   These files will be skipped. Add patterns to 'categorization_patterns' in your notebook to process them.")

        return categories

    def preview_processing(self):
        """Show preview of what will be processed."""
        print("\n" + "="*60)
        print("📋 PROCESSING PREVIEW")
        print("="*60)

        total_files = sum(len(files) for files in self.files_to_process.values())
        print(f"\nTotal files to process: {total_files}")
        print(f"Event: {self.config['event_name']}")
        print(f"Source: s3://{self.config['bucket']}/{self.config['source_path']}")
        print(f"Destination: s3://{self.config['bucket']}/{self.config['destination_base']}/")

        print("\nFile categories:")
        for category, files in self.files_to_process.items():
            print(f"  • {category}: {len(files)} files")
            # Show first file as example with custom filename creator if provided
            if files:
                example = os.path.basename(files[0])
                # Use custom filename creator if provided
                filename_creator = self.config.get('filename_creators', {}).get(category)
                if filename_creator:
                    new_name = filename_creator(files[0], self.config['event_name'])
                else:
                    new_name = self._generate_filename(files[0], category)
                print(f"    Example: {example}")
                print(f"    → {new_name}")

        print("\nSettings:")
        print(f"  • Compression: ZSTD level 22")
        print(f"  • Overwrite existing: {self.config.get('overwrite', False)}")
        print(f"  • Verify results: {self.config.get('verify', True)}")
        print("="*60)

    def process_all(self) -> pd.DataFrame:
        """
        Process all discovered files with optimized settings.

        Returns:
            DataFrame with processing results
        """
        if not self.files_to_process:
            print("⚠️ No files to process. Run discover_files() first.")
            return pd.DataFrame()

        print("\n🚀 Starting processing...")
        start_time = datetime.now()
        all_results = []

        # Process each category
        for category, file_list in self.files_to_process.items():
            print(f"\n📦 Processing {category} ({len(file_list)} files)")

            category_results = self._process_category(category, file_list)
            all_results.extend(category_results)

        # Create results DataFrame
        self.results = pd.DataFrame(all_results)

        # Show summary
        total_time = (datetime.now() - start_time).total_seconds()
        self._show_summary(total_time)

        return self.results

    def _process_category(self, category: str, file_list: List[str]) -> List[Dict]:
        """
        Process files in a category.

        Args:
            category: Category name
            file_list: List of files to process

        Returns:
            List of processing results
        """
        results = []

        # Determine output directory
        output_dir = self._get_output_dir(category)

        # Get filename creator function for this category
        filename_creator = self.config.get('filename_creators', {}).get(category)
        if not filename_creator:
            print(f"⚠️ No filename creator for {category}, using default")
            filename_creator = lambda path, _: self._generate_filename(path, category)

        for file_path in file_list:
            start = datetime.now()

            try:
                # Generate output filename using provided function
                cog_filename = filename_creator(file_path, self.config['event_name'])

                # Check if exists and handle overwrite
                output_key = f"{self.config['destination_base']}/{output_dir}/{cog_filename}"
                exists = self.check_s3_file_exists(
                    self.s3_client,
                    self.config['bucket'],
                    output_key
                )

                if exists and not self.config.get('overwrite', False):
                    results.append({
                        'source_file': os.path.basename(file_path),
                        'category': category,
                        'status': 'skipped',
                        'reason': 'already exists',
                        'output_path': f"s3://{self.config['bucket']}/{output_key}",
                        'time_seconds': 0
                    })
                    print(f"  ⏭️ Skipped: {os.path.basename(file_path)} (exists)")
                    continue

                # Get file size
                file_size_gb = self.get_file_size_from_s3(
                    self.s3_client,
                    self.config['bucket'],
                    file_path
                )

                # Process file
                print(f"  ⚙️ Processing: {os.path.basename(file_path)} ({file_size_gb:.1f}GB)")

                # Determine no-data value
                nodata = self._get_nodata_value(category)

                # Process with optimized settings
                self.convert_to_cog(
                    name=file_path,
                    bucket=self.config['bucket'],
                    cog_filename=cog_filename,
                    cog_data_bucket=self.config['bucket'],
                    cog_data_prefix=f"{self.config['destination_base']}/{output_dir}",
                    s3_client=self.s3_client,
                    manual_nodata=nodata,
                    overwrite=self.config.get('overwrite', False)
                )

                results.append({
                    'source_file': os.path.basename(file_path),
                    'category': category,
                    'status': 'success',
                    'output_filename': cog_filename,
                    'output_path': f"s3://{self.config['bucket']}/{self.config['destination_base']}/{output_dir}/{cog_filename}",
                    'time_seconds': (datetime.now() - start).total_seconds()
                })

                print(f"  ✅ Complete: {cog_filename}")

            except Exception as e:
                results.append({
                    'source_file': os.path.basename(file_path),
                    'category': category,
                    'status': 'failed',
                    'error': str(e),
                    'time_seconds': (datetime.now() - start).total_seconds()
                })
                print(f"  ❌ Failed: {os.path.basename(file_path)} - {e}")

        return results

    def _get_output_dir(self, category: str) -> str:
        """
        Get output directory for category.
        First checks user-provided configuration, then falls back to defaults.

        Args:
            category: Category name

        Returns:
            Output directory path
        """
        # First check if user provided output directories in config
        user_output_dirs = self.config.get('output_dirs', {})
        if category in user_output_dirs:
            return user_output_dirs[category]

        # Fall back to default mappings
        default_dirs = {
            'trueColor': 'imagery/trueColor',
            'colorInfrared': 'imagery/colorIR',
            'naturalColor': 'imagery/naturalColor',
            'NDVI': 'indices/NDVI',
            'MNDWI': 'indices/MNDWI',
            'SAR': 'SAR/processed',
            'optical': 'optical/bands'
        }

        # If category not in defaults, use the category name itself as directory
        return default_dirs.get(category, f'uncategorized/{category}')

    def _generate_filename(self, original_path: str, _: str = None) -> str:
        """
        Generate COG filename.

        Args:
            original_path: Original file path
            category: File category

        Returns:
            New filename
        """
        filename = os.path.basename(original_path)
        stem = os.path.splitext(filename)[0]

        # Extract date if present (YYYYMMDD format)
        date_match = re.search(r'\d{8}', stem)
        if date_match:
            date_str = date_match.group()
            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            # Remove date from stem and add formatted version
            stem_clean = re.sub(r'_?\d{8}_?', '_', stem)
            return f"{self.config['event_name']}_{stem_clean}_{formatted_date}_day.tif"
        else:
            return f"{self.config['event_name']}_{stem}_day.tif"

    def _get_nodata_value(self, category: str) -> Optional[float]:
        """
        Get appropriate no-data value for category.

        Args:
            category: File category

        Returns:
            No-data value or None for auto-detection
        """
        # Smart defaults by category
        nodata_defaults = {
            'NDVI': -9999,
            'MNDWI': -9999,
            'trueColor': None,  # Auto-detect for imagery
            'colorInfrared': None,
            'naturalColor': None,
            'SAR': None,
            'optical': None
        }

        # Check if user provided override
        user_nodata = self.config.get('nodata_values', {})
        if category in user_nodata:
            return user_nodata[category]

        return nodata_defaults.get(category)

    def _show_summary(self, total_time: float):
        """
        Show processing summary.

        Args:
            total_time: Total processing time in seconds
        """
        # Check if results exist and have data
        if self.results is None:
            return

        # Convert to DataFrame if it's a list
        import pandas as pd
        if isinstance(self.results, list):
            if len(self.results) == 0:
                return
            self.results = pd.DataFrame(self.results)

        # Check if DataFrame is empty
        if isinstance(self.results, pd.DataFrame) and self.results.empty:
            return

        print("\n" + "="*60)
        print("✅ PROCESSING COMPLETE")
        print("="*60)

        # Count by status
        if 'status' in self.results.columns:
            status_counts = self.results['status'].value_counts()
            print("\nResults:")
            for status, count in status_counts.items():
                emoji = {'success': '✅', 'failed': '❌', 'skipped': '⏭️'}.get(str(status), '•')
                print(f"  {emoji} {str(status).capitalize()}: {count}")

            # Time statistics
            print(f"\nProcessing time: {total_time/60:.1f} minutes")

            success_df = self.results[self.results['status'] == 'success']
            if not success_df.empty and 'time_seconds' in success_df.columns:
                avg_time = success_df['time_seconds'].mean()
                print(f"Average per file: {avg_time:.1f} seconds")

        # Save results if configured
        if self.config.get('save_results', True):  # Default to True for backward compatibility
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_dir = f"output/{self.config['event_name']}"
            os.makedirs(output_dir, exist_ok=True)

            csv_path = f"{output_dir}/results_{timestamp}.csv"
            self.results.to_csv(csv_path, index=False)
            print(f"\n📁 Results saved to: {csv_path}")
        else:
            print("\n💡 Results not saved to CSV (SAVE_RESULTS=False)")

        print("="*60)


def quick_process(config: Dict[str, Any]) -> pd.DataFrame:
    """
    One-function processing for maximum simplicity.

    Args:
        config: Simple configuration dictionary with:
            - event_name: Name of the disaster event
            - bucket: S3 bucket name
            - source_path: Path to source files in S3
            - destination_base: Base path for output files
            - overwrite: Whether to overwrite existing files (optional)
            - verify: Whether to verify results (optional)

    Returns:
        DataFrame with processing results

    Example:
        results = quick_process({
            'event_name': '202408_TropicalStorm_Debby',
            'bucket': 'nasa-disasters',
            'source_path': 'drcs_activations/202408_TropicalStorm_Debby/landsat8',
            'destination_base': 'drcs_activations_new',
            'overwrite': False
        })
    """
    print("🚀 Starting Quick Processing")
    print("="*60)

    # Create processor
    processor = SimpleProcessor(config)

    # Connect to S3
    if not processor.connect_to_s3():
        print("Failed to connect to S3")
        return pd.DataFrame()

    # Discover files
    num_files = processor.discover_files()
    if num_files == 0:
        print("No files found to process")
        return pd.DataFrame()

    # Show preview
    processor.preview_processing()

    # Process all files
    results = processor.process_all()

    return results