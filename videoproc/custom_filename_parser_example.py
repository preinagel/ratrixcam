"""
Template for a site-specific filename parser.

videoproc recognizes one filename shape natively: <subjectID>_<view>_<YYYYMMDD>_<HH-MM-SS>.
If your archive contains files named by an older or different convention, copy this file to
custom_filename_parser.py in the same folder and fill in parse() with your own rules; videoproc
will call it for any filename the standard rule does not recognize. Keep custom_filename_parser.py
out of version control -- it describes your data, not the tool.

parse() receives the filename stem (no extension) and must return
(subjectID, view, YYYYMMDD, HH-MM-SS), or None if the stem matches none of your rules.
"""


def parse(stem: str) -> tuple[str, str, str, str] | None:
    # Example rule: a 5-token shape with a site code in front of the subject ID,
    # siteCode_subjectID_view_date_time. Uncomment and adapt.
    #
    # tokens = stem.split("_")
    # if len(tokens) == 5:
    #     _, subj_ID, view, date, time = tokens
    #     return subj_ID, view, date, time
    return None
