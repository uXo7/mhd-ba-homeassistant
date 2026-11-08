# Bratislava MHD for Home Assistant

Departures of Bratislava public transport (DPB) for chosen stops: **realtime from
imhd.sk, with the DPB timetable (GTFS) kept locally as backbone and fallback**.

- The imhd.sk live board lists only the next few trips per line; the timetable
  fills the rest, and each realtime departure replaces its timetable trip
  (same line, same scheduled time) with prediction, delay and vehicle.
- When imhd.sk is unreachable or silent, the board keeps working from the
  cached timetable (`source: gtfs`, `realtime: false`).
- Platform letters (A, B, …) come from GTFS; imhd.sk platform numbers are
  mapped to them automatically by matching trips.

> Unofficial. imhd.sk does not publish an API; this uses the socket.io feed of
> its web departure board and may break or conflict with imhd.sk terms at any
> time. The DPB GTFS feed is published under CC BY-NC 4.0.

## Install

HACS → ⋮ → Custom repositories → `https://github.com/uXo7/mhd-ba-homeassistant`
(category *Integration*) → install **Bratislava MHD** → restart.

Add a stop: *Settings → Devices & services → Add integration → Bratislava MHD*.

| Field | Meaning |
|---|---|
| imhd.sk stop ID or board URL | the `st` value of `https://imhd.sk/ba/online-zastavkova-tabula?st=…` |
| Stop name in the DPB timetable | GTFS stop name, e.g. `Zoo`, `Patrónka`, `Cintorín Slávičie` |
| Name | device / entity name |
| Departures to keep | length of the `departures` list (1–30) |

imhd.sk stop IDs differ from GTFS `stop_code`, hence both fields.

## Entities (per stop)

**`sensor.<name>_next_departure`** — timestamp of the next departure.

| Attribute | |
|---|---|
| `line`, `destination`, `platform`, `delay`, `realtime`, `source` | next departure |
| `departures` | list, see below (not recorded) |
| `platforms` | learned imhd.sk platform → GTFS stop map (not recorded) |
| `info` | imhd.sk service notices, if any (not recorded) |

Each item of `departures`:

```yaml
line: "39"
destination: Ostredky, Súhvezdná
time: "2026-11-08T20:02:45+00:00"      # expected (realtime) or scheduled
scheduled: "2026-11-08T20:03:00+00:00"
delay: 0              # minutes; null = no realtime for this trip
realtime: true        # vehicle tracked by imhd.sk
platform: A           # null when the platform has no letter (e.g. regional lines)
platform_id: "798"    # imhd.sk or GTFS platform id
source: imhd          # imhd | gtfs
vehicle: "1:3503"
vehicle_type: SOR NS 18
vehicle_low_floor: true
vehicle_ac: true
stuck: false
```

**`binary_sensor.<name>_realtime`** (diagnostic) — on while fresh realtime data
arrives. Attributes: `last_error`, `reconnects`, `timetable_loaded`,
`timetable_valid_until`, `timetable_downloaded`, `timetable_error`.

## Card example

```yaml
type: markdown
content: >-
  {% for d in state_attr('sensor.zoo_next_departure', 'departures')[:6] %}
  {%- set m = ((as_datetime(d.time) - now()).total_seconds() / 60) | round(0, 'floor') | int %}
  **{{ d.line }}** {{ d.destination }} · {{ d.platform or '–' }} ·
  {{ as_timestamp(d.time) | timestamp_custom('%H:%M') }}
  ({{ 'teraz' if m < 1 else m ~ ' min' }}){{ ' +' ~ d.delay if d.delay }}
  {% endfor %}
```

## How it works

- **Realtime:** one socket.io connection per stop to `imhd.sk` (path `rt/sio2`),
  `tabStart [stop, "*"]`, server pushes `tabs` (~every minute) and `vInfo`.
  Reconnects with backoff forever; a watchdog drops a silent connection after 5 min.
- **Timetable:** the DPB GTFS zip is downloaded at most daily (conditional GET)
  to `.storage/mhd_ba_gtfs.zip`; only configured stops are indexed. If the
  download fails, the cached copy is used.

## Credits

Protocol handling adapted from [jose1711/ha-imhd](https://github.com/jose1711/ha-imhd) (MIT).
