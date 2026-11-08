"""Constants for Bratislava MHD departures."""

from __future__ import annotations

DOMAIN = "mhd_ba"

# imhd.sk realtime board (socket.io, the same feed the imhd.sk web board uses)
IMHD_URL = "https://imhd.sk"
IMHD_SOCKETIO_PATH = "rt/sio2"

# DPB static timetable (GTFS). Published by DPB, linked from opendata.bratislava.sk.
GTFS_URL = (
    "https://www.arcgis.com/sharing/rest/content/items/"
    "aba12fd2cbac4843bc7406151bc66106/data"
)
GTFS_FILENAME = "mhd_ba_gtfs.zip"  # cached under .storage
GTFS_REFRESH_HOURS = 24
GTFS_HORIZON_HOURS = 6  # timetable departures are generated this far ahead

# Config entry
CONF_STOP_ID = "stop_id"  # imhd.sk `st` parameter
CONF_GTFS_STOP = "gtfs_stop"  # GTFS stop name (all platforms with that name)
CONF_MAX_DEPARTURES = "max_departures"
CONF_PLATFORM_MAP = "platform_map"  # learned: imhd platform id -> GTFS stop_id

DEFAULT_MAX_DEPARTURES = 10

# Sensors drop departed connections this often between pushes.
REFRESH_SECONDS = 30
# A departure stays listed this long after its time (vehicle may still be at the stop).
DEPARTED_GRACE_SECONDS = 30

# Push: reconnect when no `tabs` arrive for this long (normally ~every minute).
WATCHDOG_SECONDS = 300
# Realtime data older than this is not trusted; timetable is used instead.
STALE_SECONDS = 300

SOURCE_REALTIME = "imhd"
SOURCE_TIMETABLE = "gtfs"
