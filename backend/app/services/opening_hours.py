"""Conservative subset of OSM hours. Unsupported rules are never treated as open."""

from dataclasses import dataclass
import re

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


class UnsupportedHours(ValueError):
    pass


def merge_windows(windows):
    result = []
    for start, end in sorted(set(windows)):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


@dataclass(frozen=True)
class OpeningRules:
    weekly: tuple

    @property
    def varies_by_weekday(self):
        return len(set(self.weekly)) > 1

    def windows(self, date=None):
        return list(self.weekly[date.weekday()]) if date is not None else merge_windows(
            [window for day in self.weekly for window in day])


def parse_opening_hours(value):
    value = re.sub(r"\s*,\s*", ",", value.strip())
    if value == "24/7":
        return OpeningRules(tuple(((0, 1440),) for _ in DAYS))
    weekly = [[] for _ in DAYS]
    day_pattern = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:[-,](?:Mo|Tu|We|Th|Fr|Sa|Su))*"
    for clause in value.split(";"):
        match = re.fullmatch(rf"(?:(?P<days>{day_pattern})\s+)?(?P<times>.+)", clause.strip())
        if not match:
            raise UnsupportedHours(value)
        selected = list(range(7))
        if match["days"]:
            selected = []
            for token in match["days"].split(","):
                if "-" in token:
                    first, last = [DAYS.index(day) for day in token.split("-")]
                    selected.extend((first + offset) % 7 for offset in range((last-first) % 7 + 1))
                else:
                    selected.append(DAYS.index(token))
        intervals = []
        if match["times"] not in {"off", "closed"}:
            for interval in match["times"].split(","):
                span = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", interval)
                if not span:
                    raise UnsupportedHours(value)
                h1, m1, h2, m2 = map(int, span.groups())
                if h1 > 23 or m1 > 59 or h2 > 24 or m2 > 59 or (h2 == 24 and m2 != 0):
                    raise UnsupportedHours(value)
                start, end = h1 * 60 + m1, h2 * 60 + m2
                if end <= start:  # overnight/appointment/holiday expressions need a complete OSM engine.
                    raise UnsupportedHours(value)
                intervals.append((start, end))
        for day in selected:
            weekly[day] = merge_windows(intervals)
    return OpeningRules(tuple(tuple(day) for day in weekly))
