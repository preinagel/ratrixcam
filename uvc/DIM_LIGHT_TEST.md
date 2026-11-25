# Dim-light camera test: instructions for the test operator

This test measures how each camera's image changes when its exposure is fixed at 2, 3, 4
and 10 ms in turn and, at each of those, its gain is stepped up through its whole range. It
is run under the real recording lights. The analysis needs exposures under 5 ms; 10 ms is
included for comparison, so the log shows a warning for it. That warning is expected. The
script prints **measurements only**: luminance values, noise and frame rate. It makes no
judgement of whether an image is usable. That assessment belongs to the analysis team,
from these numbers.

Every camera is put back to its own automatic settings at the end of its sweep. The script
reads the settings back to confirm.

Total time: about 20 minutes to install, then about 2 minutes per camera (about 15 minutes
for eight cameras).

## Before you start

- **The recorder must not be running.** Stop the recording as you normally do and close its
  Terminal windows. Close any other program that uses a camera, such as Photo Booth,
  FaceTime or Zoom.
- Leave the cameras where they are, with their lamps, filters and hoods exactly as for a
  recording. Do not unplug or move anything.
- Set the lights as they are during a recording. This test is about that light.
- If possible, nothing should move in front of the cameras during the test. Motion makes the
  noise figures larger. If animals are present, that is fine, but write it down (see step 6).
- Have a pen and paper ready: in step 2 you write down two numbers, N and M, which you need
  in step 4.

## 1. Install (once per Mac)

Open Terminal. Run each block, one at a time.

**1a. Check that Terminal has the tools.** This prints a version number for each tool. If
any line shows an error, stop and tell the rig owner.

```
cd ~ && git --version && python3 --version && python3 -c "import cv2, numpy; print('opencv', cv2.__version__, 'numpy', numpy.__version__)"
```

The last part checks for the OpenCV that the recorder itself uses. It is already installed
on a recording Mac.

**1b. Check for libusb**, the USB library the script needs:

```
cd ~ && ls /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib 2>&1
```

- If one of the two paths is listed without "No such file", libusb is installed. Go on to 1c.
- If both say "No such file" and the Mac has Homebrew, install libusb with:

  ```
  cd ~ && brew install libusb
  ```

  Then repeat the check.
- If the Mac has no Homebrew, stop and tell the rig owner.

**1c. Get the test code.** It goes into a new folder on the Desktop, next to the recorder's
own folder, which it does not touch.

```
cd ~/Desktop && git clone --branch uvc-prototype REPOSITORY-ADDRESS ratrixcam-uvc
```

(The rig owner fills in REPOSITORY-ADDRESS before handing these instructions over.)

If the folder `~/Desktop/ratrixcam-uvc` already exists from an earlier visit, update it
instead of cloning:

```
cd ~/Desktop/ratrixcam-uvc && git pull
```

**1d. Make the script's Python environment** and install its one extra package, pyusb. Skip
this if it was done on an earlier visit.

```
cd ~/Desktop/ratrixcam-uvc && python3 -m venv --system-site-packages uvc/.venv && uvc/.venv/bin/pip install pyusb
```

It must be made with the same `python3` as step 1a, so that the script uses the recorder's
OpenCV.

## 2. Find out which cameras this rig has

Neither command in this step opens a camera; they only list what is plugged in.

**2a. The test cameras (kinds A, B and C):**

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPUSBDataType 2>&1 | tee uvc/logs/dim-usb-cameras.log | grep -E "Camera:|Product ID|Location ID"
```

Each camera shows as three lines: its name, its product id and its location. The kind is:

| Name line | Product ID | Kind |
|---|---|---|
| USB 2.0 Camera | 0x6369 | A |
| USB 2.0 Camera | 0x6366 | B |
| USB Camera | 0x6366 | C |

**Write down N**: the number of cameras of kinds A, B and C together.

**2b. Every camera macOS can open, including any webcam:**

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPCameraDataType 2>&1 | tee uvc/logs/dim-all-cameras.log | grep -E "Model ID|Unique ID"
```

Each camera shows as a "Model ID" line and a "Unique ID" line. **Write down M**: the number of
"Model ID" lines.

- **M equals N:** there is no other camera. Use the step 4 command as it is.
- **M is larger than N:** there is another camera, such as a webcam. The test never writes to
  it, but it takes one of the camera numbers, so in step 4 you use the second command, with
  your M.
- **The output looks different from this description, or shows nothing:** stop and tell the
  rig owner.

## 3. Check that nothing else has the cameras

