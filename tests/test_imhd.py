"""imhd.sk payload parsing against recorded boards."""

from datetime import timedelta

from custom_components.mhd_ba.imhd import Board, parse_tabs

from .conftest import events


def _first_tabs(stop_id):
    return next(e["data"] for e in events(stop_id) if e["event"] == "tabs")


def test_parse_recorded_board():
    deps = parse_tabs(_first_tabs(324))
    assert deps, "recorded board has departures"
    assert all(d.line and d.line != "►" for d in deps)
    assert deps == sorted(deps, key=lambda d: d.time)
    d = next(d for d in deps if d.line == "39")
    assert d.realtime and d.delay == 0 and d.vehicle == "1:3503"
    assert d.destination == "Ostredky, Súhvezdná"
    assert d.platform == "798"
    # `casCP` is the timetable time; `cas` the prediction.
    assert d.scheduled.second == 0
    assert abs(d.time - d.scheduled) < timedelta(minutes=1)


def test_delay_uses_timetable_time():
    deps = parse_tabs(_first_tabs(325))
    late = [d for d in deps if d.delay and d.delay > 0]
    assert late
    for d in late:
        assert d.time > d.scheduled


def test_marker_rows_dropped():
    data = [{"nastupiste": 1, "tab": [
        {"linka": "►", "cas": 1794170925000, "casCP": 1794170925000, "typ": "cp", "cielStr": "X"},
        {"linka": "21", "cas": 1794170925000, "typ": "cp", "cielStr": "Y"},
    ]}]
    deps = parse_tabs(data)
    assert [d.line for d in deps] == ["21"]
    assert deps[0].realtime is False and deps[0].delay is None


def test_board_replaces_only_sent_platforms():
    b = Board()
    b.update([{"nastupiste": 1, "tab": [{"linka": "1", "cas": 1}]},
              {"nastupiste": 2, "tab": [{"linka": "2", "cas": 2}]}])
    b.update([{"nastupiste": 2, "tab": []}])
    assert [d.line for d in b.departures] == ["1"]


def test_garbage_ignored():
    assert parse_tabs(None) == []
    assert parse_tabs([None, {"tab": [None, {"cas": "x"}]}]) == []
