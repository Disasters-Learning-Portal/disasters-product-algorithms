import os
import re
import numpy as np
from osgeo import gdal
from datetime import datetime
import json
from typing import Literal, Union
import rasterio

from shared_utils.s3utils import *
from shared_utils.geotools import *
from shared_utils.product_paths import product_output_dir

NODATA_FLOAT = -9999.0

# UDM2 band 1 is Planet's "clear" layer: 1 = clear, anything else is not.
UDM_CLEAR_BAND = 1

# Planet's documented layouts. The 4-band analytic assets are B,G,R,NIR; the
# 3-band visual asset is R,G,B -- NOT the first three analytic bands.
BAND_ORDER = {
    "analytic": {"blue": 1, "green": 2, "red": 3, "nir": 4},
    "basic_analytic": {"blue": 1, "green": 2, "red": 3, "nir": 4},
    "visual": {"red": 1, "green": 2, "blue": 3},
}

# <LEVEL> token of the output name. DN x reflectance_coefficients is
# top-of-atmosphere reflectance, so an analytic-derived product is never "SR".
LEVEL_TOKEN = {"analytic": "TOA", "basic_analytic": "TOA", "visual": "Visual"}

_GCI_ROLE = {
    gdal.GCI_RedBand: "red",
    gdal.GCI_GreenBand: "green",
    gdal.GCI_BlueBand: "blue",
}


def band_order_from_colorinterp(ds):
    """Map role -> 1-based band index from the file's own ColorInterp, or None.

    Same rule as ``satellogic_v2.band_order_from_colorinterp``: trusted ONLY when
    red, green and blue are each declared exactly once. GDAL defaults band 1 of
    a plain multi-band TIFF to ``Gray``, so a partial declaration is a driver
    default, not a vendor statement. NIR is never declared, so on a 4-band file
    it is the single leftover band; any other leftover count rejects the read.
    """
    seen = {}
    for i in range(1, ds.RasterCount + 1):
        role = _GCI_ROLE.get(ds.GetRasterBand(i).GetColorInterpretation())
        if role:
            seen.setdefault(role, []).append(i)

    if set(seen) != {"red", "green", "blue"}:
        return None
    if any(len(bands) != 1 for bands in seen.values()):
        return None

    idx = {role: bands[0] for role, bands in seen.items()}

    leftover = [i for i in range(1, ds.RasterCount + 1) if i not in idx.values()]
    if len(leftover) == 1:
        idx["nir"] = leftover[0]
    elif leftover:
        return None
    return idx


def resolve_band_indices(ds, product_type):
    """Resolve {red,green,blue[,nir]} -> band index for THIS file.

    Prefers the file's own ColorInterp, else Planet's documented layout for the
    asset type. ALWAYS prints which source decided: a transposed band order is
    invisible in the output, because ``normalize_band`` stretches each band
    independently and an index over the wrong bands still lands in [-1, 1].
    """
    idx = band_order_from_colorinterp(ds)
    if idx:
        print(f"Band order from the file's own ColorInterp: {idx}")
        return idx

    idx = BAND_ORDER[product_type]
    print(f"Band order from the Planet {product_type} layout "
          f"(file declares no usable ColorInterp): {idx}")
    return idx


def fetch_local(s3_path, cache_dir="/tmp/s3_temp"):
    """Return a local path for ``s3_path``, downloading unless already cached."""
    local_path = f"{cache_dir}/{local_tif_basename(s3_path)}"
    if os.path.exists(local_path):
        print(f"Found locally: {local_path}")
        return local_path
    print(f"Downloading from s3: {s3_path}")
    return download_s3_file(s3_path, cache_dir)


