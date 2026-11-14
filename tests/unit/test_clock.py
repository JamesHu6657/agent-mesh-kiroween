"""Unit tests for UTC clock and duration parsing per plan §5.4."""

import datetime
import unittest
from mesh.core.clock import (
    format_duration,
    format_utc,
    now_monotonic,
    now_utc,
    parse_duration,
    parse_utc,
)


class TestClock(unittest.TestCase):
    """Test UTC timestamp generation and duration parsing."""

    def test_now_utc_format(self):
        """Verify now_utc produces valid UTC ISO-8601 with milliseconds and 'Z' suffix."""
        ts = now_utc()
        self.assertTrue(ts.endswith("Z"))
        self.assertIn(".", ts)

        # Milliseconds should be 3 digits
        parts = ts[:-1].split(".")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[1]), 3)

        dt = parse_utc(ts)
        self.assertEqual(dt.tzinfo, datetime.timezone.utc)

    def test_parse_and_format_utc(self):
        """Verify round-trip formatting and parsing of UTC datetimes."""
        dt = datetime.datetime(2026, 11, 7, 8, 30, 45, 123456, tzinfo=datetime.timezone.utc)
        formatted = format_utc(dt)
        self.assertEqual(formatted, "2026-11-07T08:30:45.123Z")

        parsed = parse_utc(formatted)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 11)
        self.assertEqual(parsed.day, 7)
        self.assertEqual(parsed.hour, 8)
        self.assertEqual(parsed.minute, 30)
        self.assertEqual(parsed.second, 45)
        self.assertEqual(parsed.microsecond, 123000)

        # Test +00:00 alternative format
        parsed2 = parse_utc("2026-11-07T08:30:45.123+00:00")
        self.assertEqual(parsed2, parsed)

    def test_now_monotonic(self):
        """Verify now_monotonic returns monotonic float seconds."""
        t1 = now_monotonic()
        t2 = now_monotonic()
        self.assertIsInstance(t1, float)
        self.assertGreaterEqual(t2, t1)

    def test_parse_duration(self):
        """Verify durations per plan §5.4: 30s, 15m, 2h, 7d, plain int."""
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("15m"), 15 * 60)
        self.assertEqual(parse_duration("2h"), 2 * 3600)
        self.assertEqual(parse_duration("7d"), 7 * 86400)
        self.assertEqual(parse_duration("60"), 60)
        self.assertEqual(parse_duration(120), 120)
        self.assertEqual(parse_duration(0), 0)
        self.assertEqual(parse_duration("0s"), 0)

        # Invalid formats
        invalid_cases = ["-5s", -1, "abc", "30x", "1.5s", "", "   "]
        for case in invalid_cases:
            with self.assertRaises(ValueError, msg=f"Should reject {case!r}"):
                parse_duration(case)

    def test_format_duration(self):
        """Verify formatting integer seconds to duration strings."""
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(900), "15m")
        self.assertEqual(format_duration(7200), "2h")
        self.assertEqual(format_duration(604800), "7d")


if __name__ == "__main__":
    unittest.main()
