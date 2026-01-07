# Disasters Product Algorithms

Unified package for processing Landsat 8/9 and Sentinel-2 satellite imagery to generate disaster response products.

## Features

### Landsat 8/9 Products
- True color imagery
- Panchromatic (Band 8)
- Natural color
- Color infrared
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- MNDWI (Modified NDWI)
- EVI (Enhanced Vegetation Index)
- NBR (Normalized Burn Ratio)
- Water extent classification
- Cloud masking
- COG (Cloud Optimized GeoTIFF) conversion

### Sentinel-2 Products
- True color imagery
- Natural color
- SWIR (Shortwave Infrared)
- Color infrared
- NDVI (Normalized Difference Vegetation Index)
- NDWI (Normalized Difference Water Index)
- MNDWI (Modified NDWI)
- NBR (Normalized Burn Ratio)
- Water extent classification
- Cloud masking (L2A only)

## Installation

### Prerequisites

**IMPORTANT:** This package requires GDAL and other geospatial libraries that MUST be installed via conda BEFORE installing this package.

#### Step 1: Install geospatial dependencies via conda

```bash
# Create a new environment:
conda create -n disasters python=3.10
conda activate disasters

# Install required geospatial packages (MUST be installed via conda):
conda install -c conda-forge gdal rasterio rio-cogeo geopandas pyproj numpy scipy requests boto3
```

**Why conda?** The GDAL Python bindings must match your system's libgdal version. Installing GDAL via pip will fail or cause version conflicts. Conda ensures all geospatial libraries are compatible.

#### Step 2: Install this package

```bash
# Clone the repository
git clone https://github.com/klesinger/disasters-product-algorithms.git
cd disasters-product-algorithms

# Install in editable mode
pip install -e .
```

**For JupyterHub users:** If your environment already has GDAL, rasterio, and other geospatial libraries installed via conda, you can skip Step 1.

## Usage

### Command Line Interface

#### Landsat 8/9

```bash
# Basic usage
process_landsat89 /path/to/landsat/data -p true ndvi ndwi

# Process all products with merging and cloud masking
process_landsat89 /path/to/landsat/data -p all -merge -mask

# Process specific date
process_landsat89 /path/to/landsat/data -p true ndvi -date 20230116

# With event naming and COG options
process_landsat89 /path/to/landsat/data -p all -event 202512_Flood_WA -compression ZSTD
```

**Landsat Options:**
```
process_landsat89 input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]]
                        [-date [DATE ...]] [-tile [TILE ...]]
                        [-merge] [-mask] [-force] [-unzip_only]
                        [-tif_only] [-nodata NODATA]
                        [-compression COMPRESSION] [-compression_level LEVEL]
                        [-event EVENT] [-zip [ZIP ...]] [-dir [DIR ...]]
```

**Products:** `all`, `true`, `pan`, `nat`, `colorIR`, `ndvi`, `ndwi`, `mndwi`, `evi`, `nbr`, `we`

#### Sentinel-2

```bash
# Basic usage
process_sentinel2 /path/to/sentinel/data -p true ndvi ndwi

# Process all products with merging and cloud masking
process_sentinel2 /path/to/sentinel/data -p all -merge -mask

# Process specific date
process_sentinel2 /path/to/sentinel/data -p true ndvi -date 20230116

# Water extent with custom thresholds
process_sentinel2 /path/to/sentinel/data -p we -we_nstd 1 1.5 2

# With event naming and COG options (same as Landsat!)
process_sentinel2 /path/to/sentinel/data -p all -event 202512_Flood_WA -compression ZSTD
```

**Sentinel-2 Options:**
```
process_sentinel2 input [-h] [-p [P ...]] [-we_nstd [WE_NSTD ...]]
                        [-date [DATE ...]] [-tile [TILE ...]]
                        [-merge] [-mask] [-force] [-unzip_only]
                        [-tif_only] [-nodata NODATA]
                        [-compression COMPRESSION] [-compression_level LEVEL]
                        [-event EVENT]
```

**Products:** `all`, `true`, `nat`, `swir`, `colorIR`, `ndvi`, `ndwi`, `mndwi`, `nbr`, `we`

### Python API

Import and use functions in your code:

```python
# Landsat functions
from landsat import genTrueColor, genNdvi, gen_water_extent, ls_merge

# Sentinel functions
from sentinel import gen_true_color, gen_ndvi, gen_water_extent, s2_merge

# Shared utilities
from shared_utils import convert_to_cog, rename_with_event
```

## Key Features

### Cloud Optimized GeoTIFFs (Both Landsat & Sentinel)
- **Automatic COG conversion by default** for both sensors
- Customizable compression (ZSTD, DEFLATE, LZW)
- Auto-detected no-data values
- Multi-level overviews for efficient visualization
- Use `-tif_only` to skip COG conversion if needed

### Event-Based File Naming (Both Landsat & Sentinel)
Add event prefix to organize outputs by disaster. The date is removed from the middle and reformatted at the end:

```bash
# Landsat
process_landsat89 /data/landsat8 -p all -event 202512_Flood_WA

# Sentinel-2
process_sentinel2 /data/sentinel2 -p all -event 202512_Flood_WA
```

**Filename Transformation:**
- **Landsat:** `LC08_trueColor_20250922_185617_046028.tif`
  → `202512_Flood_WA_LC08_trueColor_185617_046028_2025-09-22_day.tif`