def load_reflectance(ds, roles, product_type="analytic"):
    """Read ``roles`` as 0-1 TOA reflectance, with Planet's DN-0 fill as NaN.

    The fill must become NaN here rather than be left to the index arithmetic:
    NDVI/NDWI happen to yield 0/0 over fill, but EVI's denominator carries a
    ``+ 1`` and turns fill into a finite, plausible 0.0.
    """
    band = resolve_band_indices(ds, product_type)
    if "nir" in roles and "nir" not in band:
        raise ValueError(
            f"A {product_type} file has no NIR band; this product needs an "
            f"analytic or basic_analytic asset."
        )
    coeffs = get_reflectance_coefficients(ds)

    out = []
    for role in roles:
        dn = ds.GetRasterBand(band[role]).ReadAsArray().astype(np.float32)
        dn[dn == 0] = np.nan
        out.append(np.clip(dn * coeffs[band[role] - 1], 0, 1))
    return out


_SKYSAT_NAME_RE = re.compile(
    r"^(\d{8}_\d{6})_(.+)_(analytic|visual|basic_analytic)\.tiff?$", re.IGNORECASE
)


def build_output_name(in_file, out_dir, product):
    """Derive the output name from a SkySat asset basename.

      20260420_213658_ssc2_u0002_analytic.tif
        -> <out_dir>/NDVI/SkySat_TOA_NDVI_ssc2_u0002_2026-04-20T21:36:58Z.tif

    ``product`` is the PascalCase ``product_paths.PRODUCT_DIRS`` key. The scene
    token stays in the name because one collect holds several scenes (u0001,
    u0002, ...; basic_analytic tiles) that share a timestamp and would
    otherwise overwrite each other. The stem ends in ISO-Zulu, so it is a fixed
    point of ``create_output_filename`` / ``rename_with_event``.
    """
    m = _SKYSAT_NAME_RE.match(os.path.basename(in_file))
    if not m:
        raise ValueError(f"Unrecognized SkySat asset name: {os.path.basename(in_file)}")

    stamp = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    level = LEVEL_TOKEN[m.group(3).lower()]

    prod_dir = product_output_dir(out_dir, "skysat", product)
    return os.path.join(prod_dir, f"SkySat_{level}_{product}_{m.group(2)}_{stamp}.tif")


def retrieve_skysat_resources(date: Union[str, datetime], bucket="csdap-planet-skysat-delivery", prefix="disasters"):
    files = retrieve_s3_file_list(bucket, prefix)

    filtered_files = [x for x in files if len(x.split("/")) > 2]

    subdirs = {}
    event_dirs = sorted(set(x.split("/")[1] for x in filtered_files))

    for event_dir in event_dirs:
        event_subdirs = sorted(
            set(
                x.split("/")[2]
                for x in filtered_files
                if x.split("/")[1] == event_dir
            )
        )

        for event_subdir in event_subdirs:
            folder_files = [
                x for x in filtered_files
                if x.split("/")[1] == event_dir
                and x.split("/")[2] == event_subdir
            ]

            if not folder_files:
                continue

            filename = folder_files[0].split("/")[-1]
            date_string = f"{filename.split('_')[0]}_{filename.split('_')[1]}"
            acquisition_date = datetime.strptime(date_string, "%Y%m%d_%H%M%S")

            subdirs[acquisition_date] = f"{prefix}/{event_dir}/{event_subdir}"

    if isinstance(date, str):
        date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

    closest_date = min(
        subdirs,
        key=lambda d: abs(d - date)
    )

    selected = subdirs[closest_date]

    selected_files = [
        x for x in filtered_files
        if x.startswith(selected)
    ]

    tifs = [
        x for x in selected_files
        if x.lower().endswith((".tif", ".tiff"))
    ]

    print(f"Selected SkySat folder: {selected}")
    print(f"TIF files found: {len(tifs)}")
    for t in tifs:
        print("  ", t)

    return [f"s3://{bucket}/{x}" for x in tifs]


def normalize_band(band, lower_pct=2, upper_pct=98, gamma=1.0):
    valid = band[~np.isnan(band)]
    if valid.size == 0:
        return np.zeros_like(band)
    
    lo = np.percentile(valid, lower_pct)
    hi = np.percentile(valid, upper_pct)
    
    stretched = np.clip((band - lo) / (hi - lo + 1e-10), 0, 1)
    
    if gamma != 1.0:
        stretched = np.power(stretched, 1.0/gamma)
        
    return stretched
    

