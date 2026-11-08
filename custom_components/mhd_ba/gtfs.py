"""DPB static timetable (GTFS), kept locally as the fallback source.

The feed is downloaded at most once a day and cached on disk; when the
download fails the cached copy is used, so the timetable keeps working without
internet. Only the configured stops are indexed (stop_times are streamed, not
loaded whole).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import io
import json
import logging
from pathlib import Path
import unicodedata
import zipfile
from zoneinfo import ZoneInfo

import aiohttp

from .const import SOURCE_TIMETABLE
from .imhd import Departure

_LOGGER = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Bratislava")


class GtfsError(Exception):
    """Feed missing or unreadable."""


def normalize(name: str) -> str:
    """Case- and diacritics-insensitive key for stop names."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(",", " ").split())


def _rows(zf: zipfile.ZipFile, name: str):
    try:
        raw = zf.open(name)
    except KeyError:
        return
    with raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


def _secs(value: str) -> int | None:
    """GTFS HH:MM:SS (hours may exceed 24) -> seconds after service-day noon-12h."""
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except (AttributeError, ValueError):
        return None


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except (AttributeError, ValueError):
        return None


@dataclass(slots=True)
class StopInfo:
    stop_id: str
    name: str
    platform: str | None  # platform_code (letter) when the feed has it


@dataclass(slots=True)
class StopTime:
    stop_id: str
    trip_id: str
    secs: int


@dataclass(slots=True)
class Trip:
    line: str
    service_id: str
    headsign: str


@dataclass
class Index:
    """Parsed subset of the feed for a set of stop names."""

    stops: dict[str, StopInfo] = field(default_factory=dict)  # stop_id -> info
    groups: dict[str, list[str]] = field(default_factory=dict)  # name key -> stop_ids
    stop_times: dict[str, list[StopTime]] = field(default_factory=dict)  # name key -> rows
    trips: dict[str, Trip] = field(default_factory=dict)
    weekly: dict[str, tuple[date, date, tuple[bool, ...]]] = field(default_factory=dict)
    exceptions: dict[str, dict[date, bool]] = field(default_factory=dict)
    valid_until: date | None = None

    def active(self, service_id: str, day: date) -> bool:
        exc = self.exceptions.get(service_id, {}).get(day)
        if exc is not None:
            return exc
        rule = self.weekly.get(service_id)
        if rule is None:
            return False
        start, end, days = rule
        return start <= day <= end and days[day.weekday()]


def list_stop_names(path: Path) -> list[str]:
    """Distinct stop names in the feed (blocking)."""
    with zipfile.ZipFile(path) as zf:
        names = {r.get("stop_name", "").strip() for r in _rows(zf, "stops.txt")}
    return sorted((n for n in names if n), key=normalize)


def build_index(path: Path, wanted: set[str]) -> Index:
    """Index the given stop names (normalized) from the feed (blocking)."""
    idx = Index()
    with zipfile.ZipFile(path) as zf:
        stops = list(_rows(zf, "stops.txt"))
        stations: dict[str, str] = {}  # parent station id -> name key
        for r in stops:
            key = normalize(r.get("stop_name", ""))
            if r.get("location_type", "").strip() == "1" and key in wanted:
                stations[r["stop_id"]] = key
        for r in stops:
            if r.get("location_type", "").strip() not in ("", "0"):
                continue
            key = normalize(r.get("stop_name", ""))
            parent = stations.get(r.get("parent_station", "").strip())
            group = key if key in wanted else parent
            if group is None:
                continue
            sid = r["stop_id"]
            idx.stops[sid] = StopInfo(
                stop_id=sid,
                name=r.get("stop_name", "").strip(),
                platform=(r.get("platform_code") or "").strip() or None,
            )
            idx.groups.setdefault(group, []).append(sid)
        group_of = {sid: g for g, sids in idx.groups.items() for sid in sids}

        # One pass over stop_times: candidate rows + last stop of every trip
        # (a trip ending here is an arrival, not a departure).
        last_seq: dict[str, int] = {}
        candidates: list[tuple[str, StopTime, int]] = []
        for r in _rows(zf, "stop_times.txt"):
            trip_id = r["trip_id"]
            try:
                seq = int(r.get("stop_sequence") or 0)
            except ValueError:
                seq = 0
            if seq > last_seq.get(trip_id, -1):
                last_seq[trip_id] = seq
            group = group_of.get(r["stop_id"])
            if group is None or (r.get("pickup_type") or "").strip() == "1":
                continue
            secs = _secs(r.get("departure_time") or r.get("arrival_time") or "")
            if secs is None:
                continue
            candidates.append((group, StopTime(r["stop_id"], trip_id, secs), seq))
        for group, st, seq in candidates:
            if seq < last_seq.get(st.trip_id, seq + 1):
                idx.stop_times.setdefault(group, []).append(st)
        for rows in idx.stop_times.values():
            rows.sort(key=lambda s: s.secs)
        needed = {st.trip_id for rows in idx.stop_times.values() for st in rows}

        routes = {
            r["route_id"]: (r.get("route_short_name") or r.get("route_long_name") or "?").strip()
            for r in _rows(zf, "routes.txt")
        }
        for r in _rows(zf, "trips.txt"):
            if r["trip_id"] in needed:
                idx.trips[r["trip_id"]] = Trip(
                    line=routes.get(r["route_id"], "?"),
                    service_id=r["service_id"],
                    headsign=(r.get("trip_headsign") or "").strip(),
                )
        ends: list[date] = []
        for r in _rows(zf, "calendar.txt"):
            start, end = _date(r.get("start_date", "")), _date(r.get("end_date", ""))
            if start is None or end is None:
                continue
            days = tuple(
                r.get(d, "0").strip() == "1"
                for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            )
            idx.weekly[r["service_id"]] = (start, end, days)
            ends.append(end)
        for r in _rows(zf, "calendar_dates.txt"):
            day = _date(r.get("date", ""))
            if day is None:
                continue
            idx.exceptions.setdefault(r["service_id"], {})[day] = (
                r.get("exception_type", "").strip() == "1"
            )
            ends.append(day)
        idx.valid_until = max(ends) if ends else None
    _LOGGER.debug(
        "GTFS index: %d stops, %d stop times, %d trips",
        len(idx.stops), sum(len(v) for v in idx.stop_times.values()), len(idx.trips),
    )
    return idx


