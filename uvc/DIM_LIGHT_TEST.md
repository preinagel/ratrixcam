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

Total time: about 20 minutes to install, then about 2 minutes per camera (about 16 minutes
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

**1d. Make the script's Python environment** and install its one extra package, pyusb:

```
cd ~/Desktop/ratrixcam-uvc && python3 -m venv --system-site-packages uvc/.venv && uvc/.venv/bin/pip install pyusb
```

It must be made with the same `python3` as step 1a, so that the script uses the recorder's
OpenCV.

## 2. Find out which cameras this rig has

This only lists what is plugged in. It does not open any camera.

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPUSBDataType 2>&1 | tee uvc/logs/dim-usb-cameras.log | grep -E "Camera:|Product ID|Location ID"
```

Each camera shows as three lines: its name, its product id and its location. The camera
kind is:

| Name line | Product ID | Kind |
|---|---|---|
| USB 2.0 Camera | 0x6369 | A |
| USB 2.0 Camera | 0x6366 | B |
| USB Camera | 0x6366 | C |

Count the cameras of kinds A, B and C together. That number is N. The script finds the same
cameras by itself; its third line should say it selected N cameras. Any other camera listed
(for example a webcam) is not part of the test and is never written to. It does take an
index, though, so if one is listed, add `--count M` to the command in step 4, where M is
the number of all cameras listed.

## 3. Check that nothing else has the cameras

Make sure the recorder is stopped (see "Before you start"). The script also refuses to run
if the folder `~/Desktop/temp` exists, because the lab recorder uses that folder while it
records. If it refuses for that reason but you are sure the recorder is stopped, **do not
delete the folder**. Tell the rig owner.

## 4. Run the test

This sweeps every camera in turn, indices 0 to N-1, and writes everything to one log file.

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py dimsweep 2>&1 | tee uvc/logs/dim-sweep-run1.log
```

- The first time, macOS may ask whether Terminal may use the camera. Click Allow. If you
  clicked Don't Allow, stop and tell the rig owner.
- During the run, each camera's picture changes for about two seconds, so that the script
  can check which camera it is talking to. In normal light it goes very dark; in very dim
  light it goes very bright instead. Then the picture changes step by step through the
  exposures and gains. This is expected.
- The log shows `WARNING: exposure 10 ms is NOT under 5 ms` for every camera. This is
  intended.
- Do not touch the cameras or cables until the last line says `final read-back`.
- If you have to stop the run early, press Ctrl-C once and wait for the prompt. The script
  puts the camera it was working on back to automatic by itself. Then run the safety
  restore in step 5 anyway, to check every camera.

If you run it again, change `run1` to `run2` in the log name, so the first log is kept.

## 5. Safety restore (only if a run was stopped early or ended with an error)

This puts every target camera back to its own automatic settings and reads them back:

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py restore --kind all 2>&1 | tee uvc/logs/dim-restore-run1.log
```

Every camera should end with `OK` lines.

## 6. Write down the conditions and send the files

Create a short note in `uvc/logs/dim-notes.txt` with:
- the date and time;
- the room and rig name;
- which lamps were on, and whether the IR filters and hoods were fitted as for recording;
- whether animals or anything else moved in front of the cameras;
- anything unusual, such as an error message or a camera that stayed dark.

Send these files to the rig owner, all from `~/Desktop/ratrixcam-uvc/uvc/logs/`:
- `dim-usb-cameras.log`;
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
- **y_mean, y_p1, y_p50, y_p99**: luminance (Y = 0.299 R + 0.587 G + 0.114 B), its mean and
  its 1st, 50th and 99th percentiles over the picture;
- **noise**: temporal noise of one frame, the frame-to-frame standard deviation of
  luminance divided by the square root of 2. Motion in view raises it. The table gives the
  median over frame pairs, which limits the effect of brief motion;
- **snr**: y_mean divided by noise;
- **saturated, black**: percentage of pixels with luminance of 250 or more, and of 5 or
  less;
- **fps**: frames delivered per second in that step.

The statistics are computed on every second pixel in each direction (a quarter of the
pixels), from the frames as OpenCV delivers them, i.e. after the camera's MJPEG
compression, as the recorder receives them.