def print_skysat_stats(name, array):
    valid = array[~np.isnan(array)]
    if valid.size > 0:
        print(f"  {name:6s} → min: {np.nanmin(array):7.4f}  max: {np.nanmax(array):7.4f}  "
              f"mean: {np.nanmean(array):7.4f}  median: {np.nanmedian(array):7.4f}  "
              f"valid px: {valid.size:,}")
    else:
        print(f"  {name:6s} → No valid data.")


def udm_mask(s3_image_paths: list[str], image_filepath: str, bands: list[np.ndarray]):
    """
    Apply the UDM2 mask corresponding to the specific SkySat image.
    u0001/u0002 identify different scenes, not TOA/SFC.
    """
    image_name = os.path.basename(image_filepath)

    udm_name = re.sub(
        r"_(?:analytic|visual|basic_analytic)\.(?:tif|tiff)$",
        "_udm2.tif",
        image_name,
        flags=re.IGNORECASE,
    )

    udm_filepaths = [
        x for x in s3_image_paths
        if os.path.basename(x).lower() == udm_name.lower()
    ]

    if not udm_filepaths:
        print(f"  [!] UDM2 file not found for {image_name}. Skipping mask step.")
        return bands

    in_file = fetch_local(udm_filepaths[0])

    with rasterio.open(in_file) as udm_src:
        clear = udm_src.read(UDM_CLEAR_BAND).astype(np.float32)

    mask = (clear != 1)

    for band in bands:
        if band is not None:
            band[mask] = np.nan

    total_px = mask.size
    clear_px = total_px - np.sum(mask)
    print(f"  Clear pixels : {clear_px:,} ({100*clear_px/total_px:.1f}%)")

    return bands


def get_skysat_product_files(
    s3_image_paths: list[str],
    product_type: Literal["visual", "analytic", "basic_analytic"],
):
    """
    Select SkySat files for the requested product.

    Regular analytic/visual:
        ..._ssc1_u0001_analytic.tif
        ..._ssc1_u0002_analytic.tif
        ..._ssc1_u0001_visual.tif
        ..._ssc1_u0002_visual.tif

    u0001/u0002 are separate scenes and BOTH are processed.

    basic_analytic:
        ..._ssc1d1_0001_basic_analytic.tif
        ..._ssc1d1_0002_basic_analytic.tif
        ...

    The tiled ssc<N>d files are ignored for regular analytic/visual products,
    but all basic_analytic tiles are included when basic_analytic is selected.

    The satellite number is matched as ``ssc\\d+``: the constellation is
    ssc1..ssc21, and a literal ``ssc1`` selects nothing for any other satellite.
    """
    if product_type in ("analytic", "visual"):
        pattern = re.compile(
            rf"_ssc\d+_u\d+_{re.escape(product_type)}\.(?:tif|tiff)$",
            re.IGNORECASE,
        )
    elif product_type == "basic_analytic":
        pattern = re.compile(
            r"_ssc\d+d\d+_\d+_basic_analytic\.(?:tif|tiff)$",
            re.IGNORECASE,
        )
    else:
        raise ValueError(f"Unsupported SkySat product type: {product_type}")

    files = sorted(
        x for x in s3_image_paths
        if pattern.search(os.path.basename(x))
    )

    if not files:
        raise ValueError(
            f"No SkySat {product_type} files found for the selected acquisition."
        )

    print(f"SkySat {product_type} files selected: {len(files)}")
    for filepath in files:
        print(f"  {filepath}")

    return files


def get_reflectance_coefficients(ds):
    """Extract SkySat reflectance coefficients from ImageDescription metadata."""
    metadata = ds.GetMetadata()

    for key in ("TIFFTAG_IMAGEDESCRIPTION", "ImageDescription"):
        if key in metadata:
            try:
                meta_json = json.loads(metadata[key])
                coeffs = meta_json["properties"]["reflectance_coefficients"]
                print(f"  ✔ Extracted reflectance coefficients: {coeffs}")
                return coeffs
            except (json.JSONDecodeError, KeyError) as e:
                raise ValueError(
                    f"Failed to parse reflectance_coefficients from ImageDescription. "
                    f"Error: {e}"
                )

    raise ValueError("Could not find 'ImageDescription' tag in the GeoTIFF.")


