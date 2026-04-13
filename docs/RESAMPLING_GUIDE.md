# COG Resampling Guide

## Automatic Resampling Selection

The optimized GDAL COG processor automatically selects the most appropriate resampling method based on data type:

### Data Type Detection & Resampling

| Data Type | Category | Reprojection | Overview Generation | Use Case |
|-----------|----------|--------------|-------------------|----------|
| **float32, float64** | Continuous | `bilinear` | `average` | NDVI, temperature, precipitation |
| **uint8 (byte)** | Categorical | `nearest` | `mode` | Land cover, classification, masks |
| **int16, int32** | Continuous* | `bilinear` | `average` | Elevation, counts |
| **uint16, uint32** | Continuous* | `bilinear` | `average` | Sensor data, indices |

*Integer types default to continuous as it's safer for most remote sensing data

## Resampling Methods Explained

### For Continuous Data (floating point, elevation, indices):
- **Reprojection**: `bilinear` - Smooth interpolation preserves data continuity
- **Overviews**: `average` - Mean of pixels maintains statistical properties

### For Categorical Data (land cover, classification):
- **Reprojection**: `nearest` - Preserves exact class values, no interpolation
- **Overviews**: `mode` - Most frequent value in the area

## How It Works

1. **Automatic Detection**: The processor reads the data type from the file
2. **Smart Selection**: Chooses resampling based on dtype:
   ```python
   dtype = src.dtypes[0]
   resampling, overview_resampling = get_resampling_for_dtype(dtype)
   ```
3. **Applied During Processing**:
   - Reprojection: `gdalwarp -r {resampling}`
   - Overview generation: `-co OVERVIEW_RESAMPLING={overview_resampling}`

## Manual Override

If needed, you can specify resampling methods manually:
```python
# For known categorical data
create_cog_gdal(
    input_file,
    output_file,
    resampling='nearest',
    overview_resampling='mode'
)
```

## Common Product Types

### Continuous Products:
- NDVI, EVI, NDWI, MNDWI (vegetation/water indices)
- Temperature, precipitation
- Elevation models (DEM, DSM, DTM)
- SAR backscatter
- True color, false color imagery

### Categorical Products:
- Land cover classification
- Binary masks
- Forest/non-forest maps
- Water/land masks
- Cloud masks

## Performance Impact

- **Nearest/Mode**: Fastest, preserves exact values
- **Bilinear/Average**: Slightly slower, smoother results
- **Cubic**: Slowest, smoothest (not used by default)

The automatic selection ensures:
✅ Data integrity is maintained
✅ Appropriate visual quality
✅ Optimal processing speed
✅ Correct statistical properties in overviews