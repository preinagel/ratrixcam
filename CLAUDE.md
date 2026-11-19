# ratrixcam UVC control prototype

You are prototyping manual control of the cameras' automatic settings (exposure and gain)
for ratrixcam, a 24/7 multi-camera recorder on macOS. This file is your brief. This is an
experiment with a written result; nothing you make is merged by you.

## The question
The cameras adjust exposure and gain on their own during a run, which changes their frame
rate. OpenCV's AVFoundation backend on macOS does not implement the exposure or
auto-exposure properties (set() returns False; see GitHub #51), so the route is UVC control
requests sent to the camera directly. Two candidate routes:
1. uvc-util (open source, builds from source with the Xcode command-line tools present on
   this Mac), which talks to the camera through IOKit;
2. a few dozen lines over pyusb (libusb is installed by hand on this Mac; pyusb goes into
   a virtual environment in this folder).

## Which cameras
The test rig may hold several camera models at once. The target is one of them: the
Arducam 1080p day/night camera (OV2710 sensor) with the Sonix Technology UVC controller,
USB vendor 0x0c45 (3141 decimal), product 0x6369 (25449 decimal); the system report shows
it as "UVC Camera VendorID_3141 ProductID_25449". Solve it for that camera. Whether the
same code works for the sibling product 0x6366 (25446), the Logitech webcam, or a built-in
camera is a secondary question, to be noted in the report if you learn anything, not
pursued. Select cameras by vendor and product id, never by position in a list.

Answer these three, each with a yes or no and the evidence:
1. Does macOS let a user process send UVC control requests to the camera while AVFoundation
   holds it open (i.e. while a cv2.VideoCapture is streaming from it)?
2. Do the settings stick when the camera's stream starts, and after the camera is unplugged
   and re-enumerated? Many UVC cameras revert to auto on either.
3. Are the exposure mode, the exposure time and the gain exposed as writable controls on
   this model, and what ranges and units do they report?
Then say which hook location the evidence supports: one pass in the supervisor before the
cameras launch (settings applied by device, sidestepping the index-to-device question of
GitHub #60), or a call in each camera server after its capture opens.

## Hardware rules
- Never run anything against a camera while a recording is in progress. Check first:
  `pgrep -fl ratrixcam` must be empty and ~/Desktop/temp must not exist.
- After every experiment, restore every control you changed to its automatic mode and
  read it back to confirm; some cameras keep a manual setting across power cycles, and a
  rig left in manual would record wrongly later. Say in the report what you restored.
- Only the target cameras above. Any other camera entry in the system report (the sibling
  product, the webcam, a built-in camera) is not yours to touch unless the user says so.
- macOS may show a camera-permission dialog the first time a new program opens a camera;
  you cannot click it. Stop and tell the user.

## Deliverables, all under uvc/
- uvc/REPORT.md: the three answers with evidence (commands, outputs, timings), the hook
  recommendation, what was installed and how, and what was restored.
- The working script or tool: uvc/*.py for the pyusb route, or the uvc-util checkout and
  build under uvc/ for that route; a short uvc/README.md on how to run it is allowed.
- Commit on this branch as you go, one subject line each. Never push.

## Rules for this folder
- Everything under uvc/; nothing outside it is touched. The camera modules, gui/, videoproc/,
  tests/ and docs/ are read-only reference.
- Python standard library plus pyusb in a virtual environment under uvc/; no other packages
  without asking. Homebrew is not yours to run.
- Never request anything outside the sandbox; report a sandbox failure instead.
- Stop and ask at scope boundaries: anything outside uvc/, any install beyond pyusb, any
  question the brief does not answer.
