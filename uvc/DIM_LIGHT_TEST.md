# Dim-light camera test: instructions for the test operator

This test measures how each camera's image changes when its exposure is fixed at 2, 3, 4
and 10 ms in turn and, at each of those, its gain is stepped up through its whole range. It
is run under the real recording lights. The analysis needs exposures under 5 ms; 10 ms is
included for comparison, so the log shows a warning for it. That warning is expected. The
script prints **measurements only**: luminance values, noise and frame rate. It makes no
judgement of whether an image is usable. That assessment belongs to the analysis team,
from these numbers and from pictures the script saves of each camera at a selection of
settings.

Every camera is put back to its own automatic settings at the end of its sweep. The script
reads the settings back to confirm.

Total time: up to about 45 minutes to install the first time (a few minutes on a Mac where
it was done before), then about 2 minutes per camera (about 15 minutes for eight cameras).

"The requester" below means the person who asked for this test and receives the results.
When a step says to stop and contact the requester, send them what the Terminal shows (a
screenshot or the copied text).

## If you were told these instructions have been updated

If you have already done step 1 on this Mac, get the latest version of the instructions and
the test code with this command:

```
cd ~/Desktop/ratrixcam-uvc && git pull
```

It should end with a summary of the changed files, or with `Already up to date.` Your log
files and the Python environment from step 1f are kept. Then reopen this file to read the
new version. If the command shows an error, stop and send the Terminal output to the requester.

## Before you start

- **The recorder must not be running.** Stop the recording as you normally do and close its
  Terminal windows. Close any other program that uses a camera, such as Photo Booth,
  FaceTime or Zoom.
- Leave the cameras where they are, with their lamps, filters and hoods exactly as for a
  recording. Do not unplug or move anything.
- Set the lights as they are during a recording. This test is about that light.
- If possible, nothing should move in front of the cameras during the test. Motion makes the
  noise figures larger. If animals are present, that is fine; the test asks about it at the
  start (step 4).
- Have a pen and paper ready: in step 2 you write down two numbers, N and M, which you need
  in step 4.

## 1. Install (once per Mac)

Open Terminal. Run each block, one at a time. Steps 1b to 1d install three things that a
recording Mac usually does not have: Apple's command-line tools (which include `git`),
Homebrew (a package installer), and libusb (the USB library the script needs). Each step
first checks whether the thing is already there, so on a Mac where this was done before,
those steps take a second each. You need the Mac's administrator password, and an internet
connection.

**1a. Check for Python and OpenCV**, the ones the recorder itself uses:

```
cd ~ && python3 --version && python3 -c "import cv2, numpy; print('opencv', cv2.__version__, 'numpy', numpy.__version__)"
```

It should print a Python version, then a line beginning `opencv`. These are already
installed on a recording Mac. If either line shows an error instead, stop: the recorder's
Python is not the one Terminal finds, and the test cannot be run on this Mac until that is
sorted out with the requester.

**1b. Apple's command-line tools (for `git`).** Check:

```
cd ~ && xcode-select -p && git --version
```

- If it prints a folder (such as `/Library/Developer/CommandLineTools`) and a `git version`
  line, they are installed. Go on to 1c.
- If it prints an error, or a window pops up offering to install the tools, install them:

  ```
  cd ~ && xcode-select --install
  ```

  A window opens. Click **Install** and agree to the licence. It takes about 5 to 15
  minutes. When the window says the software was installed, close it, then run the check
  above again; it should now print the folder and the `git version` line.

**1c. Homebrew.** Check:

```
cd ~ && (ls /opt/homebrew/bin/brew || ls /usr/local/bin/brew) 2>&1
```

