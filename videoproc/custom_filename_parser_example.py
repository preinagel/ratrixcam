"""
Template for a site-specific filename parser.

videoproc recognizes one filename shape natively: <subjectID>_<view>_<YYYYMMDD>_<HH-MM-SS>.
If your archive contains files named by an older or different convention, copy this file to
custom_filename_parser.py in the same folder and fill in parse() with your own rules; videoproc
will call it for any filename the standard rule does not recognize. Keep custom_filename_parser.py
out of version control -- it describes your data, not the tool.

parse() receives the full filename, extension included, and must return
(subjectID, view, YYYYMMDD, HH-MM-SS), or None if the name matches none of your rules.
Make each rule exact (closed sets for subject and view, a real date and time) so that a
name that fits no rule is reported as unparseable rather than silently mis-parsed.
"""

import re
from pathlib import Path


def parse(filename: str) -> tuple[str, str, str, str] | None:
    # Example rule: a site code in front of the subject ID, siteCode_subjectID_view_date_time.
    # Uncomment and adapt; keep the pattern and the value sets as tight as your data allow.
    #
    # name = Path(filename)
    # if name.suffix != ".mp4":
    #     return None
    # m = re.fullmatch(r"(site\d{2})_(rat\d{3})_([a-z]+)_(\d{8})_(\d{2}-\d{2}-\d{2})", name.stem)
    # if m:
    #     _site, subj_ID, view, date, time = m.groups()
    #     return subj_ID, view, date, time
    return None
