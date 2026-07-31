import asyncio
import logging
import threading
from collections.abc import Coroutine
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import obstore as obs
from obstore.store import S3Store, from_url
from pystac import Item
from pystac_client import Client
from tqdm.asyncio import tqdm

from blackmarble.acquire.wrs_utils import get_wrs_tiles_for_bbox_tuple
from blackmarble.typing import BBox


if TYPE_CHECKING:
    from obstore.store import ObjectStore
    from pystac import Item

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# bbox = (-93.572078, 41.804069, -88.051814, 45.020225)
# target_date = datetime(2020, 5, 1)
# time_window_days = 7
# tolerance_days = 12
# allow_incomplete = True
# cloud_cover_max = 95

STAC_URL = "https://landsatlook.usgs.gov/stac-server"
COLLECTION = "landsat-c2l2-sr"
LANDSAT_PLATFORMS = ["LANDSAT_8", "LANDSAT_9"]
DOWNLOAD_BANDS = ["blue", "green", "red", "nir08", "qa_pixel"]
S3_BUCKET = "usgs-landsat"
AWS_REGION = "us-west-2"
MAX_CONCURRENT_DOWNLOADS = 8
DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds

T = TypeVar("T")


def run_async[T](coro: Coroutine[object, object, T]) -> T:
    """Run a coroutine from synchronous code.

    Works in three scenarios:
    1. No event loop exists (scripts)          → ``asyncio.run``
    2. Loop exists but is idle                 → ``loop.run_until_complete``
    3. Loop exists **and** is running (Jupyter) → execute in a daemon thread
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    if not loop.is_running():
        return loop.run_until_complete(coro)

    # Running inside an active loop (e.g. Jupyter) — offload to a thread.
    result: T
    error: BaseException | None = None

    def _runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if error is not None:
        raise error
    return result  # type: ignore[possibly-undefined]  # noqa: F821


# ── Helpers ───────────────────────────────────────────────────────────────────
def scene_path_row(item: Item) -> tuple[int, int]:
    return (
        int(item.properties["landsat:wrs_path"]),
        int(item.properties["landsat:wrs_row"]),
    )


def scene_date(item: Item) -> date:
    return datetime.fromisoformat(item.properties["datetime"]).date()


def scene_cloud(item: Item) -> float:
    return item.properties["eo:cloud_cover"]


def best_per_tile(items: list[Item]) -> list[Item]:
    """Return one scene per WRS tile, choosing the lowest cloud cover."""
    by_tile: dict[tuple[int, int], Item] = {}
    for item in items:
        key = scene_path_row(item)
        if key not in by_tile or scene_cloud(item) < scene_cloud(by_tile[key]):
            by_tile[key] = item
    return list(by_tile.values())


def group_scenes_around(
    base: date,
    scenes_by_date: dict[date, list[Item]],
    tolerance: int,
) -> tuple[list[Item], list[date]]:
    """Collect all scenes within ±tolerance days of base."""
    scenes: list[Item] = []
    dates: list[date] = []
    for d, items in scenes_by_date.items():
        if abs((d - base).days) <= tolerance:
            scenes.extend(items)
            dates.append(d)
    return scenes, dates


def search_landsat_grouped(
    bbox: tuple[float, float, float, float],
    target_date: datetime,
    time_window_days: int,
    tolerance_days: int,
    cloud_cover_max: float,
    allow_incomplete: bool,
) -> list[Item]:
    """Search Landsat scenes grouped by acquisition date for complete WRS tile coverage."""

    # ── Determine required tiles ────────────────────────────────────────────────
    required_tiles = {(t["path"], t["row"]) for t in get_wrs_tiles_for_bbox_tuple(bbox)}

    # ── STAC search ───────────────────────────────────────────────────────────────
    start = target_date - timedelta(days=time_window_days)
    end = target_date + timedelta(days=time_window_days)

    client = Client.open(STAC_URL)
    search = client.search(
        collections=[COLLECTION],
        datetime=(start, end),
        bbox=bbox,
        query={
            "platform": {"in": LANDSAT_PLATFORMS},
        },
    )
    # Apply cloud filter after retrieval so thresholds are consistent with S3 behaviour
    all_items = [item for item in search.item_collection() if scene_cloud(item) <= cloud_cover_max]

    # ── Group by acquisition date ─────────────────────────────────────────────────
    scenes_by_date: dict[date, list[Item]] = {}
    for item in all_items:
        scenes_by_date.setdefault(scene_date(item), []).append(item)

    # ── Find temporally-consistent complete groups ────────────────────────────────
    # A group is "complete" when its scenes cover every required WRS tile.
    # Each date can only serve as a base_date once; scenes from nearby dates
    # are freely shared across groups (matching the S3 search behaviour).
    complete_groups: list[tuple[date, list[Item], float, list[date]]] = []
    used_as_base: set[date] = set()

    for base in sorted(scenes_by_date):
        if base in used_as_base:
            continue
        used_as_base.add(base)

        group_scenes, group_dates = group_scenes_around(base, scenes_by_date, tolerance_days)
        covered_tiles = {scene_path_row(s) for s in group_scenes}

        if covered_tiles >= required_tiles:
            avg_cloud = sum(scene_cloud(s) for s in group_scenes) / len(group_scenes)
            complete_groups.append((base, group_scenes, avg_cloud, group_dates))

    # ── Select result ─────────────────────────────────────────────────────────────
    if complete_groups:
        # Best temporally-consistent group: lowest average cloud cover
        _, best_scenes, _, _ = min(complete_groups, key=lambda g: g[2])
        result = best_per_tile(best_scenes)
    else:
        result = best_per_tile(all_items) if allow_incomplete else []

    return result


def _make_s3_store() -> "ObjectStore":
    """Create an S3 store with generous timeouts for large file downloads."""
    return S3Store(
        bucket=S3_BUCKET,
        region=AWS_REGION,
        request_payer=True,
        client_options={
            "connect_timeout": "10s",
            "timeout": "300s",  # 5 min per request for large TIFs
        },
    )


def get_download_hrefs(item: Item) -> list[str]:
    hrefs: list[str] = []
    for name, asset in item.assets.items():
        if name in DOWNLOAD_BANDS:
            href = asset.extra_fields["alternate"]["s3"]["href"]
            hrefs.append(href)
    return hrefs


async def stream_download(
    store: "ObjectStore",
    dest: "ObjectStore",
    href: str,
    semaphore: asyncio.Semaphore,
    dest_path: str,
) -> str:
    remote = href.replace(f"s3://{S3_BUCKET}/", "")
    # Use the last two path components (scene_id/filename) to avoid collisions
    local_path = "/".join(remote.split("/")[-2:])
    # Skip if already downloaded and size matches remote
    local_file = Path(dest_path) / local_path
    if local_file.exists() and local_file.stat().st_size > 0:
        async with semaphore:
            remote_meta = await obs.head_async(store, remote)
        if local_file.stat().st_size == remote_meta["size"]:
            logger.debug("Skipped existing file: %s", local_file)
            return local_path

    last_error: BaseException | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            logger.debug("Downloading %s (attempt %d/%d)", remote, attempt, DOWNLOAD_RETRIES)
            async with semaphore:
                resp = await obs.get_async(store, remote)
                chunk_size = 5 * 1024 * 1024  # 5MB
                stream = resp.stream(min_chunk_size=chunk_size)
                await obs.put_async(
                    dest, local_path, stream, chunk_size=chunk_size, max_concurrency=12
                )
            return local_path
        except Exception as exc:
            last_error = exc
            # Remove partial file if any
            if local_file.exists():
                local_file.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                wait = RETRY_BACKOFF_BASE**attempt
                logger.warning(
                    "Download failed for %s: %s  — retrying in %.1fs",
                    remote,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Download failed for %s after %d attempts: %s",
                    remote,
                    DOWNLOAD_RETRIES,
                    exc,
                )

    raise last_error  # type: ignore[misc]


async def download_hrefs(
    hrefs: list[str],
    dest_path: str,
    store: "ObjectStore | None" = None,
    semaphore: asyncio.Semaphore | None = None,
) -> list[str]:
    """Download a list of hrefs to the specified path.

    If store or semaphore are not provided, they will be created for this download.
    To share resources across multiple downloads, pass the same store and semaphore.
    """
    if store is None:
        store = _make_s3_store()
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    dest = from_url(f"file://{Path(dest_path).absolute()}", mkdir=True)
    tasks = [
        asyncio.create_task(stream_download(store, dest, href, semaphore, dest_path))
        for href in hrefs
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    first_error = next((task.exception() for task in done if task.exception() is not None), None)
    if first_error is not None:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise first_error

    if pending:
        await asyncio.wait(pending)
    relative_paths = [task.result() for task in tasks]
    return [f"{dest_path}/{p}" for p in relative_paths]


async def download_item(
    item: Item,
    dest_path: str,
    store: "ObjectStore | None" = None,
    semaphore: asyncio.Semaphore | None = None,
) -> list[str]:
    """Download all bands for a single STAC item to the specified path.

    If store or semaphore are not provided, they will be created for this download.
    To share resources across multiple items, pass the same store and semaphore.
    """
    hrefs = get_download_hrefs(item)
    return await download_hrefs(hrefs, dest_path, store, semaphore)


async def download_items(
    items: list[Item], local_path: str, max_concurrency: int = MAX_CONCURRENT_DOWNLOADS
) -> list[str]:
    store = _make_s3_store()
    semaphore = asyncio.Semaphore(max_concurrency)

    tasks = [
        asyncio.create_task(download_item(item, local_path, store, semaphore)) for item in items
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    first_error = next((task.exception() for task in done if task.exception() is not None), None)
    if first_error is not None:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise first_error

    if pending:
        await asyncio.wait(pending)

    results: list[list[str]] = []
    for task in tqdm(tasks, total=len(tasks)):
        results.append(task.result())
    return [path for paths in results for path in paths]


async def download_items_by_date(
    items_by_date: dict[datetime, list[Item]],
    local_path: str,
    max_concurrency: int = MAX_CONCURRENT_DOWNLOADS,
) -> dict[datetime, list[str]]:
    """Download items across all dates with a single progress bar.

    Concurrency is bounded by the semaphore inside stream_download,
    so we can create all tasks upfront and collect as they finish.
    """
    store = _make_s3_store()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _tagged(
        dt: datetime,
        item: Item,
    ) -> tuple[datetime, list[str]]:
        paths = await download_item(
            item,
            local_path,
            store,
            semaphore,
        )
        return dt, paths

    tasks = [
        asyncio.create_task(_tagged(dt, item))
        for dt, items in items_by_date.items()
        for item in items
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    first_error = next((task.exception() for task in done if task.exception() is not None), None)
    if first_error is not None:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise first_error

    if pending:
        await asyncio.wait(pending)

    results: dict[datetime, list[str]] = {d: [] for d in items_by_date}
    for task in tqdm(tasks, total=len(tasks), desc="Downloading scenes", unit="scene(s)"):
        dt, paths = task.result()
        results[dt].extend(paths)

    return results


def download_landsat(
    date: datetime,
    bbox: BBox,
    data_dir: str,
    time_window_days: int = 7,
    tolerance_days: int = 12,
    cloud_cover_max: float = 95,
) -> dict[datetime, list[str]]:
    """Synchronous wrapper to download Landsat data for a given date and bbox."""
    landsat_files: dict[datetime, list[str]] = {}
    items_by_date: dict[datetime, list[Item]] = {}
    for i in range(12):
        date_offset = date - timedelta(days=30 * i)
        items = search_landsat_grouped(
            bbox=bbox,
            target_date=date_offset,
            time_window_days=time_window_days,
            tolerance_days=tolerance_days,
            cloud_cover_max=cloud_cover_max,
            allow_incomplete=True,
        )
        landsat_files[date_offset] = []
        if items:
            items_by_date[date_offset] = items

    if not items_by_date:
        return landsat_files

    downloaded = run_async(
        download_items_by_date(
            items_by_date,
            local_path=data_dir,
            max_concurrency=MAX_CONCURRENT_DOWNLOADS,
        )
    )
    landsat_files.update(downloaded)
    return landsat_files
