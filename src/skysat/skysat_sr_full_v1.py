import numpy as np
import rasterio
import rasterio.shutil
from rasterio.enums import Resampling
import matplotlib.pyplot as plt
import os

# ══════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════

IN_PATH        = "/mnt/disasters1/data/esops/eventData/2026/Sinlaku_Guam/skysat/20260420T213658_ssc2_u0002"
SR_IMAGE_PATH  = os.path.join(IN_PATH, "20260420_213658_ssc2_u0002_analytic_SR_clip.tif")          
UDM_PATH       = os.path.join(IN_PATH, "20260420_213658_ssc2_u0002_udm2_clip.tif") 
OUTPUT_DIR     = "output_sr"                

# Optional Processing Toggles
USE_UDM        = False   # Set to False to skip UDM masking
CALC_NDVI      = False   # Set to False to skip NDVI calculation & export
CALC_EVI       = False   # Set to False to skip EVI calculation & export
CALC_NDWI      = False   # Set to False to skip NDWI calculation & export

# SkySat SR band indices (1-based) standard order
BLUE_BAND  = 1  
GREEN_BAND = 2
RED_BAND   = 3  
NIR_BAND   = 4

UDM_CLEAR_BAND = 1

# Planet SR scale factor
SCALE_FACTOR = 10000.0

# Explicit NoData value for derived index GeoTIFFs (Float32)
NODATA_VALUE = -9999.0

# Image Brightness Control
# < 1.0 brightens the image (e.g., 0.6 to 0.8 is typical for satellite data)
# > 1.0 darkens the image
RGB_GAMMA = 0.7  

# EVI coefficients (standard MODIS-based)
EVI_G  = 2.5
EVI_C1 = 6.0
EVI_C2 = 7.5
EVI_L  = 1.0

os.makedirs(os.path.join(IN_PATH, OUTPUT_DIR), exist_ok=True)

# ══════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════

def load_band(src, band_index):
    data = src.read(band_index).astype(np.float32)
    nodata = src.nodata
    if nodata is not None:
        data[data == nodata] = np.nan
    # Replace zeros with NaNs to act as a basic NoData mask for edges
    data[data == 0] = np.nan
    return data

def normalize_band(band, lower_pct=2, upper_pct=98, gamma=1.0):
    valid = band[~np.isnan(band)]
    if valid.size == 0:
        return np.zeros_like(band)
    
    lo = np.percentile(valid, lower_pct)
    hi = np.percentile(valid, upper_pct)
    
    stretched = np.clip((band - lo) / (hi - lo + 1e-10), 0, 1)
    
    if gamma != 1.0:
        stretched = np.power(stretched, gamma)
        
    return stretched

def build_rgb_composite(r_band, g_band, b_band, gamma=1.0):
    r = normalize_band(r_band, gamma=gamma)
    g = normalize_band(g_band, gamma=gamma)
    b = normalize_band(b_band, gamma=gamma)
    return np.dstack([r, g, b])

def convert_to_cog(src_path, cog_path):
    with rasterio.open(src_path, 'r+') as src:
        overviews = [2, 4, 8, 16]
        src.build_overviews(overviews, Resampling.nearest)
        src.update_tags(ns='rio_overview', resampling='nearest')
        
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        profile.update(
            driver='GTiff', tiled=True, blockxsize=256, blockysize=256,
            compress='lzw', interleave='pixel'
        )
        rasterio.shutil.copy(src, cog_path, copy_src_overviews=True, **profile)
    print(f"  ✔ COG Created: {cog_path}")

def write_rgb_uint8(output_path, r_band, g_band, b_band, profile, tags=None, gamma=1.0):
    out_profile = profile.copy()
    out_profile.update(
        dtype=rasterio.uint8, count=3, nodata=0, compress='lzw', photometric='RGB'
    )
    stack = []
    for band in [r_band, g_band, b_band]:
        stretched = normalize_band(band, gamma=gamma)
        uint8_band = (stretched * 255).astype(np.uint8)
        uint8_band[np.isnan(band)] = 0  
        stack.append(uint8_band)

    with rasterio.open(output_path, 'w', **out_profile) as dst:
        dst.write(np.stack(stack, axis=0))
        if tags:
            dst.update_tags(**tags)
    print(f"  ✔ Written: {output_path}")
    
    cog_path = output_path.replace(".tif", "_cog.tif")
    convert_to_cog(output_path, cog_path)

def write_geotiff(output_path, data, profile, band_count=1, nodata=-9999.0, tags=None):
    out_profile = profile.copy()
    out_profile.update(dtype=rasterio.float32, count=band_count, nodata=nodata, compress='lzw')
    
    out_data = data.copy()
    if not np.isnan(nodata):
        out_data[np.isnan(out_data)] = nodata

    with rasterio.open(output_path, 'w', **out_profile) as dst:
        if band_count == 1:
            dst.write(out_data.astype(np.float32), 1)
        else:
            dst.write(out_data.astype(np.float32))
        if tags:
            dst.update_tags(**tags)
    print(f"  ✔ Written: {output_path}")
    
    cog_path = output_path.replace(".tif", "_cog.tif")
    convert_to_cog(output_path, cog_path)

