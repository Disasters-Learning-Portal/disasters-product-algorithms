"""
landcover.py

Land-cover references for the water-extent products: the USDA Cropland Data
Layer (CONUS) and ESA WorldCover (elsewhere), reclassified into the buckets
the water-extent classifier uses.

ONE table for every pipeline. The Sentinel-2 STAC path, the Sentinel-2 .SAFE
path and Landsat each used to carry their own copy; CDL 112 (Perennial
Ice/Snow) was fixed in one copy and left mapped to permanent water in the
other two (.clinerules.md rules 54 and 65).

Codes and names verified against the USDA NASS CDL code list
(https://www.nass.usda.gov/Research_and_Science/Cropland/sarsfaqs2.php) and,
for the NLCD-derived classes 110-195, the NLCD legend
(https://www.mrlc.gov/data/legends/national-land-cover-database-class-legend-and-description):
  111 Open Water          "less than 25% cover of vegetation or soil"
  112 Perennial Ice/Snow  "perennial cover of ice and/or snow, generally
                           greater than 25% of total cover"
  190/195 Woody/Herbaceous Wetlands -- substrate "periodically saturated with
                           or covered with water"
"""

import numpy as np


# CDL codes allowed to seed the permanent-water NIR statistics.
# 111 = NLCD "Open Water" (<25% vegetation or soil cover) -- the
# cleanest available endmember. Deliberately excludes 92 Aquaculture
# and the legacy 83, both of which are labelled water in the product
# but are poor reflectance references.
CDL_WATER_SAMPLE_CODES = frozenset({111})

# WorldCover codes allowed to seed the permanent-water NIR statistics.
# 80 = "Permanent water bodies" is the only water class in the legend.
WORLDCOVER_WATER_SAMPLE_CODES = frozenset({80})


