"""
Simple matplotlib previews for COG outputs.

Matplotlib is an OPTIONAL dep; imports happen at call time so that
``shared_utils`` stays importable on machines without matplotlib (which
includes the CLI smoke test in ``.github/workflows/lint.yml``).

Use from a notebook:

    from shared_utils.plotting import preview_cogs
    preview_cogs(cog_files, sample_n=4)
"""

import os


def plot_cog(src, title=None, max_dim=1024):
    """Render a 2-panel preview (image + histogram) of a COG's band 1.

    Decimated read via rasterio's ``out_shape`` pulls from the COG's
    overviews when present and downsamples on-the-fly otherwise, so this
    stays fast even on multi-GB inputs.

    Args:
        src: local path, ``/vsis3/...`` URI, or ``s3://...`` URI.
        title: figure title; defaults to ``basename(src)``.
        max_dim: max output dimension for the decimated read (default 1024).
    """
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    if isinstance(src, str) and src.startswith("s3://"):
        src = src.replace("s3://", "/vsis3/", 1)

    with rasterio.open(src) as ds:
        scale = max(ds.height, ds.width) / max_dim
        if scale > 1:
            out_h = int(ds.height / scale)
            out_w = int(ds.width / scale)
        else:
            out_h, out_w = ds.height, ds.width
        arr = ds.read(
            1,
            out_shape=(out_h, out_w),
            resampling=Resampling.average,
        ).astype(float)
        nodata = ds.nodata

    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)

    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        print(f"  ⚠️ {src}: all-nodata band, skipping plot")
        return

    vmin, vmax = np.nanpercentile(valid, (2, 98))
    fig, (ax_img, ax_hist) = plt.subplots(1, 2, figsize=(14, 5))

    im = ax_img.imshow(arr, cmap="gray", vmin=vmin, vmax=vmax)
    ax_img.set_title(title or os.path.basename(str(src)))
    ax_img.axis("off")
    plt.colorbar(im, ax=ax_img, shrink=0.8)

    ax_hist.hist(valid.flatten(), bins=100, color="steelblue", edgecolor="black")
    ax_hist.set_xlabel("Pixel value")
    ax_hist.set_ylabel("Frequency")
    ax_hist.grid(alpha=0.3)
    ax_hist.set_title(
        f"min={np.nanmin(valid):.3g}  "
        f"max={np.nanmax(valid):.3g}  "
        f"mean={np.nanmean(valid):.3g}"
    )

    plt.tight_layout()
    plt.show()


def save_cog_png(src, out_path, vmin=None, vmax=None, max_dim=2048):
    """Render a COG to a standalone PNG quicklook (no axes, no borders).

    RGB composite when the COG has >= 3 bands, grayscale otherwise. Pixel
    scaling: explicit ``vmin``/``vmax`` when given; else 0-255 for uint8
    (already display-stretched, e.g. trueColor), else the 2nd-98th percentile
    of valid pixels (matches the old quickplot behaviour). Uses the headless
    Agg backend so it works on a DPS worker with no display.

    Args:
        src: local path, ``/vsis3/...`` URI, or ``s3://...`` URI.
        out_path: destination .png path.
        vmin, vmax: manual scaling bounds (float). None = auto (see above).
        max_dim: max output dimension for the decimated read (default 2048).

    Returns:
        out_path on success, or None if the band(s) were all-nodata.
    """
    import matplotlib
    matplotlib.use("Agg")  # headless: no display required
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    if isinstance(src, str) and src.startswith("s3://"):
        src = src.replace("s3://", "/vsis3/", 1)

    with rasterio.open(src) as ds:
        scale = max(ds.height, ds.width) / max_dim
        out_h = int(ds.height / scale) if scale > 1 else ds.height
        out_w = int(ds.width / scale) if scale > 1 else ds.width
        count = min(ds.count, 3)
        arr = ds.read(
            list(range(1, count + 1)),
            out_shape=(count, out_h, out_w),
            resampling=Resampling.average,
        ).astype(float)
        nodata = ds.nodata
        is_uint8 = ds.dtypes[0] == "uint8"

    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)

    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        print(f"  ⚠️ {src}: all-nodata, skipping png")
        return None

    if vmin is not None and vmax is not None:
        lo, hi = float(vmin), float(vmax)
    elif is_uint8:
        lo, hi = 0.0, 255.0
    else:
        lo, hi = np.nanpercentile(valid, (2, 98))
    if hi <= lo:
        hi = lo + 1.0

    scaled = np.clip((arr - lo) / (hi - lo), 0, 1)

    fig, ax = plt.subplots(figsize=(out_w / 100, out_h / 100), dpi=100)
    ax.axis("off")
    if arr.shape[0] >= 3:
        ax.imshow(np.nan_to_num(np.transpose(scaled[:3], (1, 2, 0))))
    else:
        ax.imshow(scaled[0], cmap="gray", vmin=0, vmax=1)
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    print(f"  saved png: {out_path}")
    return out_path


def preview_cogs(sources, sample_n=4, **kwargs):
    """Render :func:`plot_cog` for up to ``sample_n`` of the provided sources.

    Args:
        sources: iterable of paths / S3 URIs. Falsy values (None, empty
            strings) are filtered out automatically.
        sample_n: max files to plot (default 4).
        **kwargs: forwarded to :func:`plot_cog` (``title``, ``max_dim``).
    """
    sources = [s for s in sources if s]
    if not sources:
        print("No sources to preview.")
        return
    for src in sources[:sample_n]:
        plot_cog(src, **kwargs)
    if len(sources) > sample_n:
        print(
            f"({len(sources) - sample_n} more files not shown; "
            f"preview is limited to sample_n={sample_n})"
        )