def print_stats(name, array):
    valid = array[~np.isnan(array)]
    if valid.size > 0:
        print(f"  {name:6s} → min: {np.nanmin(array):7.4f}  max: {np.nanmax(array):7.4f}  "
              f"mean: {np.nanmean(array):7.4f}  median: {np.nanmedian(array):7.4f}  "
              f"valid px: {valid.size:,}")
    else:
        print(f"  {name:6s} → No valid data.")

# ══════════════════════════════════════════════════════
# STEP 1: LOAD SR BANDS & SCALE
# ══════════════════════════════════════════════════════
print("\n── Step 1: Loading SR bands & converting to Reflectance ──")

with rasterio.open(SR_IMAGE_PATH) as src:
    profile = src.profile.copy()
    
    blue_dn  = load_band(src, BLUE_BAND)
    green_dn = load_band(src, GREEN_BAND)
    red_dn   = load_band(src, RED_BAND)
    nir_dn   = load_band(src, NIR_BAND)

# Scale DN to Surface Reflectance using the 10000.0 scale factor
blue  = np.clip(blue_dn  / SCALE_FACTOR, 0, 1)
green = np.clip(green_dn / SCALE_FACTOR, 0, 1)
red   = np.clip(red_dn   / SCALE_FACTOR, 0, 1)
nir   = np.clip(nir_dn   / SCALE_FACTOR, 0, 1)
print(f"  Loaded & converted {4} bands from: {SR_IMAGE_PATH}")

# ══════════════════════════════════════════════════════
# STEP 2: APPLY UDM2 MASK (OPTIONAL)
# ══════════════════════════════════════════════════════
print("\n── Step 2: Applying UDM2 Mask ─────────────────────────────")
udm_mask = None
if USE_UDM:
    if UDM_PATH and os.path.exists(UDM_PATH):
        with rasterio.open(UDM_PATH) as udm_src:
            clear = udm_src.read(UDM_CLEAR_BAND).astype(np.float32)

        udm_mask = (clear != 1)
        for band in [blue, green, red, nir]:
            band[udm_mask] = np.nan

        total_px   = udm_mask.size
        clear_px   = total_px - np.sum(udm_mask)
        print(f"  Clear pixels : {clear_px:,} ({100*clear_px/total_px:.1f}%)")
    else:
        print(f"  [!] UDM file not found at {UDM_PATH}. Skipping mask step.")
else:
    print("  USE_UDM is set to False. Skipping mask step.")

# ══════════════════════════════════════════════════════
# STEP 3: CALCULATE INDICES (OPTIONAL)
# ══════════════════════════════════════════════════════
print("\n── Step 3: Calculating Spectral Indices ───────────────────")

ndvi = evi = ndwi = None

if CALC_NDVI:
    with np.errstate(invalid='ignore', divide='ignore'):
        denom_ndvi = nir + red
        ndvi = np.where(denom_ndvi != 0, (nir - red) / denom_ndvi, np.nan)
    print_stats("NDVI",  ndvi)
else:
    print("  NDVI calculation skipped.")

if CALC_EVI:
    with np.errstate(invalid='ignore', divide='ignore'):
        denom_evi = nir + EVI_C1 * red - EVI_C2 * blue + EVI_L
        evi = np.where(denom_evi != 0, EVI_G * (nir - red) / denom_evi, np.nan)
        evi = np.clip(evi, -1, 1)
    print_stats("EVI",   evi)
else:
    print("  EVI calculation skipped.")

if CALC_NDWI:
    with np.errstate(invalid='ignore', divide='ignore'):
        denom_ndwi = green + nir
        ndwi = np.where(denom_ndwi != 0, (green - nir) / denom_ndwi, np.nan)
    print_stats("NDWI",  ndwi)
else:
    print("  NDWI calculation skipped.")

# ══════════════════════════════════════════════════════
# STEP 4: BUILD RGB COMPOSITES (display only)
# ══════════════════════════════════════════════════════
print("\n── Step 4: Building RGB Composites ────────────────────────")
true_color = build_rgb_composite(red, green, blue, gamma=RGB_GAMMA)   
cir        = build_rgb_composite(nir,  red,  green, gamma=RGB_GAMMA)  
print(f"  ✔ True Color RGB & CIR built (Gamma: {RGB_GAMMA})")

# ══════════════════════════════════════════════════════
# STEP 5: WRITE ALL GEOTIFFS & COGs
# ══════════════════════════════════════════════════════
print("\n── Step 5: Writing GeoTIFF & COG Outputs ──────────────────")
udm_applied = str(udm_mask is not None)

write_rgb_uint8(
    os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_TrueColor.tif"),
    r_band=red, g_band=green, b_band=blue, profile=profile,
    tags=dict(DESCRIPTION=f"True Color RGB (24-bit) — SkySat SR (Gamma {RGB_GAMMA})", UDM_APPLIED=udm_applied),
    gamma=RGB_GAMMA
)

