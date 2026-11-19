# Frame drops: findings for the camera developer (2026-11-19)

Reply to `uvc_developer_frame_drop_investigation.md`, findings only. Sources:
- the records the user placed in this clone under `for_uvc_developer/` (the paths in the
  request were superseded by the user);
- this prototype's own logs in `uvc/logs/`.

No camera was opened for this file.

## 1. Which camera is behind each view

`for_uvc_developer/camera_config_261119.json` lists view1-view8 in order with no per-camera
format, so every camera ran at 640x480 @ 30.
- The recorder opens entry k as cv2 index k (`ratrixcam_server.py:461`).
- The user's tests show index order follows rig-port order, so **view N = cv2 index N-1 =
  rig port N**.
- This prototype confirmed three of those indices directly. Writing a marker exposure to a
  camera by USB location darkened exactly the expected index: index 0 = port 1, index 1 =
  port 2, index 3 = port 4. Index 0 = port 1 also held across replugs in replug runs 2 and 3.
  The other five follow from the port order.

Kinds, by USB product id and name string (`uvc/REPORT.md`, eight-camera table; the profiler's
location id is given because the request asks for the `Unique ID`, which is location plus
vendor and product):

| View | Port | Location id | USB id | Name string | Kind |
|---|---|---|---|---|---|
| view1 | 1 | 0x01141000 | 0c45:6369 | USB 2.0 Camera | A |
| view2 | 2 | 0x01142000 | 0c45:6366 | USB 2.0 Camera | B |
| view3 | 3 | 0x01143000 | 0c45:6369 | USB 2.0 Camera | A |
| view4 | 4 | 0x01144000 | 0c45:6366 | USB Camera | C |
| view5 | 5 | 0x00141000 | 0c45:6369 | USB 2.0 Camera | A |
| view6 | 6 | 0x00142000 | 0c45:6366 | USB 2.0 Camera | B |
| view7 | 7 | 0x00143000 | 0c45:6366 | USB Camera | C |
| view8 | 8 | 0x00144000 | 0c45:6366 | USB 2.0 Camera | B |

**The request's three drop classes coincide exactly with the three kinds:**
- "one in 43" = view1, view3, view5 = all of kind A;
- "one in 32" = view4, view7 = all of kind C;
- "no drop" = view2, view6, view8 = all of kind B.

