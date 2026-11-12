"""Clock, time, and duration utilities per plan §5.4.

Times are stored as UTC ISO-8601 with milliseconds and a 'Z' suffix (e.g. 2026-11-07T08:30:00.123Z).
In-process timers use time.monotonic().
Durations are given as 30s, 15m, 2h, 7d, or plain integer seconds.
"""

import datetime
import re
import time
from typing import Union

DURATION_PATTERN = re.compile(r"^(\d+)([smhd]?)$")


def now_utc() -> str:
    """Return the current UTC time as ISO-8601 string with millisecond precision and 'Z' suffix."""
    dt = datetime.datetime.now(datetime.timezone.utc)
    return format_utc(dt)


def now_iso_utc(ts: Union[float, int, datetime.datetime, None] = None) -> str:
    """Return UTC time formatted as ISO-8601 with milliseconds and 'Z' suffix."""
    if ts is None:
        return now_utc()
    if isinstance(ts, (int, float)):
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        return format_utc(dt)
    if isinstance(ts, datetime.datetime):
        return format_utc(ts)
    raise ValueError(f"Unsupported timestamp type: {type(ts)}")


def now_monotonic() -> float:
    """Return the monotonic clock value in seconds for in-process timers."""
    return time.monotonic()


def format_utc(dt: datetime.datetime) -> str:
    """Format a datetime as UTC ISO-8601 with 3-digit millisecond precision and 'Z' suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)

    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def parse_utc(s: str) -> datetime.datetime:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware datetime.

    Accepts formats ending in 'Z' or '+00:00', with or without microseconds/milliseconds.
    """
    if not isinstance(s, str):
        raise ValueError(f"Expected timestamp string, got {type(s).__name__}")

    clean = s.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"

    try:
        dt = datetime.datetime.fromisoformat(clean)
    except ValueError as e:
        raise ValueError(f"Invalid ISO-8601 UTC timestamp '{s}': {e}") from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)

    return dt


def parse_duration(val: Union[str, int]) -> int:
    """Parse a duration string or integer into integer seconds.

    Accepted formats:
        - Plain integers or integer strings: 60, "60" -> 60 seconds
        - Suffixes:
            - 's': seconds (e.g. "30s" -> 30)
            - 'm': minutes (e.g. "15m" -> 900)
            - 'h': hours (e.g. "2h" -> 7200)
            - 'd': days (e.g. "7d" -> 604800)

    Returns:
        Integer number of seconds >= 0.

    Raises:
        ValueError if input is invalid or negative.
    """
    if isinstance(val, int):
        if val < 0:
            raise ValueError(f"Duration cannot be negative, got {val}")
        return val

    if not isinstance(val, str):
        raise ValueError(f"Expected duration string or integer, got {type(val).__name__}")

    s = val.strip()
    m = DURATION_PATTERN.match(s)
    if not m:
        raise ValueError(f"Invalid duration format '{val}'; expected e.g. '30s', '15m', '2h', '7d', or '60'")

    num_str, unit = m.groups()
    num = int(num_str)

    if unit == "" or unit == "s":
        return num
    elif unit == "m":
        return num * 60
    elif unit == "h":
        return num * 3600
    elif unit == "d":
        return num * 86400
    else:
        raise ValueError(f"Unknown duration unit '{unit}' in '{val}'")


def format_duration(seconds: int) -> str:
    """Format an integer number of seconds into a human-readable duration string."""
    if seconds < 0:
        raise ValueError(f"Duration cannot be negative, got {seconds}")

    if seconds == 0:
        return "0s"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
