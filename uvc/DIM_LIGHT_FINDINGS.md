# Fixed short exposure under recording light: what the prototype can do

Scope: whether the UVC control prototype can hold the cameras at a fixed exposure under 5 ms
(the analysis specification) under real recording light, and what constraints, conditions
or calibrations that needs. The answers to the three brief questions (control while
streaming, persistence, control ranges) are in `REPORT.md`; this report adds what a trial
under real lighting showed. Test data, pictures and per-camera results are kept in the
lab's own documentation, not in this repository.

## The tool

`probe.py dimsweep` (operator instructions: `DIM_LIGHT_TEST.md`) runs on a rig whose camera
layout is unknown:
- it finds every target camera (kinds A, B, C) by USB id and name string, and identifies
  which camera is behind each OpenCV index by a brightness marker (dark, or bright when the
  scene is too dim);
- on each camera while it streams at 640x480 MJPEG, 30 fps: it measures in auto, then sets
  manual exposure (default 2, 3, 4 and 10 ms) and at each steps gain through its range,
  measuring luminance statistics per step (Y mean and percentiles, temporal noise, shares
  of clipped and black pixels, frame rate);
- it saves pictures (lossless stills at a selection of settings, two consecutive frames,
  a 3 s clip) and the system's camera lists, asks the operator four questions for the notes,
  and packs everything into one zip file per run, also when the run is interrupted;
- it restores every camera to its own defaults and reads them back, also on Ctrl-C or an
  error; a failure to save a picture never stops the measurements or the restore.

Supporting tools: `dim_results.py` (log to per-camera tables) and `pair_analysis.py` (where in
the picture the frame-to-frame change comes from). Neither opens a camera.

**Example case:** one deployment rig with eight cameras, all kind C, run twice on
2026-11-25 about 85 minutes apart, under the recording lamps with IR-pass filters and no
animal present.

## What the trial showed about the code

- **Manual exposure and gain take effect while streaming and read back as written**, at
  every step on every camera (2-10 ms; gain 0-100 in steps of 10). Identification by the
  dark marker worked first time on every camera in real lighting.
- **The frame rate was unaffected** by any setting: 29.2-30.4 fps at every one of 368 steps
  per run, with no failed reads.
- **Restore was reliable:** every camera read back at its defaults after its sweep and at
  the end of both runs.
- **The response is repeatable:** at the same setting, a camera's Y mean agreed between the
  two runs within a few units (for example 50.4 and 48.4; 164.9 and 164.1). A calibration
  measured once is therefore meaningful for the same scene and lighting.
- **The tool runs on an offline Mac** with only the install steps in `DIM_LIGHT_TEST.md`.
  Results travel as one zip file (97 MB for eight cameras in the example).

## What it showed about auto exposure

The cameras' auto exposure does not meet the specification under real recording light:
- The exposure auto chose cannot be read directly: on kind A the exposure read in auto mode
  is the stored value, not the live exposure (`REPORT.md`, goal 1), and on kind C the reads
  in auto in this trial likewise showed only the stored default.
- It can be bounded from brightness. At fixed gain a longer exposure gives a brighter
  image, and auto can raise gain at most to the camera's maximum. So the manual exposure
  that reproduces auto's brightness at maximum gain is the shortest auto can have used.
- In the example, on six of the eight views auto's brightness needed about 7.6-8 ms even at
  maximum gain (about 17-19 ms at gain 0): **auto ran above 5 ms**. On the two brightest
  views it was **below 2 ms**. Auto held 30 fps in both cases, so it stayed within the frame
  interval.
- Auto exposure also changes when the scene changes (an animal entering or leaving), so
  only a fixed manual setting gives a known, constant exposure.

## Constraints and calibration a fixed exposure needs

1. **Settings must be chosen per camera (per view), not per kind or per rig.** Cameras of
   the same kind on the same rig differed by a factor of about 100 at the same setting (Y mean
   1 to 165 at 2 ms, gain 0), because lighting and scene differ per view.
2. **A per-camera calibration procedure is feasible with the existing tool.** One
   candidate: for each camera take the longest exposure at or under the limit that does not
   clip at minimum gain, then raise gain until clipping or maximum gain. The thresholds
   (how much clipping, how bright is enough) are for the analysis team to set; the tool
   reports the measures they would need (Y mean and percentiles, ge240/ge250, black share).
   The clip measure must allow for the kind: decoded output clips at about 244-245 on kinds
   A and B and at 255 on kind C (`REPORT.md`).
3. **Very bright views need exposures under 2 ms.** In the example two views clipped heavily
   already at 2 ms, gain 0, and the estimate at 5 ms, gain 0 is Y about 220. The tool
   supports this: `--exposures 5 10 20 50` sweeps 0.5, 1, 2 and 5 ms (units of 0.1 ms).
   Sub-2 ms steps have not been measured yet.
4. **On dim views, brightness under 5 ms is limited by the light.** In the example, the
   estimate at 5 ms and maximum gain was Y about 60-90 on six views, against 100-130 in auto.
   If that is not enough for the analysis, the remedy is more light on those views, not a
   camera setting.
5. **Gain trades brightness for noise.** From gain 0 to 100 at 2-4 ms, Y rose 2.4 to 11
   times, temporal noise 1.8 to 3.8 times, and Y divided by noise 1.3 to 2.9 times, across the
   six dim views of the example.
6. **Calibrate with the real scene, including an animal.** Frame-to-frame variation (the
   noise column) depends strongly on scene detail, not only on the sensor. In the example's
   frame pairs there was no whole-picture brightness change (no lamp flicker) and no
   whole-picture shift. The change was 1.5 to 5 times larger on detailed pixels than on
   flat ones, and it rose with the amount of detail in the view, consistent with MJPEG
   compression of fine detail. Empty cages have little in focus, so an empty-scene
   calibration understates both the detail and the noise that recordings will have.
7. **Re-calibrate when lamps, filters, hoods, lenses or camera positions change**; the
   calibration is specific to each view's light and scene.
8. **Apply per camera by USB location, then keep it.** Settings are written by USB device,
   not by OpenCV index, so a camera must be matched to its location (the identification
   method, or a fixed location-to-view table). Whether manual settings persist across stream
   start and re-enumeration, and where the hook belongs, are answered in `REPORT.md`.

## Not yet shown

- Exposures under 2 ms (needed for bright views).
- Any setting with an animal in view.
- A fixed manual setting held through a long recording with the recorder itself (the hook
  integration).