def georeference_rgb_with_rpc(
    in_file: str,
    outfile: str,
    band1: np.ndarray,
    band2: np.ndarray,
    band3: np.ndarray,
    cols: int,
    rows: int,
):
    """
    Create an RGB GeoTIFF and georeference it using the RPC metadata
    from the original SkySat image.

    band1, band2, band3 are uint8 arrays representing the output
    RGB channels.
    """

    # Temporary RGB file before RPC orthorectification
    temp_rgb = f"{outfile}.tmp.tif"

    print("  Creating temporary RGB image...")

    driver = gdal.GetDriverByName("GTiff")

    out_ds = driver.Create(
        temp_rgb,
        cols,
        rows,
        3,
        gdal.GDT_Byte,
        options=[
            "COMPRESS=LZW",
            "TILED=YES"
        ]
    )

    if out_ds is None:
        raise RuntimeError(f"Could not create temporary RGB file: {temp_rgb}")

    # Write RGB bands
    out_ds.GetRasterBand(1).WriteArray(band1)
    out_ds.GetRasterBand(2).WriteArray(band2)
    out_ds.GetRasterBand(3).WriteArray(band3)

    # Set color interpretation
    out_ds.GetRasterBand(1).SetColorInterpretation(gdal.GCI_RedBand)
    out_ds.GetRasterBand(2).SetColorInterpretation(gdal.GCI_GreenBand)
    out_ds.GetRasterBand(3).SetColorInterpretation(gdal.GCI_BlueBand)

    out_ds.FlushCache()
    out_ds = None

    # ---------------------------------------------------------
    # Copy RPC metadata from original SkySat image
    # ---------------------------------------------------------

    print("  Copying RPC metadata...")

    src_ds = gdal.Open(in_file)

    if src_ds is None:
        raise RuntimeError(f"Could not open source file: {in_file}")

    rpc_metadata = src_ds.GetMetadata("RPC")

    if not rpc_metadata:
        src_ds = None
        raise ValueError(
            "No RPC metadata found in the SkySat source image."
        )

    rgb_ds = gdal.Open(temp_rgb, gdal.GA_Update)

    if rgb_ds is None:
        src_ds = None
        raise RuntimeError(
            f"Could not reopen temporary RGB file: {temp_rgb}"
        )

    rgb_ds.SetMetadata(rpc_metadata, "RPC")

    rgb_ds.FlushCache()
    rgb_ds = None
    src_ds = None

    # ---------------------------------------------------------
    # RPC orthorectification
    # ---------------------------------------------------------

    print("  Orthorectifying using SkySat RPC metadata...")

    warp_options = gdal.WarpOptions(
        format="GTiff",
        rpc=True,
        dstSRS="EPSG:4326",
        resampleAlg="bilinear",
        creationOptions=[
            "COMPRESS=LZW",
            "TILED=YES"
        ]
    )

    result = gdal.Warp(
        outfile,
        temp_rgb,
        options=warp_options
    )

    if result is None:
        raise RuntimeError(
            "GDAL RPC orthorectification failed."
        )

    result.FlushCache()
    result = None

    # Remove temporary file
    try:
        os.remove(temp_rgb)
    except OSError:
        pass

    print(f"  RPC georeferencing completed: {outfile}")

def _ndvi(nir, red):
    denom = nir + red
    return np.where(denom != 0, (nir - red) / denom, np.nan)


def _ndwi(green, nir):
    # McFeeters (green - NIR): the water index, not Gao's NIR/SWIR moisture one.
    denom = green + nir
    return np.where(denom != 0, (green - nir) / denom, np.nan)


def _evi(blue, red, nir):
    # The denominator passes THROUGH zero (a large blue drives it negative), so
    # guard on magnitude; clip rather than nodata, since EVI legitimately
    # exceeds 1 over dense canopy. Same constants as the other sensors.
    denom = nir + 6.0 * red - 7.5 * blue + 1.0
    evi = np.where(np.abs(denom) > 1e-6, 2.5 * (nir - red) / denom, np.nan)
    return np.clip(evi, -1, 1)


