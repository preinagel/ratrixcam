"""Per-file decision logic and CSV logging shared by compress_drive.py and confirm_and_delete.py."""

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from util import RAW_CODEC, get_codec_nframes

# fixed vocabulary for the log's `status` column
STATUS_COMPRESSED = "compressed"
STATUS_COPIED = "copied"
STATUS_ALREADY_DONE = "already_done"
STATUS_RAW_AT_DESTINATION = "raw_at_destination"
STATUS_COLLISION = "collision"
STATUS_INVALID_SOURCE = "invalid_source"
STATUS_MISSING_SOURCE = "missing_source"  # listed in the manifest, not found at the source
STATUS_OUTPUT_INVALID = "output_invalid"
STATUS_ERROR = "error"
STATUS_NOT_ATTEMPTED = "not_attempted"
# verify-only outcomes (confirm_and_delete)
STATUS_NOT_AT_DESTINATION = "not_at_destination"
STATUS_DESTINATION_INVALID = "destination_invalid"
STATUS_TOO_RECENT = "too_recent"
STATUS_DECODE_FAILED = "decode_failed"

ACTION_COPY = "copy"
ACTION_COMPRESS = "compress"

LOG_FIELDS = [
    "input_path",
    "output_path",
    "subjID",
    "view",
    "start_time",
    "status",
    "action_item",
    "skipped_reason",
    "error",
    "motion_perc",
    "found_motion",
    "motion_detection_time",
    "fract_frames_exceeding",
    "motion_max",
    "n_frames_over_threshold",
    "motion_review_flag",
    "crf",
    "compression_ratio",
    "compression_success",
    "compression_time",
    "valid_output",
]


class Logger:
    """Per-file CSV log, plus a second CSV holding only the rows flagged for human review"""

    # one attribute per log column (LOG_FIELDS): cleared by reset(), filled by the caller
    input_path: Path | None = None
    output_path: Path | None = None
    subjID: str | None = None
    view: str | None = None
    start_time: str | None = None
    status: str | None = None
    action_item: bool = False
    skipped_reason: str | None = None
    error: str | None = None
    motion_perc: float | None = None
    found_motion: bool | None = None
    motion_detection_time: float | None = None
    fract_frames_exceeding: float | None = None
    motion_max: float | None = None
    n_frames_over_threshold: int | None = None
    motion_review_flag: bool | None = None
    crf: int | None = None
    compression_ratio: float | None = None
    compression_success: bool | None = None
    compression_time: float | None = None
    valid_output: bool | None = None

    def __init__(self, log_path: Path, action_items_path: Path, fields=LOG_FIELDS):
        self.fields = list(fields)
        self.log_path = log_path
        self.action_items_path = action_items_path
        self.status_counts: Counter[str] = Counter()
        self.n_action_items = 0
        self.reset()

        for path in (self.log_path, self.action_items_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, mode="w", newline="") as f:
                csv.writer(f).writerow(self.fields)

    def reset(self) -> None:
        for field in self.fields:
            setattr(self, field, None)
        self.action_item = False

    def append(self) -> None:
        values = [getattr(self, field) for field in self.fields]
        row = ["" if v is None else str(v) for v in values]

        with open(self.log_path, mode="a", newline="") as f:
            csv.writer(f).writerow(row)
        if self.action_item:
            with open(self.action_items_path, mode="a", newline="") as f:
                csv.writer(f).writerow(row)
            self.n_action_items += 1
        self.status_counts[str(self.status)] += 1


class SkipFile(Exception):
    """Ends processing of one file with a known outcome. `action_item` marks rows the user
    must review before trusting that the source has been transferred."""

    def __init__(self, status: str, reason: str, action_item: bool = False):
        super().__init__(reason)
        self.status = status
        self.action_item = action_item


@dataclass
class Decision:
    """Outcome of classifying one source file against its destination path: either an
    action to take, or a SkipFile explaining why nothing will be done."""

    action: str | None
    skip: SkipFile | None
    note: str | None
    input_codec: str | None
    input_n_frames: int | None
    output_exists: bool
    output_codec: str | None
    output_n_frames: int | None


def classify_file(
    source: Path,
    destination: Path,
    *,
    recompress: bool,
    overwrite_raw: bool,
    overwrite_compressed: bool,
    verify_only: bool = False,
) -> Decision:
    """Decide what to do with one source video given what sits at its destination path.

    With verify_only the function reports instead of acting: cases that would copy or
    compress come back as skips (`not_at_destination`, `destination_invalid`). Callers in
    that mode should pass recompress/overwrite_* as False."""
    input_codec, input_n_frames = get_codec_nframes(source)
    input_is_valid = input_codec is not None
    input_is_raw = input_codec == RAW_CODEC

    output_exists = destination.is_file()
    output_codec, output_n_frames = get_codec_nframes(destination) if output_exists else (None, None)
    output_is_valid = output_codec is not None
    output_is_raw = output_codec == RAW_CODEC

    def decision(action=None, skip=None, note=None):
        return Decision(
            action, skip, note, input_codec, input_n_frames, output_exists, output_codec, output_n_frames
        )

    def skipped(status, reason, action_item=False):
        return decision(skip=SkipFile(status, reason, action_item))

    # an invalid source is never copied. Only a raw video already sitting at the destination
    # needs the user's attention (they will likely want it compressed)
    if not input_is_valid:
        if not output_exists:
            return skipped(STATUS_INVALID_SOURCE, "source cannot be opened; nothing at destination")
        if not output_is_valid:
            return skipped(
                STATUS_INVALID_SOURCE, "source cannot be opened; destination exists but also cannot be opened"
            )
        if output_is_raw:
            return skipped(
                STATUS_INVALID_SOURCE,
                "source cannot be opened; destination exists and is raw (uncompressed)",
                action_item=True,
            )
        return skipped(STATUS_INVALID_SOURCE, "source cannot be opened; destination exists and is compressed")

    note = None
    if output_is_valid:
        if output_n_frames != input_n_frames:
            return skipped(
                STATUS_COLLISION,
                f"destination exists with {output_n_frames} frames vs source's {input_n_frames}; not overwriting",
                action_item=True,
            )
        if output_is_raw and not overwrite_raw:
            return skipped(
                STATUS_RAW_AT_DESTINATION,
                "destination exists and is raw (uncompressed); rerun with --overwrite_raw to replace it",
                action_item=True,
            )
        if not output_is_raw and not overwrite_compressed:
            return skipped(STATUS_ALREADY_DONE, "valid compressed output exists, not overwriting")
        note = f"replacing existing {'raw' if output_is_raw else 'compressed'} destination file"
    elif output_exists:
        if verify_only:
            return skipped(STATUS_DESTINATION_INVALID, "destination exists but cannot be opened", action_item=True)
        note = "destination exists but cannot be opened; replacing it"
    elif verify_only:
        return skipped(STATUS_NOT_AT_DESTINATION, "nothing at destination", action_item=True)

    action = ACTION_COPY if (not input_is_raw and not recompress) else ACTION_COMPRESS
    return decision(action=action, note=note)