write_rgb_uint8(
    os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_CIR.tif"),
    r_band=nir, g_band=red, b_band=green, profile=profile,
    tags=dict(DESCRIPTION=f"Color Infrared CIR (24-bit) — SkySat SR (Gamma {RGB_GAMMA})", UDM_APPLIED=udm_applied),
    gamma=RGB_GAMMA
)

if CALC_NDVI:
    write_geotiff(
        os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_NDVI.tif"), 
        ndvi, profile, nodata=NODATA_VALUE, 
        tags=dict(DESCRIPTION="NDVI — SkySat SR", UDM_APPLIED=udm_applied)
    )
if CALC_EVI:
    write_geotiff(
        os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_EVI.tif"), 
        evi, profile, nodata=NODATA_VALUE, 
        tags=dict(DESCRIPTION="EVI — SkySat SR", UDM_APPLIED=udm_applied)
    )
if CALC_NDWI:
    write_geotiff(
        os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_NDWI.tif"), 
        ndwi, profile, nodata=NODATA_VALUE, 
        tags=dict(DESCRIPTION="NDWI — SkySat SR", UDM_APPLIED=udm_applied)
    )
'''
# ══════════════════════════════════════════════════════
# STEP 6: VISUALIZATION — 2-ROW DASHBOARD
# ══════════════════════════════════════════════════════
print("\n── Step 6: Generating Visualization Dashboard ─────────────")
output_png = os.path.join(IN_PATH, OUTPUT_DIR, "skysat_sr_dashboard.png")

fig = plt.figure(figsize=(24, 14))
fig.suptitle(f"SkySat Derived Products Dashboard\nSurface Reflectance — True Color (Gamma {RGB_GAMMA}) | CIR | NDVI | EVI | NDWI", fontsize=15, fontweight='bold', y=0.98)

veg_cmap   = plt.cm.RdYlGn   
water_cmap = plt.cm.RdYlBu   

ax1 = fig.add_subplot(2, 3, 1)
ax1.imshow(true_color)
ax1.set_title("True Color RGB", fontsize=10)
ax1.axis('off')

ax2 = fig.add_subplot(2, 3, 2)
ax2.imshow(cir)
ax2.set_title("Color Infrared (CIR)", fontsize=10)
ax2.axis('off')

# Panel 3: NDVI Map
ax3 = fig.add_subplot(2, 3, 3)
if CALC_NDVI:
    im3 = ax3.imshow(ndvi, cmap=veg_cmap, vmin=-1, vmax=1)
    ax3.set_title("NDVI", fontsize=10)
    plt.colorbar(im3, ax=ax3, fraction=0.046, pad=0.04, label='NDVI')
else:
    ax3.set_title("NDVI (Skipped)", fontsize=10)
ax3.axis('off')

# Panel 4: EVI Map
ax4 = fig.add_subplot(2, 3, 4)
if CALC_EVI:
    im4 = ax4.imshow(evi, cmap=veg_cmap, vmin=-1, vmax=1)
    ax4.set_title("EVI", fontsize=10)
    plt.colorbar(im4, ax=ax4, fraction=0.046, pad=0.04, label='EVI')
else:
    ax4.set_title("EVI (Skipped)", fontsize=10)
ax4.axis('off')

# Panel 5: NDWI Map
ax5 = fig.add_subplot(2, 3, 5)
if CALC_NDWI:
    im5 = ax5.imshow(ndwi, cmap=water_cmap, vmin=-1, vmax=1)
    ax5.set_title("NDWI", fontsize=10)
    plt.colorbar(im5, ax=ax5, fraction=0.046, pad=0.04, label='NDWI')
else:
    ax5.set_title("NDWI (Skipped)", fontsize=10)
ax5.axis('off')

# Panel 6: Histograms
ax6 = fig.add_subplot(2, 3, 6)
hist_data = []
if CALC_NDVI: hist_data.append((ndvi, "NDVI", "forestgreen"))
if CALC_EVI:  hist_data.append((evi, "EVI", "darkorange"))
if CALC_NDWI: hist_data.append((ndwi, "NDWI", "steelblue"))

if hist_data:
    for arr, label, color in hist_data:
        valid = arr[~np.isnan(arr)].flatten()
        if valid.size > 0:
            ax6.hist(valid, bins=150, alpha=0.5, color=color, label=f"{label} (μ={np.nanmean(arr):.3f})", edgecolor='none')
    ax6.axvline(0, color='black', linestyle='--', linewidth=1.0)
    ax6.legend(fontsize=8)
else:
    ax6.text(0.5, 0.5, 'No indices calculated', ha='center', va='center', fontsize=12)

ax6.set_title("Index Distributions", fontsize=10)
ax6.set_xlim(-1, 1)

plt.tight_layout(rect=[0, 0, 1, 0.96])
plt.savefig(output_png, dpi=150, bbox_inches='tight')
'''
#print(f"  ✔ Dashboard saved: {output_png}")
print("\n✅ All SR products & COGs complete!")
