# Screen Recorder & Editor

Record your screen, then trim, cut, speed up, rotate or join the videos. Everything runs locally on your PC using FFmpeg, so nothing is uploaded.

## Tabs

| Tab | What it does |
|---|---|
| **Record** | Record the whole screen or a region, with a choice of frame rate and quality, audio devices, and whether the mouse is shown |
| **Edit** | Trim or cut out a section, change speed and rotate |
| **Join clips** | Combine several videos into one |

Edited videos are saved as new files, so your originals are never overwritten.

## How to run (Windows)

1. Download `screen_recorder.py` and `Run Screen Recorder.bat` into the same folder.
2. Double-click **Run Screen Recorder.bat**.
   - If Python isn't installed, the window tells you the command to run: `winget install Python.Python.3.13`.
3. If FFmpeg isn't installed, click the button at the top of the app. It runs `winget install Gyan.FFmpeg` for you.

Python's own `tkinter` is the only other thing it uses, so there are no `pip install` steps.