Each kind sits on fixed ports, so these records cannot separate kind from port. A port swap
(the request's item 5) would.

## 2. The records, re-analysed

`uvc/.venv/bin/python -I uvc/frames_analysis.py for_uvc_developer` (standard library; output
reproduced from the run of 2026-11-19).
- A "1-frame gap" is an arrival interval of 1.5 or more times the clip's median interval
  that rounds to one missing frame. The spacing is the number of frames between
  consecutive 1-frame gaps.
- "Idle at gap" means `worked_s` under 25% of the gap and `waited_s` over 60% of it.
- Clips are analysed separately, and totals are per camera.

| View | Kind | Clips | Frames | Seconds | Delivered fps | 1-frame gaps / min | Gaps of 2+ frames | Idle at gap | Most common spacings (spacing x count) |
|---|---|---|---|---|---|---|---|---|---|
| view1 | A | 6 | 10319 | 354 | 29.13 | 47.1 | 27 | 293/305 | 42x163, 41x41 |
| view3 | A | 6 | 10061 | 344 | 29.23 | 46.7 | 24 | 282/292 | 42x167, 41x40 |
| view5 | A | 6 | 9765 | 334 | 29.22 | 47.1 | 23 | 267/285 | 42x158, 41x45 |
| view4 | C | 6 | 9818 | 339 | 28.95 | 62.0 | 23 | 353/373 | 31x206, 30x73 |
| view7 | C | 6 | 9429 | 324 | 29.09 | 61.3 | 19 | 335/350 | 31x202, 30x69 |
| view2 | B | 6 | 10470 | 349 | 30.00 | 5.8 | 22 | 42/56 | no periodic spacing |
| view6 | B | 6 | 9889 | 328 | 30.10 | 5.3 | 19 | 40/48 | no periodic spacing |
| view8 | B | 6 | 9614 | 319 | 30.15 | 5.3 | 15 | 32/43 | no periodic spacing |

- The periodic spacing appears in **every clip** of every kind A and kind C camera:
  42/41 on A, 31/30 on C. The irregular gaps on top of it are a smaller share.
- On kind B, 1-frame gaps occur at about 5 per minute with no dominant spacing. That is more
  than the request's "zero or one per minute". The difference may come from the 1.5 x median
  threshold, which counts arrival jitter of about 50 ms as a gap. Check against your own
  criterion.
- Camera clock: this script's estimate (mean of undisturbed intervals) is 30.35-30.51 fps
  per camera, and higher in each camera's first clip (30.53-30.78). That is not the request's
  30.27 / 30.32 fps. The estimate is sensitive to how intervals near a gap are excluded, so
  use your own figure for the clock.

## 3. Independent evidence from this prototype: single camera, probe process, not ratrixcam

The probe streams one identified camera in its own process (cv2 `VideoCapture(index)`, width,
height and fps set as the recorder sets them). Its delivered rate, summed over every
stream window of 2 s or more with 0 failed reads:

| Kind (camera) | Format | Windows | Seconds | Frames | Delivered fps |
|---|---|---|---|---|---|
| A (port 1) | 640x480 @ 30 | 42 | 97 | 2873 | **29.62** |
| B (port 2) | 640x480 @ 30 | 16 | 38 | 1146 | **30.16** |
| C (port 4) | **352x288** @ 30 | 16 | 38 | 1122 | **29.53** |

Logs: A = q1-run3, aetrack-run1, gainsweep-run1, persist-run1, persist-run2; B = q1-B-run1,
gainsweep-B-run1, persist-B-run1; C = q1-C-run1, gainsweep-C-run1, persist-C-run2.
- Kinds A and C deliver fewer frames than B outside ratrixcam too, with only that camera
  streaming in the process.
- Kind C's shortfall also appears at 352x288, a different frame size from the recorder's.
- These are window counts, so they are coarser than the per-frame records.

## 4. Negotiated stream parameters (request item 3), from VS_COMMIT_CONTROL GET_CUR

Read from the camera itself while the probe streamed it (`uvc/probe.py`, `vs_commit`). Max packet
sizes are from the descriptors (`uvc/logs/list-all-run2.log`).

| Kind (camera) | Requested | Format | Frame | dwFrameInterval | dwMaxVideoFrameSize | dwMaxPayloadTransferSize | Log |
|---|---|---|---|---|---|---|---|
| A (port 1) | 640x480 @ 30 | MJPEG | 2 (640x480) | 333333 (30.0 fps) | 614989 | 800 | persist-run2 |
| B (port 2) | 640x480 @ 30 | MJPEG | 3 (640x480) | 333333 (30.0 fps) | 614989 | 800 | q1-B-run1, gainsweep-B-run1, persist-B-run1 |
| C (port 4) | 352x288 @ 30 | MJPEG | 8 (352x288) | 333333 (30.0 fps) | 203341 | 800 | q1-C-run1, gainsweep-C-run1, persist-C-run2 |

- All three kinds offer the same isochronous alternate settings on their (first) streaming
  interface (wMaxPacketSize; bits 12-11 = extra transactions per microframe):
  - 0x0080: 128 B x 1
  - 0x0100: 256 B x 1
  - 0x0320: 800 B x 1
  - 0x0b20: 800 B x 2
  - 0x1320: 800 B x 3
  - 0x1400: 1024 B x 3
- A max payload of 800 B corresponds to **0x0320, 800 B x 1 transaction per microframe**: 6.4 MB/s,
  about 51 Mb/s reserved per camera at 640x480 MJPEG.
- Kind C at 640x480 and any kind at 1080p have not been read. The two-cameras-at-1080p
  question needs a commit read while streaming 1080p.
- Every commit read so far (9 reads, all kinds) is MJPEG, including cases where YUY2 would
  also give 30 fps. Nothing in ratrixcam requests a format.

## 5. Frame intervals offered at 640x480 (request item 4, first half), from the descriptors

| Kind | MJPEG 640x480 | YUY2 640x480 |
|---|---|---|
| A | 30 fps only (333333) | 30 fps only |
| B | 30 fps only | 30 fps only |
| C | 30 fps only | **15 fps only** |

- Each frame descriptor lists a single discrete interval. **No kind offers 25 fps at
  640x480**, and only C offers 15, and only in YUY2.
- The second half of item 4 (does the drop change at 25 or 15 fps requested) is untested.

## 6. Not done: the per-frame FID / PTS / SCR sequence (request items 1, 2 and 5)

- This prototype's tooling sends control requests only; it has never received video
  payloads.
- Reading payload headers on macOS needs the VideoStreaming interface claimed and an
  alternate setting selected. Apple's UVC driver holds that interface exclusively: run 1's
  claim of the VideoControl interface was refused with "exclusive access".
- So the camera would have to be detached from Apple's driver, which libusb on macOS does
  only with root, and isochronous transfers would have to be read directly. pyusb has no
  isochronous support, so that means a ctypes reader over libusb.
- That is outside this prototype's brief and needs root, so it is not started; whether to
  do it on this Mac or move to a Linux host is the rig owner's decision. The request's
  alternative, a Linux host with the stock uvcvideo driver and its trace options, avoids
  both.

## 7. Outcome (added after the developer's `avfoundation_probe_report_20261119.md`)

- The developer's AVFoundation-level capture shows no missing frames on any kind. The
  USB-clock stamps form a uniform sequence with no hole, and the dropped-frame callback
  never fires.
- Each kind runs at its own wire rate: A 29.60, B 30.55, C 29.35 fps. OpenCV delivers the
  same count. This prototype's single-camera rates in section 3 agree (A 29.62, B 30.16,
  C 29.53 at 352x288).
- The 42/41 and 31/30 beat is in how the host hands frames over (callbacks every 33.1 ms,
  then one pause). It is not a frame lost on the Mac.
- **Correction to this file's earlier hypothesis.** The guess was "an empty slot on the
  camera's USB-side grid". That is not supported: on the wire, the stream is uniform at the
  kind's own rate. Whether the sensor skips internally cannot be seen from the host, and it
  does not matter for the recording.
- **Section 6's root/Linux FID/PTS capture is no longer needed for this question**, and it
  is not planned.
- The negotiated format agrees with the developer's. The camera-side commit reads MJPEG, and
  AVFoundation reports the decoded MJPEG as '420v' 640x480.

## What to hand over

This file and `uvc/frames_analysis.py` (standard library only; run it on the
`for_uvc_developer/` folder to reproduce section 2). The logs and `uvc/REPORT.md` cited above
are on branch `uvc-prototype` of the UVC prototype clone, not pushed. They are only needed to
check sections 1, 3, 4 and 5 against the raw output.
