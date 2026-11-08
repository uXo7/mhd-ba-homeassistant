"""Realtime overlay on the timetable, on recorded data."""

from datetime import datetime, timezone

from custom_components.mhd_ba.board import StopBoard

from .conftest import STOPS, recorded_client


def _board(stop_id, index, fresh=True):
    client, last = recorded_client(stop_id, fresh)
    b = StopBoard(STOPS[stop_id], client)
    b.index = index
    return b, datetime.fromtimestamp(last, timezone.utc)


def test_overlay_slavicie(index):
    b, now = _board(324, index)
    deps = b.departures(now, 12)
    assert len(deps) == 12
    assert deps == sorted(deps, key=lambda d: d.time)
    live = [d for d in deps if d.source == "imhd"]
    tt = [d for d in deps if d.source == "gtfs"]
    assert live and tt, "realtime first, timetable continues the board"
    assert max(d.time for d in live) <= min(d.time for d in tt)
    # no duplicates of the same trip
    keys = [(d.line, d.scheduled.replace(second=0)) for d in deps]
    assert len(keys) == len(set(keys))
    # platform letters from GTFS, vehicle details from vInfo
    assert {d.platform_label for d in deps} <= {"A", "C"}
    assert live[0].extra.get("vehicle_type")
    # timetable rows use imhd.sk's destination text once learned
    assert all(d.destination == "Ostredky, Súhvezdná" for d in deps if d.line == "39")


def test_platform_map_learned(index):
    b, now = _board(325, index)
    b.departures(now, 10)
    labels = {p: index.stops[s].platform for p, s in b.platforms.as_dict().items()}
    assert labels == {"801": "A", "802": "B", "8368": "C", "803": "D"}


def test_delayed_trip_replaces_timetable(index):
    b, now = _board(325, index)
    deps = b.departures(now, 30)
    late = [d for d in deps if d.delay and d.delay > 0]
    assert late
    for d in late:
        same = [x for x in deps if x.line == d.line and x.scheduled == d.scheduled]
        assert same == [d]


def test_unmatched_realtime_kept(index):
    # Regional IDS BK lines are on the imhd.sk board but not in the DPB feed.
    b, now = _board(254, index)
    gtfs_lines = {index.trips[st.trip_id].line for st in index.stop_times["patronka"]}
    foreign = {d.line for d in b.client.board.departures if d.time >= now} - gtfs_lines
    assert foreign, "recorded board has a non-DPB line"
    deps = b.departures(now, 200)
    assert foreign <= {d.line for d in deps if d.source == "imhd"}


def test_fallback_to_timetable(index):
    b, now = _board(324, index, fresh=False)
    deps = b.departures(now, 5)
    assert len(deps) == 5
    assert all(d.source == "gtfs" and not d.realtime for d in deps)


def test_no_timetable_realtime_only(index):
    b, now = _board(324, None)
    b.index = None
    deps = b.departures(now, 20)
    assert deps and all(d.source == "imhd" for d in deps)