def departures(
    idx: Index, name: str, start: datetime, end: datetime
) -> list[Departure]:
    """Timetable departures of a stop group between start and end."""
    key = normalize(name)
    rows = idx.stop_times.get(key, [])
    out: list[Departure] = []
    first = start.astimezone(TZ).date() - timedelta(days=1)  # trips past midnight
    last = end.astimezone(TZ).date()
    day = first
    while day <= last:
        # GTFS times count from "noon minus 12 h" of the service day (DST safe).
        base = datetime.combine(day, time(12), TZ) - timedelta(hours=12)
        active: dict[str, bool] = {}
        for st in rows:
            trip = idx.trips.get(st.trip_id)
            if trip is None:
                continue
            if trip.service_id not in active:
                active[trip.service_id] = idx.active(trip.service_id, day)
            if not active[trip.service_id]:
                continue
            when = (base + timedelta(seconds=st.secs)).astimezone(timezone.utc)
            if start <= when <= end:
                info = idx.stops.get(st.stop_id)
                out.append(
                    Departure(
                        line=trip.line,
                        destination=trip.headsign,
                        time=when,
                        scheduled=when,
                        delay=None,
                        realtime=False,
                        platform=st.stop_id,
                        source=SOURCE_TIMETABLE,
                        platform_label=info.platform if info else None,
                    )
                )
        day += timedelta(days=1)
    out.sort(key=lambda d: d.time)
    return out


class GtfsFeed:
    """Cached feed file + the index for the configured stops."""

    def __init__(self, path: Path, url: str) -> None:
        self.path = path
        self.url = url
        self._meta_path = path.with_suffix(".json")
        self.index: Index | None = None
        self.downloaded: datetime | None = None
        self.last_error: str | None = None
        self._wanted: frozenset[str] = frozenset()

    def _meta(self) -> dict:
        try:
            return json.loads(self._meta_path.read_text())
        except (OSError, ValueError):
            return {}

    def load_meta(self) -> None:
        """Read download timestamp of the cached file (blocking)."""
        ts = self._meta().get("downloaded")
        if ts and self.path.exists():
            try:
                self.downloaded = datetime.fromisoformat(ts)
            except ValueError:
                self.downloaded = None

    async def download(self, session: aiohttp.ClientSession, hass_executor) -> bool:
        """Fetch the feed if changed. Returns True when a new file was stored."""
        meta = await hass_executor(self._meta)
        headers = {"User-Agent": "mhd-ba-homeassistant"}
        if self.path.exists():
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        try:
            async with session.get(
                self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 304:
                    changed, body = False, None
                else:
                    resp.raise_for_status()
                    body = await resp.read()
                    changed = True
                new_meta = {
                    "etag": resp.headers.get("ETag") or meta.get("etag"),
                    "last_modified": resp.headers.get("Last-Modified") or meta.get("last_modified"),
                    "downloaded": datetime.now(timezone.utc).isoformat(),
                }
        except (aiohttp.ClientError, TimeoutError) as err:
            self.last_error = f"download failed: {err}"
            _LOGGER.warning("GTFS %s, using cached copy", self.last_error)
            return False

        def _store() -> None:
            if body is not None:
                with zipfile.ZipFile(io.BytesIO(body)) as zf:  # validate before replacing
                    if "stop_times.txt" not in zf.namelist():
                        raise GtfsError("stop_times.txt missing")
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(body)
                tmp.replace(self.path)
            self._meta_path.write_text(json.dumps(new_meta))

        try:
            await hass_executor(_store)
        except (OSError, zipfile.BadZipFile, GtfsError) as err:
            self.last_error = f"invalid feed: {err}"
            _LOGGER.warning("GTFS %s, using cached copy", self.last_error)
            return False
        self.downloaded = datetime.fromisoformat(new_meta["downloaded"])
        self.last_error = None
        return changed

    async def reindex(self, wanted: set[str], hass_executor, force: bool = False) -> None:
        keys = frozenset(normalize(n) for n in wanted)
        if not force and self.index is not None and keys == self._wanted:
            return
        if not self.path.exists():
            raise GtfsError("no timetable downloaded yet")
        try:
            self.index = await hass_executor(build_index, self.path, set(keys))
        except (OSError, zipfile.BadZipFile, KeyError) as err:
            raise GtfsError(str(err)) from err
        self._wanted = keys
