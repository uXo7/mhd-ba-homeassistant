"""Merge realtime (imhd.sk) and timetable (GTFS) departures for one stop."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta

from .const import DEPARTED_GRACE_SECONDS, GTFS_HORIZON_HOURS, STALE_SECONDS
from .gtfs import Index, departures as gtfs_departures
from .imhd import Departure, ImhdPushClient

MATCH_TOLERANCE = timedelta(seconds=90)


class PlatformMap:
    """Learns imhd platform id -> GTFS stop by matching line + scheduled time.

    imhd.sk numbers platforms internally (e.g. 837); GTFS has the stop ids and,
    when present, the platform letters. Every realtime departure that matches a
    timetable departure (same line, scheduled time within tolerance) is a vote.
    """

    def __init__(self) -> None:
        self._votes: dict[str, Counter[str]] = defaultdict(Counter)

    def learn(self, realtime: list[Departure], timetable: list[Departure]) -> None:
        by_line: dict[str, list[Departure]] = defaultdict(list)
        for t in timetable:
            by_line[t.line].append(t)
        for r in realtime:
            if r.scheduled is None:
                continue
            cands = [
                t for t in by_line.get(r.line, ())
                if abs(t.time - r.scheduled) <= MATCH_TOLERANCE
            ]
            if len({t.platform for t in cands}) == 1:  # unambiguous only
                self._votes[r.platform][cands[0].platform] += 1

    def gtfs_stop(self, imhd_platform: str) -> str | None:
        votes = self._votes.get(imhd_platform)
        if not votes:
            return None
        stop, n = votes.most_common(1)[0]
        return stop if n >= 2 else None

    def as_dict(self) -> dict[str, str]:
        return {p: s for p in self._votes if (s := self.gtfs_stop(p))}


class StopBoard:
    """Departures of one stop: realtime when fresh, timetable otherwise."""

    def __init__(self, name: str, client: ImhdPushClient | None) -> None:
        self.name = name  # GTFS stop name (group of platforms)
        self.client = client
        self.index: Index | None = None
        self.platforms = PlatformMap()
        # GTFS headsign -> imhd.sk destination text, per line (learned from matches),
        # so timetable rows read the same as realtime ones.
        self._dest_alias: dict[tuple[str, str], str] = {}

    @property
    def realtime_ok(self) -> bool:
        return self.client is not None and self.client.fresh(
            timedelta(seconds=STALE_SECONDS)
        )

    def _timetable(self, start: datetime, end: datetime) -> list[Departure]:
        if self.index is None:
            return []
        return gtfs_departures(self.index, self.name, start, end)

    def _label(self, imhd_platform: str) -> str | None:
        if self.index is None:
            return None
        stop = self.platforms.gtfs_stop(imhd_platform)
        info = self.index.stops.get(stop) if stop else None
        return info.platform if info else None

    def _vehicle_extra(self, dep: Departure) -> Departure:
        info = self.client.vehicles.get(dep.vehicle) if dep.vehicle else None
        if info:
            dep = replace(dep, extra={"vehicle_" + k: v for k, v in info.items()})
        return dep

    def departures(self, now: datetime, limit: int) -> list[Departure]:
        """Timetable as the backbone, realtime overlaid on matching trips.

        The imhd.sk board lists only the next few trips per line, so the
        timetable fills the rest. A realtime departure replaces the timetable
        departure of the same line with the same scheduled time; realtime
        departures without a timetable match (extra trips) are kept as they are.
        """
        start = now - timedelta(seconds=DEPARTED_GRACE_SECONDS)
        end = now + timedelta(hours=GTFS_HORIZON_HOURS)
        # Wider window for matching: a delayed vehicle may be scheduled earlier.
        timetable = self._timetable(start - timedelta(hours=1), end)
        if not self.realtime_ok:
            return [d for d in timetable if d.time >= start][:limit]

        live = self.client.board.departures
        self.platforms.learn(live, timetable)
        by_key: dict[tuple[str, datetime], Departure] = {}
        for t in timetable:
            by_key.setdefault((t.line, t.scheduled.replace(second=0)), t)
        merged: list[Departure] = []
        replaced: set[int] = set()
        for r in live:
            match = None
            if r.scheduled is not None:
                base = r.scheduled.replace(second=0)
                for off in (0, -60, 60):
                    match = by_key.get((r.line, base + timedelta(seconds=off)))
                    if match is not None and id(match) not in replaced:
                        break
                    match = None
            label = match.platform_label if match else self._label(r.platform)
            if match is not None:
                replaced.add(id(match))
                if match.destination and r.destination:
                    self._dest_alias[(r.line, match.destination)] = r.destination
            merged.append(self._vehicle_extra(r.with_label(label)))
        for t in timetable:
            if id(t) in replaced:
                continue
            alias = self._dest_alias.get((t.line, t.destination))
            merged.append(replace(t, destination=alias) if alias else t)
        merged = [d for d in merged if d.time >= start]
        merged.sort(key=lambda d: d.time)
        return merged[:limit]
