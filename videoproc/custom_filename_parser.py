"""
This lab's legacy filename rules for the 2025 longitudinal experiment, used by
videoproc/util.parse_filenames for any name that is not a standard
rat<ID>_<view>_<YYYYMMDD>_<HH-MM-SS>.mp4. Version-controlled on the legacy branch.

Each rule is exact: every field comes from a closed set or is strictly parsed, and a name
that fails any check is rejected so a human looks at it. A rule for one case is never
applied to another.

Known typo cases NOT yet handled (names from these are rejected until each gets a rule
scoped to its rat, camera and date range):
  - doubled underscore after the rat ID, e.g. rat560__buddy_20250715_09-22-07
    (seen in OLD560_Longitudinal on buddy, home and lid; a hand-typed camera name)
  - station and rat both present, e.g. stn13_rat559_home_20250708_04-54-09 and
    stn13_rat559__home_20250708_04-54-09 (seen in OLD559_camnames; stn13 is rat560's
    station, so these names contradict themselves)
"""

import re
from pathlib import Path

# station era: rats were stably assigned to stations, so the station names the subject
STATION_TO_RAT = {
    "stn09": "rat556",
    "stn10": "rat557",
    "stn11": "rat558",
    "stn12": "rat559",
    "stn13": "rat560",
    "stn14": "rat561",
    "stn15": "rat562",
    "stn16": "rat563",
}

# the leading camera slot: 01-04 on odd-numbered stations, 05-08 on even-numbered ones
SLOTS_ODD_STATION = frozenset("1234")
SLOTS_EVEN_STATION = frozenset("5678")

# 0<slot>_stn<NN>_<view>_<YYYYMMDD>_<HH-MM-SS>, e.g. 04_stn09_buddy_20250627_15-34-12
STATION_ERA_STEM = re.compile(r"0(\d)_(stn\d{2})_([a-z]+)_(\d{8})_(\d{2}-\d{2}-\d{2})")


def parse(filename: str) -> tuple[str, str, str, str] | None:
    """(subjectID, view, YYYYMMDD, HH-MM-SS) for a filename matching a legacy rule exactly,
    else None. The extension is checked here, so a rule may accept a variant if one is
    ever needed."""
    from util import RECORDING_DATE_FIRST, RECORDING_DATE_LAST, VIDEO_EXT, VIEWS, parse_recording_datetime

    name = Path(filename)
    if name.suffix != VIDEO_EXT:
        print(f"    custom parser: '{filename}' rejected: extension '{name.suffix}' is not {VIDEO_EXT}")
        return None
    m = STATION_ERA_STEM.fullmatch(name.stem)
    if m:
        slot, station, camera_view, filming_date, filming_time = m.groups()
        subj_ID = STATION_TO_RAT.get(station)
        slots = SLOTS_ODD_STATION if int(station[3:]) % 2 else SLOTS_EVEN_STATION
        recorded = parse_recording_datetime(filming_date, filming_time)
        if (
            subj_ID is not None
            and slot in slots
            and camera_view in VIEWS
            and recorded is not None
            and RECORDING_DATE_FIRST <= recorded.date() <= RECORDING_DATE_LAST
        ):
            return subj_ID, camera_view, filming_date, filming_time
    return None