Make sure the recorder is stopped (see "Before you start"). The script also refuses to run
if the folder `~/Desktop/temp` exists, because the lab recorder uses that folder while it
records. If it refuses for that reason but you are sure the recorder is stopped, **do not
delete the folder**. Tell the rig owner.

## 4. Run the test

This sweeps every camera in turn and writes everything to one log file.

**If M equals N** (no other camera):

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py dimsweep 2>&1 | tee uvc/logs/dim-sweep-run1.log
```

**If M is larger than N** (another camera is present): type your M in place of the `M` at the
end of `--count M`:

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py dimsweep --count M 2>&1 | tee uvc/logs/dim-sweep-run1.log
```

**First, check the third line.** Within a few seconds the test prints its third line, which
begins with the date and time, then `selected`, a number, and `camera(s) of any target kind
(A, B, C)`.
- **The number equals your N:** carry on and let the test run.
- **It does not:** press Ctrl-C once, wait for the prompt, run the safety restore in step 5,
  and tell the rig owner.

While it runs:
- The first time, macOS may ask whether Terminal may use the camera. Click Allow. If you
  clicked Don't Allow, stop and tell the rig owner.
- Each camera's picture changes for about two seconds, so that the script can check which
  camera it is talking to. In normal light it goes very dark; in very dim light it goes very
  bright instead. Then the picture changes step by step through the exposures and gains.
  This is expected.
- The log shows `WARNING: exposure 10 ms is NOT under 5 ms` for every camera. This is
  intended.
- If another camera is present (M larger than N), the log shows `IDENTIFICATION FAILED` for
  that camera's number. This is expected: it is not a test camera and is skipped.
- Do not touch the cameras or cables until the last line says `final read-back`.
- If you have to stop the run early, press Ctrl-C once and wait for the prompt. The script
  puts the camera it was working on back to automatic by itself. Then run the safety
  restore in step 5 anyway, to check every camera.

If you run it again, change `run1` to `run2` in the log name, so the first log is kept.

## 5. Safety restore (only if a run was stopped early or ended with an error)

This puts every test camera back to its own automatic settings and reads them back:

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py restore --kind all 2>&1 | tee uvc/logs/dim-restore-run1.log
```

Every camera should end with `OK` lines.

## 6. Write down the conditions and send the files

Create a short note in `uvc/logs/dim-notes.txt` with:
- the date and time;
- the room and rig name;
- N and M from step 2;
- which lamps were on, and whether the IR filters and hoods were fitted as for recording;
- whether animals or anything else moved in front of the cameras;
- anything unusual, such as an error message or a camera that stayed dark.

Send these files to the rig owner, all from `~/Desktop/ratrixcam-uvc/uvc/logs/`:
- `dim-usb-cameras.log`;
- `dim-all-cameras.log`;
- `dim-sweep-run1.log` (and any later runs);
- `dim-restore-run1.log`, if you ran it;
- `dim-notes.txt`.

## What the log contains (for the rig owner and the analysis team)

For each camera index:
- the line `index N is kind K at 0x...`: which camera, and which kind, the numbers belong to;
- the format the camera streamed (640x480 MJPEG at 30 fps requested);
- which marker identified it (`dark`, or `bright` when the scene was too dim for the dark
  one);
- one table row each for auto exposure before the sweep; each gain step (0 to 100 in steps
  of 10) at 2, 3, 4 and 10 ms; and auto exposure after.

The columns:
- **brightness**: mean of all pixel values (blue, green, red), 0-255, as in the lab logs;
- **y_mean, y_p1, y_p50, y_p99, y_max**: luminance (Y = 0.299 R + 0.587 G + 0.114 B), its
  mean, its 1st, 50th and 99th percentiles over the picture, and its maximum (averaged over
  the frames of the step);
- **noise**: temporal noise of one frame, the frame-to-frame standard deviation of
  luminance divided by the square root of 2. Motion in view raises it. The table gives the
  median over frame pairs, which limits the effect of brief motion;
- **snr**: y_mean divided by noise;
- **ge240, ge250**: percentage of pixels with luminance of 240 or more, and of 250 or more.
  - In the lab run (2026-11-19), bright areas piled up at about 244-245 on kinds A and B,
    and at 255 on kind C. A few stray pixels reached 247-255 on every kind.
  - So clipping on A and B shows in ge240 and in y_p50/y_p99 near 244, not in ge250;
    on C it shows in both columns;
- **black**: percentage of pixels with luminance of 5 or less;
- **fps**: frames delivered per second in that step.

The statistics are computed on every second pixel in each direction (a quarter of the
pixels), from the frames as OpenCV delivers them, i.e. after the camera's MJPEG
compression, as the recorder receives them.
