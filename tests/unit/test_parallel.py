"""
Tests for shared_utils.parallel.map_threaded.
"""

import time

import pytest

from shared_utils.parallel import map_threaded


def test_map_threaded_preserves_input_order():
    """Returned list aligns with input order even when later items finish first."""
    def slow_for_small_indices(i):
        # Earlier indices sleep longer so they finish AFTER later ones.
        time.sleep(0.05 * (5 - i))
        return i * 10

    items = list(range(5))
    results = map_threaded(slow_for_small_indices, items, max_workers=5)
    assert results == [0, 10, 20, 30, 40]


def test_map_threaded_captures_exceptions():
    """A worker that raises returns the exception object; siblings still succeed."""
    def maybe_fail(i):
        if i == 2:
            raise ValueError(f"bad item {i}")
        return i * 10

    results = map_threaded(maybe_fail, [0, 1, 2, 3, 4], max_workers=3)

    assert results[0] == 0
    assert results[1] == 10
    assert isinstance(results[2], ValueError)
    assert "bad item 2" in str(results[2])
    assert results[3] == 30
    assert results[4] == 40


def test_map_threaded_runs_concurrently():
    """Wall time for N sleeping items is closer to one sleep than to N sleeps."""
    sleep_time = 0.2
    n_items = 4

    def sleeper(_):
        time.sleep(sleep_time)
        return True

    start = time.monotonic()
    results = map_threaded(sleeper, list(range(n_items)), max_workers=n_items)
    elapsed = time.monotonic() - start

    assert all(results)
    # Sequential would be n_items * sleep_time. With max_workers=n_items, the
    # total should be close to one sleep. Use a generous 2x margin for CI flake.
    assert elapsed < 2 * sleep_time, (
        f"Expected ~{sleep_time}s with full concurrency, got {elapsed:.2f}s"
    )


def test_map_threaded_max_workers_one_is_sequential():
    """max_workers=1 still works (sequential thread pool); results correct."""
    call_log = []

    def record(i):
        call_log.append(i)
        return i

    results = map_threaded(record, [0, 1, 2, 3], max_workers=1)
    assert results == [0, 1, 2, 3]
    assert sorted(call_log) == [0, 1, 2, 3]


def test_map_threaded_empty_input():
    """Empty input returns empty list, doesn't crash."""
    results = map_threaded(lambda x: x, [], max_workers=4)
    assert results == []


def test_map_threaded_accepts_iterable_not_just_list():
    """Generator / range inputs are materialized internally."""
    results = map_threaded(lambda x: x * 2, range(3), max_workers=2)
    assert results == [0, 2, 4]
