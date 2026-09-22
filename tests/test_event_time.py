from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from lastz_bot.event_time import (
    parse_apocalypse_time,
    utc_now_naive,
    utc_to_apocalypse_time,
)


class EventTimeTests(unittest.TestCase):
    def test_at_to_utc_and_back(self) -> None:
        cases = [
            ("2026-09-25 17:00", datetime(2026, 9, 25, 19)),
            ("2026-09-25 23:30", datetime(2026, 9, 26, 1, 30)),
            ("2026-12-31 23:59", datetime(2027, 1, 1, 1, 59)),
            ("2028-02-29 23:00", datetime(2028, 3, 1, 1)),
            ("2026-01-15 00:00", datetime(2026, 1, 15, 2)),
            ("2026-07-15 00:00", datetime(2026, 7, 15, 2)),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                stored = parse_apocalypse_time(value)
                self.assertEqual(stored, expected)
                self.assertIsNone(stored.tzinfo)
                displayed = utc_to_apocalypse_time(stored)
                self.assertEqual(displayed.strftime("%Y-%m-%d %H:%M"), value)
                self.assertEqual(displayed.utcoffset(), timedelta(hours=-2))

    def test_existing_whitespace_and_unpadded_input_semantics(self) -> None:
        self.assertEqual(
            parse_apocalypse_time("  2026-9-5 7:03 \n"),
            datetime(2026, 9, 5, 9, 3),
        )

    def test_invalid_input_raises_value_error(self) -> None:
        for value in (
            "", "not a date", "2026-02-29 17:00", "2026-09-25 24:00",
            "2026-09-25 17:60", "2026-09-25T17:00", "2026-09-25 17:00 AT",
            "2026-09-25 17:00:00",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_apocalypse_time(value)

    def test_display_interprets_naive_storage_as_utc(self) -> None:
        displayed = utc_to_apocalypse_time(datetime(2027, 1, 1, 0, 30))
        self.assertEqual(displayed.isoformat(), "2026-12-31T22:30:00-02:00")

    def test_current_time_uses_utc_and_removes_timezone(self) -> None:
        instant = datetime(2026, 9, 25, 19, 0, 1, 234567, tzinfo=timezone.utc)
        with patch("lastz_bot.event_time.datetime") as clock:
            clock.now.return_value = instant
            result = utc_now_naive()
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(result, datetime(2026, 9, 25, 19, 0, 1, 234567))
        self.assertIsNone(result.tzinfo)