- If one path is listed without "No such file", Homebrew is installed. Go on to 1d.
- If both say "No such file", install Homebrew with the command from its website
  (https://brew.sh), copied here:

  ```
  cd ~ && /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```

  - It asks for the Mac's password (nothing shows as you type; press Return at the end),
    then asks you to press Return to continue. It takes a few minutes.
  - At the end it prints **Next steps** with one or two commands to run. Run them exactly as
    printed (copy each line into Terminal). On most Macs they are:

    ```
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile && eval "$(/opt/homebrew/bin/brew shellenv)"
    ```

  - Then run the check above again; one path should now be listed.

**1d. libusb.** Check:

```
cd ~ && ls /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib 2>&1
```

- If one of the two paths is listed without "No such file", libusb is installed (the other
  path saying "No such file" is normal). Go on to 1e.
- If both say "No such file", install it:

  ```
  cd ~ && (/opt/homebrew/bin/brew install libusb || /usr/local/bin/brew install libusb)
  ```

  Then run the check again; one path should now be listed.

**1e. Get the test code.** It goes into a new folder on the Desktop, next to the recorder's
own folder, which it does not touch.

```
cd ~/Desktop && git clone --branch uvc-prototype https://github.com/preinagel/ratrixcam.git ratrixcam-uvc
```

If the folder `~/Desktop/ratrixcam-uvc` already exists from an earlier visit, update it
instead of cloning:

```
cd ~/Desktop/ratrixcam-uvc && git pull
```

**1f. Make the script's Python environment** and install its one extra package, pyusb. Skip
this if it was done on an earlier visit.

```
cd ~/Desktop/ratrixcam-uvc && python3 -m venv --system-site-packages uvc/.venv && uvc/.venv/bin/pip install pyusb
```

It must be made with the same `python3` as step 1a, so that the script uses the recorder's
OpenCV.

## 2. Find out which cameras this rig has

Neither command in this step opens a camera; they only list what is plugged in. (The test
saves both lists itself in step 4; here they are only for counting.)

**2a. The test cameras (kinds A, B and C):**

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPUSBDataType 2>&1 | awk '/Camera:/{c=1; print; next} c && /Product ID|Location ID/{print} /Location ID/{c=0}'
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
cd ~/Desktop/ratrixcam-uvc && system_profiler SPCameraDataType 2>&1 | grep -E "Model ID|Unique ID"
```

Each camera shows as a "Model ID" line and a "Unique ID" line. **Write down M**: the number of
"Model ID" lines.

- **M equals N:** there is no other camera. Use the step 4 command as it is.
- **M is larger than N:** there is another camera, such as a webcam. The test never writes to
  it, but it takes one of the camera numbers, so in step 4 you use the second command, with
  your M.
- **The output looks different from this description, or shows nothing:** stop and send the
  Terminal output to the requester.

## 3. Check that nothing else has the cameras

Make sure the recorder is stopped (see "Before you start"). The script also refuses to run
if the folder `~/Desktop/temp` exists, because the lab recorder uses that folder while it
records. If it refuses for that reason but you are sure the recorder is stopped, **do not
delete the folder**: it may hold video that has not been processed yet. Check with the
requester first.

## 4. Run the test

This sweeps every camera in turn. Everything it produces goes into one new folder, named
after the date and time (for example `uvc/results/dim-2026-11-26-143000/`), and at the end
the folder is packed into one zip file. That zip file is the only thing you send.

**If M equals N** (no other camera):

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py dimsweep
```

**If M is larger than N** (another camera is present): type your M in place of the `M` at the
end of `--count M`:

```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py dimsweep --count M
```

**First, answer four questions.** The test starts by asking, one at a time: which Mac, room
and stations this is; whether the lamps, IR filters and hoods are as for a recording;
whether animals are present or anything will move in view; and anything else to note. Type
each answer and press Return (for the last one, just Return if there is nothing). The
answers are saved with the results.

**Then check the camera count.** A few seconds after the last answer, a line appears that
begins with the date and time, then `selected`, a number, and `camera(s) of any target kind
(A, B, C)`.
- **The number equals your N:** carry on and let the test run.
- **It does not:** press Ctrl-C once, wait for the prompt, run the safety restore in step 5,
  and send the Terminal output to the requester.

**How long it runs:** about 2 minutes per camera, so about 15 to 20 minutes for eight
cameras. It ends by itself. The last lines are `final read-back, every target camera`
followed by `OK` lines, then `packing the results folder ...` and finally a line beginning
`RESULTS FILE TO SEND:` with the zip file's name and size; then the Terminal prompt comes
back. You do not need to watch it the whole time, but stay nearby so you can answer the
camera-permission question below and check the end. If it is still running after 30
minutes, or nothing new has appeared in the Terminal for 5 minutes, press Ctrl-C once, run
the safety restore in step 5, and send the Terminal output to the requester.

While it runs:
- The first time, macOS may ask whether Terminal may use the camera. Click Allow. If you
  clicked Don't Allow, press Ctrl-C once, run the safety restore in step 5, open System
  Settings > Privacy & Security > Camera, turn Terminal on, close and reopen Terminal, and
  start step 4 again.
- Nothing appears on screen except text in the Terminal: the script shows no picture, and
  there is nothing to open in a browser. Behind the scenes it changes each camera's
  settings: for about two seconds it makes the image very dark (or, in very dim light, very
  bright) to check which camera it is talking to, and then it steps through the exposures
  and gains. Along the way it saves pictures from each camera into the results folder.
- The log shows `WARNING: exposure 10 ms is NOT under 5 ms` for every camera. This is
  intended.
- If another camera is present (M larger than N), the log shows `IDENTIFICATION FAILED` for
  that camera's number. This is expected: it is not a test camera and is skipped.
- Do not touch the cameras or cables until the `RESULTS FILE TO SEND` line appears.
- If you have to stop the run early, press Ctrl-C once and wait for the prompt. The script
  puts the camera it was working on back to automatic by itself and still packs what it
  has into a zip file. Then run the safety restore in step 5 anyway, to check every camera.

Each run makes its own new folder and zip file, so running it again never overwrites an
earlier run.

## 5. Safety restore (only if a run was stopped early or ended with an error)

This puts every test camera back to its own automatic settings and reads them back:

```
cd ~/Desktop/ratrixcam-uvc && mkdir -p uvc/results && uvc/.venv/bin/python uvc/probe.py restore --kind all 2>&1 | tee uvc/results/dim-restore.log
```

Every camera should end with `OK` lines.

## 6. Send the results

Copy **the zip file** named on the `RESULTS FILE TO SEND:` line to the requester. It is in
`~/Desktop/ratrixcam-uvc/uvc/results/`, with a name like `dim-2026-11-26-143000.zip`, and
holds the log, the camera lists, your answers to the four questions and the pictures
(expect roughly 100 to 200 MB for eight cameras).

Also copy `dim-restore.log` from the same folder if you ran step 5, and tell the requester
about anything unusual during the run (an error message, a camera that stayed dark, someone
entering the room).

## What the zip file contains (for the requester and the analysis team)

- `dim-sweep.log`: everything the Terminal showed (described below);
- `notes.txt`: the operator's answers to the four questions, with the date and time;
- `usb-devices.txt`, `cameras.txt`: the full USB and camera lists (system_profiler);
- one folder per camera, `index<N>-0x<location>/`, whose file names all begin with the
  camera's location and kind (for example `0x01142000_C_...`):
  - `..._auto_before.png`, `..._auto_after.png`: a frame in auto exposure, before and after
    the sweep;
  - `..._<E>ms_gain<G>.png`: a frame at each exposure E (2, 3, 4, 10 ms) at gains 0, 50
    and 100, the last frame of that step's measurement;
  - `..._auto_pair1.png`, `..._auto_pair2.png`: two consecutive frames in auto, the kind of
    pair the noise column is computed from;
  - `..._auto_clip.avi`: 3 seconds of video in auto.

  All pictures are the frames as OpenCV delivers them, after the camera's MJPEG
  compression. The PNG files are lossless copies of those frames, full size, with nothing
  drawn on them. The clip is compressed again (MJPEG, quality 90) to keep it small, so it is
  not pixel-identical to what the camera sent; use the PNG files for pixel values.

## What the log contains

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
