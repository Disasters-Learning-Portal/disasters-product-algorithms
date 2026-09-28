"""Resolve a ``--date`` argument to a scene in a CSDA vendor bucket.

Capella, Umbra, Satellogic and SkySat all select a scene by timestamp. Each one
used to ``strptime`` the argument and take ``min(dates, key=...)`` over the
bucket, which failed in two unhelpful ways:

* a malformed ``--date`` surfaced as a raw ``strptime`` error (``unconverted
  data remains: 1906``) that did not say what format was expected;
* a well-formed ``--date`` for a scene the vendor had not delivered yet
  silently selected whichever scene was closest, however far away -- the
  buckets hold every activation's scenes, so that could be another event's.

:func:`parse_date_arg` and :func:`select_scene_date` replace both with errors
that name the fix, and the CLIs print them instead of a traceback.
"""

from datetime import datetime, timedelta
from typing import Iterable, Optional

# How far --date may be from a scene's acquisition timestamp and still select
# it. Wide enough to absorb a date copied from a catalog or rounded to the
# minute; narrow enough that it can't reach a different pass.
DATE_TOLERANCE = timedelta(minutes=5)


def parse_date_arg(value: str, fmt: str, example: str, sensor: str) -> datetime:
    """Parse ``value`` with ``fmt``, requiring the exact zero-padded form.

    ``strptime`` accepts single-digit fields (a 13-digit ``%Y%m%d%H%M%S`` parses),
    so the parse is round-tripped through ``strftime`` to reject those too.
    """
    try:
        parsed = datetime.strptime(value, fmt)
        if parsed.strftime(fmt) != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ValueError(
            f"--date {value!r} is not a valid {sensor} date: expected "
            f"{fmt_label(fmt)} (e.g. {example})."
        ) from None


def fmt_label(fmt: str) -> str:
    """Human-readable form of a strptime format, e.g. ``YYYY-MM-DD HH:MM:SS``."""
    for code, label in (("%Y", "YYYY"), ("%m", "MM"), ("%d", "DD"),
                        ("%H", "HH"), ("%M", "MM"), ("%S", "SS")):
        fmt = fmt.replace(code, label)
    return fmt


def select_scene_date(
    requested: datetime,
    available: Iterable[datetime],
    what: str,
    where: str,
    fmt: str,
    hint: Optional[str] = None,
    tolerance: timedelta = DATE_TOLERANCE,
) -> datetime:
    """Return the available date closest to ``requested``, within ``tolerance``.

    Raises ``FileNotFoundError`` when nothing is available, or when the closest
    scene is further than ``tolerance`` away (listing the nearest dates, printed
    in ``fmt`` so they can be passed straight back as ``--date``). A non-exact
    match is printed so the substitution is visible in the log.

    ``what`` names the scene kind (``"Capella scene"``), ``where`` the searched
    location, and ``hint`` an optional pointer to a discovery tool.
    """
    dates = sorted(set(available))
    extra = f" {hint}" if hint else ""

    if not dates:
        raise FileNotFoundError(
            f"No {what}s found under {where} -- nothing has been delivered "
            f"there, or it is not readable with the current credentials.{extra}"
        )

    closest = min(dates, key=lambda d: abs(d - requested))

    if abs(closest - requested) > tolerance:
        nearest = sorted(sorted(dates, key=lambda d: abs(d - requested))[:5])
        listing = "\n".join(f"  {d.strftime(fmt)}" for d in nearest)
        raise FileNotFoundError(
            f"No {what} within {tolerance} of --date {requested.strftime(fmt)} "
            f"under {where}. The vendor may not have delivered it yet -- check "
            f"again later.{extra} Nearest available dates:\n{listing}"
        )

    if closest != requested:
        print(
            f"--date {requested.strftime(fmt)} has no exact {what}; using the "
            f"closest, {closest.strftime(fmt)} ({abs(closest - requested)} away)"
        )
    return closest
