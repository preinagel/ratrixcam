## Overview
<img align="right" width="300" src="./docs/img/RatcamIcon.png"> The ratrixCam system is an inexpensive, bare-bones open-source hardware and software package designed to record video 24 hours a day 7 days a week for at least 1 week continuously, with the minimum possible frame drops within videos or gaps between videos. It was developed for in-home-cage animal behavior monitoring, but could be used in any application requiring continuous uninterrupted video recording. The code provides a minimal graphical user interface that allows the user to monitor the camera status and session statistics during the run. The code is currently verified to support up to 8 cameras streaming at 30fps, 640x480p. 

See the [Camera User Manual](./docs/CameraUserManual.md), the [VideoProc User Manual](./docs/VideoProcUserManual.md) and [Setup Instructions](./docs/SetUpInstructions.md) for more information.

Project started by Blake Bruell and Pamela Reinagel 2025

With support from the [NIH BRAIN Initiative](https://braininitiative.nih.gov/) and [NINDS](https://www.ninds.nih.gov/) 1R34NS132037-01

## Running the tests

The `videoproc` tools (`compress_drive.py`, `confirm_and_delete.py`) have a pytest suite
under `tests/`. It generates its own synthetic video clips and runs entirely in temporary
directories -- no real recordings or network drives are touched.

Requirements: Python 3.10+, `ffmpeg` on PATH, and the packages `videoproc` itself already
needs (`opencv-python`, `numpy`), plus `pytest`:

    pip install -r requirements-dev.txt

Run the suite from the repository root:

    pytest -v

GitHub Actions runs the same suite on every push and pull request to `dev` and `main`
(see `.github/workflows/tests.yml`).
