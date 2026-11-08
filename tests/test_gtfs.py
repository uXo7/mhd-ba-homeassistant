"""Timetable index and departures from the trimmed DPB feed."""

from datetime import datetime, timedelta, timezone

from custom_components.mhd_ba import gtfs

TZ = gtfs.TZ


def test_stop_names(tmp_path):
    from .conftest import GTFS_ZIP
    names = gtfs.list_stop_names(GTFS_ZIP)
    assert {"Cintorín Slávičie", "Patrónka", "Zoo"} <= set(names)


def test_groups_and_platform_letters(index):
    labels = {index.stops[s].platform for s in index.groups["cintorin slavicie"]}
    assert labels == {"A", "B", "C"}
    assert index.valid_until is not None


def test_terminus_has_no_arrivals(index):
    # Slávičie is a terminus: trips ending there are arrivals, not departures.
    for st in index.stop_times["cintorin slavicie"]:
        assert index.trips[st.trip_id].headsign != "Cintorín Slávičie"


def test_departures_sorted_in_window(index):
    start = datetime(2026, 11, 9, 7, 0, tzinfo=TZ).astimezone(timezone.utc)
    end = start + timedelta(hours=1)
    deps = gtfs.departures(index, "Zoo", start, end)
    assert deps
    assert deps == sorted(deps, key=lambda d: d.time)
    assert all(start <= d.time <= end for d in deps)
    assert all(d.platform_label in {"A", "B", "C", "D"} for d in deps)
    assert not any(d.realtime for d in deps)


def test_after_midnight_trips(index):
    # Night lines run with times > 24:00 of the previous service day.
    start = datetime(2026, 11, 10, 0, 5, tzinfo=TZ).astimezone(timezone.utc)
    deps = gtfs.departures(index, "Patrónka", start, start + timedelta(hours=1))
    assert any(d.line.startswith("N") for d in deps)


def test_calendar_exception(index):
    # Working-day services are removed on 24.12. (calendar_dates type 2).
    wd = next(s for s in index.weekly if s.startswith("Prac.dny"))
    assert index.active(wd, datetime(2026, 12, 24).date()) is False
    assert index.active(wd, datetime(2026, 11, 9).date()) is True
    assert index.active(wd, datetime(2026, 11, 8).date()) is False  # Sunday


def test_name_normalization():
    assert gtfs.normalize("Cintorín  Slávičie") == "cintorin slavicie"
    assert gtfs.normalize("Prievoz, most") == "prievoz most"