def reclass_cdl_array(
    cdl_array,
    nir_nodata_mask,
):
    """
    Reclassify the USDA CDL into the water-extent buckets.

    Buckets:
        1 = cropland/grassland
        2 = developed
        3 = other non-developed land cover
        4 = permanent water
        999 = no data / excluded

    Returns
    -------
    translated : numpy.ndarray (uint16)
        The bucket per pixel.
    sample_mask : numpy.ndarray (bool)
        Pixels eligible to seed the permanent-water NIR statistics.
        A strict subset of bucket 4 -- see CDL_WATER_SAMPLE_CODES.
    """

    codes_dict = {
        1: 1, 2: 1, 3: 1, 4: 1, 5: 1,
        6: 1, 10: 1, 11: 1, 12: 1,
        13: 1, 14: 1, 21: 1, 22: 1,
        23: 1, 24: 1, 25: 1, 26: 1,
        27: 1, 28: 1, 29: 1, 30: 1,
        31: 1, 32: 1, 33: 1, 34: 1,
        35: 1, 36: 1, 37: 1, 38: 1,
        39: 1, 41: 1, 42: 1, 43: 1,
        44: 1, 45: 1, 46: 1, 47: 1,
        48: 1, 49: 1, 50: 1, 51: 1,
        52: 1, 53: 1, 54: 1, 55: 1,
        56: 1, 57: 1, 58: 1, 59: 1,
        60: 1, 61: 1, 66: 1, 67: 1,
        68: 1, 69: 1, 70: 1, 71: 1,
        72: 1, 74: 1, 75: 1, 76: 1,
        77: 1, 204: 1, 205: 1,
        206: 1, 207: 1, 208: 1,
        209: 1, 210: 1, 211: 1,
        212: 1, 213: 1, 214: 1,
        215: 1, 216: 1, 217: 1,
        218: 1, 219: 1, 220: 1,
        221: 1, 222: 1, 223: 1,
        224: 1, 225: 1, 226: 1,
        227: 1, 228: 1, 229: 1,
        230: 1, 231: 1, 232: 1,
        233: 1, 234: 1, 235: 1,
        236: 1, 237: 1, 238: 1,
        239: 1, 240: 1, 241: 1,
        242: 1, 243: 1, 244: 1,
        245: 1, 246: 1, 247: 1,
        248: 1, 249: 1, 250: 1,
        254: 1, 176: 1,

        # --- 1: cropland / grassland (non-crop members) ---
        # 62 Pasture/Grass is a pre-2008 legacy code, retired in 2013
        # and collapsed into 176.
        62: 1,

        # --- 2: developed ---
        # 82 is the pre-2008 legacy "Developed" before it was split
        # into the 121-124 intensity classes.
        82: 2,
        121: 2, 122: 2, 123: 2, 124: 2,

        # --- 3: other non-developed land cover ---
        # Named "other vegetation" historically, but it is really a
        # catch-all for not-crop / not-developed / not-water. Barren
        # (65, 131) and Nonag/Undefined (88) are NOT vegetation; they
        # live here so that sand bars, beaches, playas and gravel --
        # highly flood-relevant terrain, 131 alone is ~78,000 km2 of
        # CONUS -- remain eligible to be mapped as flooded rather than
        # silently excluded.
        #
        # Wetlands (87, 190, 195) belong here, NOT in permanent water.
        # They are vegetation canopy over periodically saturated
        # ground: bright in NIR, so they would contaminate the water
        # sample, and only intermittently inundated, so calling them
        # permanent water would blind the product to flooding in
        # exactly the places flooding is most expected.
        63: 3, 64: 3, 65: 3, 87: 3, 88: 3,
        131: 3, 141: 3, 142: 3,
        143: 3, 152: 3, 190: 3,
        195: 3,

        # --- 4: permanent water ---
        # 83 Water is the pre-2008 legacy code; 92 Aquaculture and
        # 111 Open Water are current.
        83: 4, 92: 4, 111: 4,

        # --- 999: no data / excluded ---
        # 112 is Perennial Ice/Snow (NLCD class 12), NOT water. It was
        # mapped to permanent water, which is the single most damaging
        # entry in this table: snow reflects ~0.6-0.9 at B08's 842 nm
        # against ~0.01-0.05 for open water, so including it inflates
        # both the mean and the standard deviation of the water NIR
        # sample, and the threshold is mean + nstd*std. Simulated at 5%
        # contamination the threshold moves 1550 -> 3391 DN and false
        # water goes from 0.3% to 25% of land. Snow only darkens in
        # SWIR (~1600 nm), which is what NDSI exploits -- not at 842 nm.
        # It is present in every CONUS CDL year (0.5-1.6M pixels).
        112: 999,
        0: 999,
        81: 999,
    }

    translated = np.full(
        cdl_array.shape,
        999,
        dtype=np.uint16,
    )

    for source_code, target_code in codes_dict.items():

        translated[
            cdl_array == source_code
        ] = target_code

    translated[
        nir_nodata_mask
    ] = 999

    # Seed the NIR statistics from NLCD "Open Water" only. Bucket 4
    # also holds 92 Aquaculture, which is genuinely water and belongs
    # in the product label, but aquaculture ponds are turbid, algal and
    # periodically drained -- all of which raise NIR and widen the
    # threshold. 111 is the only class NLCD defines as open water with
    # under 25% vegetation or soil cover.
    sample_mask = (
        np.isin(cdl_array, list(CDL_WATER_SAMPLE_CODES))
        & ~nir_nodata_mask
    )

    return translated, sample_mask


