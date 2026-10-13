"""The cloud-mask path must follow the file through its event rename.

`rename_with_event` MOVES the cloud mask (`..._20260919_161940.tif` ->
`..._2026-09-19T16:19:40Z.tif`) and returns the new path. The dispatch used to
discard that return value, so `cloudMask` kept the pre-rename path and every
masked product downstream failed `apply_cloud_mask` with `RasterioIOError: No
such file or directory`.

The dispatch runs under `if __name__ == "__main__":` and can't be imported, so
this pins it statically: inside the `try:` that produces the cloud mask, every
`rename_with_event(...)` call must be assigned back to `cloudMask`.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DISPATCHES = [
    REPO / "src" / "landsat" / "process_landsat89.py",
    REPO / "src" / "sentinel2" / "process_sentinel2.py",
]


def _is_call_to(node, name):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name)


def _cloud_mask_try_blocks(tree):
    """`try:` blocks whose body assigns `cloudMask = gen_cloudMask(...)`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "cloudMask" for t in stmt.targets)
                    and _is_call_to(stmt.value, "gen_cloudMask")):
                yield node
                break


@pytest.mark.parametrize("path", DISPATCHES, ids=lambda p: p.name)
def test_cloud_mask_tracks_renamed_path(path):
    tree = ast.parse(path.read_text())
    blocks = list(_cloud_mask_try_blocks(tree))
    assert blocks, f"no `cloudMask = gen_cloudMask(...)` try-block found in {path.name}"

    for block in blocks:
        assigned = set()
        for node in ast.walk(block):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "cloudMask" for t in node.targets)
                    and _is_call_to(node.value, "rename_with_event")):
                assigned.add(id(node.value))
        calls = [n for n in ast.walk(block) if _is_call_to(n, "rename_with_event")]
        assert calls, f"no rename_with_event call in the cloud-mask block of {path.name}"
        stray = [c.lineno for c in calls if id(c) not in assigned]
        assert not stray, (
            f"{path.name}: rename_with_event at line(s) {stray} discards the renamed "
            f"path, so `cloudMask` points at a file that no longer exists"
        )