# product -> (band roles in call order, formula)
_INDEX_FORMULAS = {
    "NDVI": (("nir", "red"), _ndvi),
    "NDWI": (("green", "nir"), _ndwi),
    "EVI": (("blue", "red", "nir"), _evi),
}


def _calc_index(product, s3_image_paths, save_location):
    roles, formula = _INDEX_FORMULAS[product]
    output_files = []

    for in_filepath in get_skysat_product_files(s3_image_paths, "analytic"):
        print(f"\nGenerating {product} for {in_filepath}")

        ds = gdal.Open(fetch_local(in_filepath))
        if ds is None:
            raise RuntimeError(f"Could not open SkySat file: {in_filepath}")
        in_geo = ds.GetGeoTransform()
        projref = ds.GetProjectionRef()

        bands = load_reflectance(ds, roles)
        bands = udm_mask(s3_image_paths, in_filepath, bands)

        with np.errstate(invalid="ignore", divide="ignore"):
            index = formula(*bands).astype(np.float32)

        print_skysat_stats(product, index)
        index[np.isnan(index)] = NODATA_FLOAT

        outfile = build_output_name(in_filepath, save_location, product)
        dump_geotiff_float(outfile, index, projref, in_geo)
        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def calc_ndvi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    return _calc_index("NDVI", s3_image_paths, save_location)


def calc_evi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    return _calc_index("EVI", s3_image_paths, save_location)


def calc_ndwi(s3_image_paths: list[str], save_location: str = "/tmp/s3_temp"):
    return _calc_index("NDWI", s3_image_paths, save_location)


def _produce_composite(product, roles, s3_image_paths, product_type, save_location, gamma):
    """Write an 8-bit 3-band composite whose output channels are ``roles`` in order."""
    output_files = []

    for in_filepath in get_skysat_product_files(s3_image_paths, product_type):
        print(f"\nProcessing {product}: {in_filepath}")

        in_file = fetch_local(in_filepath)
        ds = gdal.Open(in_file)
        if ds is None:
            raise RuntimeError(f"Could not open SkySat file: {in_file}")

        if product_type == "visual":
            print("  Visual file selected; reflectance coefficients will not be applied.")
            band = resolve_band_indices(ds, product_type)
            arrays = [
                np.clip(ds.GetRasterBand(band[role]).ReadAsArray(), 0, 255) / 255.0
                for role in roles
            ]
        else:
            arrays = load_reflectance(ds, roles, product_type)

        channels = []
        for arr in arrays:
            arr = normalize_band(arr, gamma=gamma)
            arr[np.isnan(arr)] = 0
            channels.append((arr * 255).astype(np.uint8))

        outfile = build_output_name(in_filepath, save_location, product)

        if product_type == "basic_analytic":
            georeference_rgb_with_rpc(
                in_file, outfile, *channels, ds.RasterXSize, ds.RasterYSize
            )
        else:
            dump_geotiff_rgb(
                outfile, *channels, ds.GetProjectionRef(), ds.GetGeoTransform()
            )

        print(f"Generation completed, file saved to {outfile}")
        output_files.append(outfile)

    return output_files


def produce_truecolor(
    s3_image_paths: list[str],
    product_type: Literal["visual", "analytic", "basic_analytic"],
    save_location: str = "/tmp/s3_temp",
    gamma: float = 2.2,
):
    return _produce_composite(
        "TrueColor", ("red", "green", "blue"),
        s3_image_paths, product_type, save_location, gamma,
    )


def produce_colorir(
    s3_image_paths: list[str],
    product_type: Literal["analytic", "basic_analytic"],
    save_location: str = "/tmp/s3_temp",
    gamma: float = 2.2,
):
    if product_type == "visual":
        raise ValueError(
            "Color IR requires an analytic or basic_analytic file "
            "because the visual product does not provide the NIR band."
        )
    # Color IR = NIR, Red, Green.
    return _produce_composite(
        "ColorIR", ("nir", "red", "green"),
        s3_image_paths, product_type, save_location, gamma,
    )
