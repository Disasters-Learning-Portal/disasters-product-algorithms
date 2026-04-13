# Jupyter Notebook Guide for Disaster COG Processing

This guide helps you get started with converting disaster satellite imagery to Cloud Optimized GeoTIFFs (COGs).

## Quick Start - Two Workflows

### 🚀 Route 1: Single Script (Recommended for Most Users)

Use [templates/simple_disaster_template.ipynb](templates/simple_disaster_template.ipynb) for an all-in-one streamlined experience:

**Best for:** Quick processing, single events, simple workflows

1. **Open the notebook**
   ```bash
   jupyter notebook templates/simple_disaster_template.ipynb
   ```

2. **Configure your event** (Step 1)
   - Set `EVENT_NAME` (e.g., '202510_Flood_AK')
   - Set `SUB_PRODUCT_NAME` (e.g., 'sentinel2')
   - Set `SOURCE_PATH` to your S3 location

3. **See your files** (Step 2)
   - Lists all .tif files in your S3 path
   - Shows file names and sizes to help configure transformations

4. **Define filename transformations** (Step 3)
   - Set categorization patterns (regex to identify file types)
   - Define filename creator functions (how to rename files)
   - Configure output directories (where to save each category)
   - Preview transformations before processing

5. **Process all files** (Step 4-5)
   - Processes all files to COGs with new filenames
   - Automatic verification and results tracking

### 📋 Route 2: Two-Step Workflow (For Complex Renaming)

Use two notebooks for more control over filename mapping:

**Best for:** Complex renaming, batch processing, CSV-based workflows

#### Step 1: Create Filename Mapping
Use [templates/renaming_file_template.ipynb](templates/renaming_file_template.ipynb):
- Lists all files from S3
- Define categorization and filename transformation functions
- Generates a CSV mapping file with:
  - Original S3 paths
  - New filenames
  - Categories
  - Nodata values
  - File sizes
- Review and validate the CSV before processing

#### Step 2: Process from CSV
Use [templates/process_from_csv_template.ipynb](templates/process_from_csv_template.ipynb):
- Loads the CSV mapping created in Step 1
- Previews all transformations
- Processes files to COGs using the CSV mappings
- Tracks results and saves processing statistics

**Why use this route?**
- Separate filename planning from processing
- Review and edit CSV mappings before processing
- Reuse mappings across multiple processing runs
- Better for large batches with complex naming rules

## Configuration Examples

### Basic Configuration

```python
EVENT_NAME = '202408_TropicalStorm_Debby'
PRODUCT_NAME = 'landsat8'
BUCKET = 'nasa-disasters'
SOURCE_PATH = f'drcs_activations/{EVENT_NAME}/{PRODUCT_NAME}'
DESTINATION_BASE = 'drcs_activations_new'
OVERWRITE = False  # Set True to replace existing files
```

### Custom Filename Functions

Define how your files are renamed:

```python
def create_truecolor_filename(original_path, event_name):
    """Create filename for trueColor products."""
    filename = os.path.basename(original_path)
    stem = os.path.splitext(filename)[0]
    date = extract_date_from_filename(stem)

    if date:
        stem_clean = re.sub(r'_\d{8}', '', stem)
        return f"{event_name}_{stem_clean}_{date}_day.tif"
    return f"{event_name}_{stem}_day.tif"
```

### Map Products to Filename Functions

```python
FILENAME_CREATORS = {
    'trueColor': create_truecolor_filename,
    'colorInfrared': create_colorinfrared_filename,
    'naturalColor': create_naturalcolor_filename,
}
```

## File Organization

The system automatically:
- **Discovers** files in your S3 source path
- **Categorizes** them by product type (trueColor, NDVI, etc.)
- **Applies** the appropriate filename function
- **Saves** to organized output directories

### Default Output Structure
```
drcs_activations_new/
├── imagery/
│   ├── trueColor/
│   ├── colorIR/
│   └── naturalColor/
├── indices/
│   ├── NDVI/
│   └── MNDWI/
└── SAR/
    └── processed/
```

## Common Patterns

### Process Multiple Product Types

The system automatically detects and processes different product types:

```python
# Files are auto-categorized by these patterns:
- 'trueColor' → imagery/trueColor/
- 'colorInfrared' → imagery/colorIR/
- 'NDVI' → indices/NDVI/
- 'MNDWI' → indices/MNDWI/
- 'SAR' → SAR/processed/
```

### Custom No-Data Values

```python
NODATA_VALUES = {
    'NDVI': -9999,      # Specific value for NDVI
    'MNDWI': -9999,     # Specific value for MNDWI
    'trueColor': None,  # Auto-detect for imagery
}
```

### Override Output Directories

```python
OUTPUT_DIRS = {
    'trueColor': 'Landsat/trueColor',
    'colorInfrared': 'Landsat/colorIR',
    'naturalColor': 'Landsat/naturalColor',
}
```

## Troubleshooting

### Issue: "No files found"
- Check `SOURCE_PATH` is correct
- Verify files exist: `aws s3 ls s3://bucket/path/`

### Issue: "Failed to connect to S3"
- Check AWS credentials: `aws configure list`
- Ensure bucket access permissions

### Issue: Files being skipped
- Files already exist in destination
- Set `OVERWRITE = True` to reprocess

### Issue: Wrong filenames
- Modify filename creator functions
- Re-run from discovery step to preview

### Issue: Processing is slow
- Large files take time (normal)
- System automatically uses GDAL optimization
- Files >1.5GB use optimized chunking

## Performance Tips

1. **File Size Optimization**
   - Files <1.5GB: Processed whole (fastest)
   - Files >1.5GB: Smart chunking
   - Files >7GB: Ultra-large file handling

2. **Compression**
   - Uses ZSTD level 22 (maximum compression)
   - Automatic predictor selection
   - Intelligent resampling based on data type

3. **Parallel Processing**
   - The system automatically optimizes processing for your files
   - Files are processed sequentially with smart memory management

## Advanced Features

### Using the Helper Module Directly

```python
from lib.notebook_helpers import quick_process

results = quick_process({
    'event_name': '202408_TropicalStorm_Debby',
    'bucket': 'nasa-disasters',
    'source_path': 'drcs_activations/202408_TropicalStorm_Debby/landsat8',
    'destination_base': 'drcs_activations_new',
    'overwrite': False,
    'filename_creators': FILENAME_CREATORS
})
```

### Batch Processing Multiple Events

```python
events = [
    '202408_TropicalStorm_Debby',
    '202409_Hurricane_Example',
    '202410_Wildfire_Sample'
]

for event in events:
    config['event_name'] = event
    config['source_path'] = f'drcs_activations/{event}/landsat8'
    processor = SimpleProcessor(config)
    processor.connect_to_s3()
    processor.discover_files()
    processor.process_all()
```

## Next Steps

1. Start with the simple template
2. Run a small test batch
3. Verify output filenames are correct
4. Process full dataset
5. Check results in S3

For more details, see the main [README.md](README.md) or review the [RESAMPLING_GUIDE.md](RESAMPLING_GUIDE.md) for data type handling.