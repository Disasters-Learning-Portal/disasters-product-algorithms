# Landsat Product Algorithm

Process Landsat 8/9 surface reflectance data to generate various satellite imagery products.

## Features

This package processes Landsat 8/9 satellite imagery to produce:
- True color imagery
- Panchromatic (Band 8)
- Natural color
- Color infrared
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- MNDWI (Modified NDWI)
- EVI (Enhanced Vegetation Index)
- NBR (Normalized Burn Ratio)
- Water extent classification with cloud masking

## Installation

### Prerequisites

**IMPORTANT:** This package requires GDAL and other geospatial libraries that MUST be installed via conda BEFORE installing this package via pip.

#### Step 1: Install geospatial dependencies via conda

```bash
# If creating a new environment:
conda create -n landsat python=3.10
conda activate landsat

# Install required geospatial packages (MUST be installed via conda):
conda install -c conda-forge gdal rasterio rio-cogeo geopandas pyproj numpy scipy
```

**Why conda?** The GDAL Python bindings must match your system's libgdal version. Installing GDAL via pip will fail or cause version conflicts. Conda ensures all geospatial libraries are compatible with each other.

#### Step 2: Install this package from GitHub

Once geospatial dependencies are installed in your conda environment or JupyterHub:

```bash
pip install git+https://github.com/Disasters-Learning-Portal/landsat-product-algorithm.git
```

Or for development/editable installation:

```bash
git clone https://github.com/Disasters-Learning-Portal/landsat-product-algorithm.git
cd landsat-product-algorithm
pip install -e .
```

**For JupyterHub users:** If your JupyterHub environment already has GDAL, rasterio, and other geospatial libraries installed via conda, you can skip Step 1 and go directly to Step 2.

**Note on dependencies:** This package has minimal pip dependencies (only Pillow and lxml). All other dependencies (GDAL, rasterio, numpy, boto3, etc.) should already be present in your conda environment. This prevents pip from upgrading packages and causing version conflicts with your existing conda environment.

## Usage

### Command Line Interface

After installation, you can use the `process_landsat89` command:

```bash
process_landsat89 /path/to/landsat/data -p true ndvi ndwi -merge -mask
```

Or run the script directly:

```bash
python process_landsat89.py /path/to/landsat/data -p true ndvi ndwi -merge -mask
```

### Command Line Options

```
process_landsat89 input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]]
                        [-date [DATE ...]] [-tile [TILE ...]]
                        [-merge] [-mask] [-force] [-unzip_only]
                        [-tif_only] [-nodata NODATA]
                        [-compression COMPRESSION] [-compression_level LEVEL]
                        [-event EVENT] [-zip [ZIP ...]] [-dir [DIR ...]]

Required:
  input                        Path to directory containing .tar / .zip files

Optional - Products:
  -p [P ...]                   Products to produce: all, true, pan, nat, colorIR,
                               ndvi, ndwi, mndwi, evi, nbr, we (default: true)
  -we_nstd [NSTD ...]          Water extent standard deviations (default: 1)

Optional - Filtering:
  -date [DATE ...]             Process specific dates (e.g., 20230116)
  -tile [TILE ...]             Process specific tiles by Path/Row (e.g., 171035)
  -zip [ZIP ...]               Process specific .tar/.zip files only
  -dir [DIR ...]               Process specific unpacked directories only

Optional - Processing:
  -merge                       Merge all images by date and product
  -mask                        Generate and apply cloud masks
  -force                       Force overwrite existing products
  -unzip_only                  Only unzip archives, don't process

Optional - COG Output:
  -tif_only                    Skip COG conversion, output regular GeoTIFF (COG is default)
  -nodata NODATA               No-data value for COG (auto-detected if not specified)
  -compression COMPRESSION     Compression type: ZSTD (default), DEFLATE, LZW, etc.
  -compression_level LEVEL     Compression level (default: 22 for ZSTD, 9 for DEFLATE)

Optional - File Naming:
  -event EVENT                 Event name prefix for outputs (e.g., 202512_Flood_WA)
```

### Python API

Import and use functions in your own code:

```python
from landsat89_functions import genTrueColor, genNdvi, gen_water_extent

# Use the functions in your workflow
# ... your code here
```

Or import from the package:

```python
import landsat_product_algorithm
# Or specific imports:
from landsat_product_algorithm import genTrueColor, genNdvi
```

## Examples

### Process all products for all dates

```bash
process_landsat89 /data/landsat8 -p all -merge -mask
```

### Process specific products for a specific date

