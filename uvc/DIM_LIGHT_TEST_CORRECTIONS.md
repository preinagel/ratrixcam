# Dim-light camera test: corrections to the operator instructions (2026-11-24)

These corrections apply to `uvc/DIM_LIGHT_TEST.md`, which is unchanged. Use them in place of
its step 2 and alongside its step 4. Everything else in that file stands.

## Why

- Step 2 said "the script finds the same cameras by itself; its third line should say it
  selected N cameras" before any script had been run. It did not say what to do with that
  information.
- Step 2's command lists only USB devices whose name ends in "Camera". A webcam named
  otherwise (for example "HD Pro Webcam C920") would not appear, so you would not know to
  add `--count`.

## Step 2 (replaces step 2): find out which cameras this rig has

Neither command opens a camera.

**2a. The cameras of kinds A, B and C:**

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPUSBDataType 2>&1 | tee uvc/logs/dim-usb-cameras.log | grep -E "Camera:|Product ID|Location ID"
```

Each camera shows as three lines: its name, its product id and its location. The kind is:

| Name line | Product ID | Kind |
|---|---|---|
| USB 2.0 Camera | 0x6369 | A |
| USB 2.0 Camera | 0x6366 | B |
| USB Camera | 0x6366 | C |

**Write down N**, the number of cameras of kinds A, B and C together. You need it in step 4.

**2b. Every camera macOS can open, including any webcam:**

```
cd ~/Desktop/ratrixcam-uvc && system_profiler SPCameraDataType 2>&1 | tee uvc/logs/dim-all-cameras.log | grep -E "Model ID|Unique ID"
```

Each camera shows as a "Model ID" line and a "Unique ID" line. **Write down M**, the number of
"Model ID" lines.
- If M equals N, there is no other camera. Use the step 4 command as it is.
- If M is larger than N, there is another camera, such as a webcam. Add `--count M` to the
  end of the step 4 command, with your number in place of M. That camera is never written
  to, but it still takes one of the camera numbers, and without `--count` one of the test
  cameras could be missed.
- If the output looks different from this description, or shows nothing, stop and tell the
  rig owner. This command's output was not checked on the lab Mac when these corrections
  were written.

## Step 4 (addition): check the count when the test starts

When the test starts, look at its **third line**. It begins with the date and time, then
`selected`, a number, and `camera(s) of any target kind (A, B, C)`.
- **If the number equals your N**, carry on and let the test run.
- **If it does not**, press Ctrl-C once, wait for the prompt, run the safety restore in step 5,
  and tell the rig owner.

## Files to send (addition to step 6)

Also send `dim-all-cameras.log` from `~/Desktop/ratrixcam-uvc/uvc/logs/`.