- **Sentinel-2:** `S2B_MSIL2A_colorInfrared_20251111_161419_T17RLN.tif`
  → `202512_Flood_WA_S2B_MSIL2A_colorInfrared_161419_T17RLN_2025-11-11_day.tif`

### Smart Processing
- Automatically skips already processed files
- Use `-force` to reprocess existing outputs
- Supports merging by date and product
- Cloud masking for cleaner imagery

## Input Data

**Landsat 8/9:**
- Collection 2 Level-2 surface reflectance data in `.tar` or `.zip` format
- Download from [USGS EarthExplorer](https://earthexplorer.usgs.gov/) or [NASA Earthdata](https://earthdata.nasa.gov/)

**Sentinel-2:**
- L1C or L2A data in `.zip` format
- Download using `download_sentinel2` command or from [Copernicus Open Access Hub](https://scihub.copernicus.eu/)

### Downloading Sentinel-2 Data

Use the `download_sentinel2` command to download Sentinel-2 imagery from Copernicus:

```bash
# Download by tile and date
download_sentinel2 /output/dir -tile T36SYD -date 20230116

# Download by point coordinates
download_sentinel2 /output/dir -point 35.5 33.9 -date 20230116

# Download by polygon
download_sentinel2 /output/dir -polygon /path/to/polygon.shp -date 20230116

# Download date range
download_sentinel2 /output/dir -tile T36SYD -date 20230116 20230120
```

## Output

Processed products are saved to an `output/` directory within the input directory, organized by:
- Date directories (YYYYMMDD)
- Product type subdirectories
- GeoTIFF files with standardized naming

## Dependencies

- Python >= 3.8
- GDAL (via conda)
- rasterio (via conda)
- rio-cogeo (via conda)
- geopandas (via conda)
- pyproj (via conda)
- numpy (via conda)
- scipy (via conda)
- Pillow (pip)
- lxml (pip)
- requests (via conda)
- boto3 (via conda)

## Package Structure

```
disasters-product-algorithms/
├── landsat/              # Landsat 8/9 processing
├── sentinel/             # Sentinel-2 processing
└── shared_utils/         # Shared utilities (COG, GDAL, geotools)
```

## Docker Integration

This package is automatically integrated into the [pangeo-notebook-veda-image](https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image) Docker image for use on VEDA JupyterHub instances.

### Automatic Rebuilds

When code is pushed to the `main` branch of this repository, the Docker image is automatically rebuilt to include the latest changes. This is accomplished through a GitHub Actions workflow that triggers the pangeo-notebook-veda-image build pipeline.

**Monitor build status:**
- [pangeo-notebook-veda-image Actions](https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image/actions)

### Using in JupyterHub

The package is pre-installed in VEDA JupyterHub environments. All CLI commands and Python APIs are available without additional installation:

```bash
# CLI commands available in terminal
process_landsat89 --help
process_sentinel2 --help
download_sentinel2 --help
```

```python
# Python APIs available in notebooks
from landsat import genTrueColor, genNdvi
from sentinel import gen_true_color, gen_ndvi
from shared_utils import convert_to_cog, rename_with_event
```

### Docker Image Details

- **Base Image:** `pangeo/pangeo-notebook:2025.08.14`
- **Registry:** Docker Hub (`disasters-jupyterhub-docker-image`)
- **Installation:** Installed from GitHub via pip during build
- **Documentation:** See [pangeo-notebook-veda-image](https://github.com/Disasters-Learning-Portal/pangeo-notebook-veda-image) for deployment details

## Development in JupyterHub

If you want to modify and test the package code within a JupyterHub environment (where the package is pre-installed via Docker), you need to ensure your local edits are used instead of the pre-installed version.

### Option 1: Install in Editable Mode (Recommended)

After cloning the repo in JupyterHub, install it in editable mode:

```bash
# Clone the repository
cd ~
git clone https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git
cd disasters-product-algorithms

# Uninstall the pre-installed version
pip uninstall -y disasters-product-algorithms

# Install your local copy in editable mode
pip install -e .
```

**What `-e` does:**
- Creates a symlink to your local directory instead of copying files
- Any changes you make are immediately reflected when you run the CLI commands
- No need to reinstall after each edit

Now when you run `process_landsat89` or `process_sentinel2`, they'll use your edited code!

### Option 2: Run Scripts Directly (Quick Testing)

If you just want to test changes without reinstalling:

```bash
cd ~/disasters-product-algorithms

# Run the scripts directly with python
python landsat/process_landsat89.py /path/to/data -p true ndvi
python sentinel/process_sentinel2.py /path/to/data -p true ndvi
```

This bypasses the CLI entry points entirely and runs your local code directly.

### Option 3: Modify PYTHONPATH (Temporary)

Add your local directory to Python's path for the current session:

```bash
cd ~/disasters-product-algorithms
export PYTHONPATH="${PWD}:${PYTHONPATH}"

# Now Python will check your local directory first
python -c "from landsat import process_landsat89; print(process_landsat89.__file__)"
```

### Verification

To verify which version is being used:

```bash
# Check where the CLI command points
which process_landsat89

# Check where Python imports from
python -c "import landsat; print(landsat.__file__)"
```

**Editable mode (Option 1) is best** because it preserves the CLI commands while using your edited code - exactly what you need for development!

## Author

Kaylee Sharp (February 2025)

## License

See repository for license information.