```bash
process_landsat89 /data/landsat8 -p true ndvi ndwi -date 20230116
```

### Process with water extent using multiple standard deviations

```bash
process_landsat89 /data/landsat8 -p we -we_nstd 1 1.5 2
```

### Skipping Already Processed Files

**Automatic skip behavior** - the processor checks if the final output file already exists before processing. If it exists, processing is skipped with a notification message.

```bash
# If output file already exists, it will be skipped
process_landsat89 /data/landsat8 -p true ndvi
# Output: * True color already exists: LC08_trueColor_185617_046028_2025-09-22_day.tif. Use "-force" to overwrite.

# Force reprocessing of existing files
process_landsat89 /data/landsat8 -p true ndvi -force
```

**How it works:**
- Before processing each product, the tool predicts the final output filename (accounting for COG conversion and event naming)
- If the final file exists, processing is skipped and a message is printed
- Use the `-force` flag to overwrite existing files and reprocess
- This behavior applies to all products: true color, panchromatic, natural color, color infrared, NDVI, NDWI, MNDWI, EVI, NBR, and water extent

**Benefits:**
- Resume interrupted processing runs without reprocessing completed files
- Avoid unnecessary computation when files already exist
- Safe incremental processing when adding new data to existing directories

### Cloud Optimized GeoTIFFs (Default)

**COG conversion is automatic** - all outputs are generated as Cloud Optimized GeoTIFFs by default for optimal cloud storage and visualization.

```bash
# Standard processing (creates COGs automatically)
process_landsat89 /data/landsat8 -p true ndvi

# Specify custom no-data value
process_landsat89 /data/landsat8 -p all -nodata 0

# Use custom compression type and level
process_landsat89 /data/landsat8 -p ndvi ndwi -compression DEFLATE -compression_level 9

# Skip COG conversion and keep regular GeoTIFF
process_landsat89 /data/landsat8 -p true -tif_only
```

**COG Parameters:**
- `-tif_only`: Skip COG conversion and output regular GeoTIFF (COG is default)
- `-nodata [VALUE]`: No-data value (auto-detected from data type if not specified)
- `-compression [TYPE]`: Compression type - ZSTD (default), DEFLATE, LZW, etc.
- `-compression_level [LEVEL]`: Compression level (default: 22 for ZSTD, 9 for DEFLATE)

**Auto-detected no-data values by data type:**
- uint8: 0
- uint16: 0
- int8: -128
- int16: -9999
- int32/float: -9999

All COGs include 5 overview levels for efficient visualization at multiple zoom levels.

### Event-Based File Naming

Add event name prefix and formatted date to organize outputs by disaster/event:

```bash
# Add event name to filenames
process_landsat89 /data/landsat8 -p true ndvi -event 202512_Flood_WA

# Output format: 202512_Flood_WA_LC08_trueColor_185617_046028_2025-09-22_day.tif
```

**Filename transformation:**
- Individual file: `LC08_trueColor_20250922_185617_046028.tif` → `202512_Flood_WA_LC08_trueColor_185617_046028_2025-09-22_day.tif`
- Merged file: `LC08_trueColor_20250922_merged.tif` → `202512_Flood_WA_LC08_trueColor_merged_2025-09-22_day.tif`

The event name is added as a prefix, the date is removed from the middle, and the reformatted date (YYYYMMDD → YYYY-MM-DD) is added at the end with "_day" suffix for AWS/cloud compatibility.

### Complete Example

```bash
# Process all products with event naming, merging, masking, and COG (default)
process_landsat89 /data/landsat8 -p all -merge -mask -event 202512_Flood_WA

# With custom compression
process_landsat89 /data/landsat8 -p ndvi ndwi -event 202512_Flood_WA -compression ZSTD -compression_level 20
```

## Input Data

The script expects Landsat 8/9 Collection 2 Level-2 surface reflectance data in `.tar` or `.zip` format. Data can be downloaded from:
- [USGS EarthExplorer](https://earthexplorer.usgs.gov/)
- [NASA Earthdata](https://earthdata.nasa.gov/)

## Output

Processed products are saved to an `output/` directory within the input directory, organized by:
- Date directories (YYYYMMDD)
- Product type subdirectories
- GeoTIFF files with standardized naming

## Dependencies

- Python >= 3.8
- GDAL
- rasterio
- rio-cogeo (for Cloud Optimized GeoTIFF support)
- geopandas
- pyproj
- numpy
- scipy
- Pillow (PIL)
- lxml
- requests
- boto3

## Author

Kaylee Sharp (February 2025)

## License

See repository for license information.
