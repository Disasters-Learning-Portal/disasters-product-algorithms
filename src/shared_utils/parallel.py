"""
Thread-pool parallelism helper for I/O-bound batch operations.

The codebase already uses thread-level parallelism inside GDAL/rasterio (via
``NUM_THREADS=ALL_CPUS``) and inside boto3 (which releases the GIL on network
calls). This module exposes one small helper for fanning a Python-level loop
out across a thread pool — useful in notebooks and scripts that iterate over
N independent S3 keys / files / products.

Threads (not processes) are intentional: the work targeted here is I/O-bound
(S3 upload/download) plus GIL-releasing native calls (GDAL, numpy). A
ProcessPool would oversubscribe CPU cores because each worker's
``convert_to_cog`` already spawns ``NUM_THREADS=ALL_CPUS`` internally.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, List, Optional


def map_threaded(
    func: Callable[[Any], Any],
    items: Iterable[Any],
    max_workers: int = 4,
    desc: Optional[str] = None,
) -> List[Any]:
    """
    Apply ``func`` to each item using a thread pool, preserving input order.

    Per-item exceptions are captured (not re-raised) and returned in place of
    the result for that item, so one bad input does not kill the batch.

    Args:
        func: Single-argument callable invoked once per item.
        items: Iterable of items to process. Materialized into a list so the
            return value can be index-aligned.
        max_workers: Pool size. Defaults to 4 — a safe value for batches of
            4-8 items that mix S3 I/O with GDAL native compute.
        desc: Optional tqdm progress-bar description. When set, requires
            ``tqdm`` to be importable; falls back silently if not.

    Returns:
        List of length ``len(items)``. Each element is either ``func(item)``
        or the ``Exception`` instance raised for that item.
    """
    items = list(items)
    results: List[Any] = [None] * len(items)

    if not items:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {ex.submit(func, item): i for i, item in enumerate(items)}
        iterator = as_completed(future_to_idx)

        if desc:
            try:
                from tqdm.auto import tqdm
                iterator = tqdm(iterator, total=len(items), desc=desc)
            except ImportError:
                pass

        for fut in iterator:
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = e

    return results