def reclass_worldcover_array(
    worldcover_array,
    nir_nodata_mask,
):
    """
    Reclassify ESA WorldCover (v100/2020, v200/2021 -- the legend is
    byte-identical between them) into the water-extent buckets.

    Buckets:
        1 = cropland/grassland
        2 = developed
        3 = other non-developed land cover
        4 = permanent water
        999 = no data / excluded

    Returns
    -------
    translated, sample_mask : see reclass_cdl_array.
    """

    codes_dict = {
        0: 999,    # No data
        10: 3,     # Tree cover
        20: 3,     # Shrubland
        30: 1,     # Grassland
        40: 1,     # Cropland
        50: 2,     # Built-up
        # 60 is "Bare / sparse vegetation", defined as never exceeding
        # 10% vegetated cover. It sits in bucket 3 for coverage, not
        # because it is vegetation -- see the bucket-3 note in
        # reclass_cdl_array.
        60: 3,
        # 70 Snow and ice is EXCLUDED, for the same NIR-brightness
        # reason CDL 112 is. Note this does not cost us frozen lakes:
        # water frozen for less than 9 months stays class 80.
        70: 999,
        80: 4,     # Permanent water bodies
        # 90 Herbaceous wetland / 95 Mangroves are vegetation over
        # water -- bright in NIR, and only intermittently inundated.
        # Not permanent water. Same reasoning as CDL 190/195.
        90: 3,
        95: 3,
        100: 3,    # Moss and lichen
    }

    translated = np.full(
        worldcover_array.shape,
        999,
        dtype=np.uint16,
    )

    for source_code, target_code in codes_dict.items():

        translated[
            worldcover_array == source_code
        ] = target_code

    translated[
        nir_nodata_mask
    ] = 999

    sample_mask = (
        np.isin(worldcover_array, list(WORLDCOVER_WATER_SAMPLE_CODES))
        & ~nir_nodata_mask
    )

    return translated, sample_mask


# A strict sample this small gives an unstable mean/std; below it the
# threshold falls back to the whole permanent-water bucket.
MIN_WATER_SAMPLE = 100


def select_water_sample(permanent_water_mask, sample_mask):
    """Pick the pixels that seed the NIR water threshold.

    `permanent_water_mask` is what gets LABELLED permanent water (all of
    bucket 4); `sample_mask` is the strict reflectance endmember (CDL 111 /
    WorldCover 80). Both must already exclude cloud and nodata.

    Falls back to the full bucket when the strict sample has fewer than
    MIN_WATER_SAMPLE pixels -- a scene whose only water is aquaculture is
    better served by a slightly biased threshold than by no product.
    Raises when there is no water at all: mean/std of an empty sample is NaN,
    `nir <= NaN` is False everywhere, and the product would silently report
    no flooding.
    """
    water_count = int(np.count_nonzero(permanent_water_mask))
    sample_count = int(np.count_nonzero(sample_mask))
    print(f"\t* Permanent-water reference pixels: {water_count:,}")
    print(f"\t* Threshold sample pixels:          {sample_count:,}")

    if sample_count < MIN_WATER_SAMPLE and water_count >= MIN_WATER_SAMPLE:
        print(f"\t* Sample below {MIN_WATER_SAMPLE} px; falling back to the "
              f"full permanent-water bucket for the threshold.")
        return permanent_water_mask

    if sample_count == 0:
        raise RuntimeError(
            "No valid permanent-water pixels were found after applying the "
            "land-cover and cloud masks. Cannot calculate the NIR water "
            "threshold.")

    return sample_mask


# On-disk reclass cache used by the .SAFE and Landsat water extents: band 1 =
# bucket, band 2 = threshold-sample flag. The suffix is VERSIONED so a cache
# written before CDL 112 left the water bucket (and before the sample band
# existed) is never picked up again -- a stale `_reclass.tif` would silently
# reintroduce snow into the threshold.
RECLASS_SUFFIX = '_reclass_v2.tif'


def reclass_path(reference_path):
    return reference_path.replace('.tif', RECLASS_SUFFIX)


def write_reclass(path, buckets, sample_mask, crs, transform):
    import rasterio
    with rasterio.open(path, 'w', driver='GTiff', count=2, dtype='uint16',
                       height=buckets.shape[0], width=buckets.shape[1],
                       crs=crs, transform=transform) as dst:
        dst.write(buckets.astype('uint16'), 1)
        dst.write(sample_mask.astype('uint16'), 2)
    return path


def read_reclass(path):
    """Return (buckets, sample_mask) from a write_reclass file."""
    import rasterio
    with rasterio.open(path) as src:
        return src.read(1), src.read(2).astype(bool)
