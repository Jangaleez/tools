# Screen Recorder & Editor

Record your screen, then trim, cut, speed up, rotate or join the videos. Everything runs locally on your PC using FFmpeg, so nothing is uploaded.

It runs on **Windows** and **Linux** (Debian, Ubuntu, TUXEDO OS, Kubuntu, Mint and similar). It's the same app on both.

## Tabs

| Tab | What it does |
|---|---|
| **Record** | Record the whole screen, part of it (Windows and Linux X11) or one window (Linux Wayland). You can set the frame rate and quality, record from a microphone and computer sound, and choose whether to show the mouse pointer |
| **Edit** | Trim or cut out a section, change speed and rotate |
| **Join clips** | Combine several videos into one |

Edited videos are saved as new files, so your originals are never overwritten.

## How to run on Windows

1. Download `screen_recorder.py` and `Run Screen Recorder.bat` into the same folder.
2. Double-click **Run Screen Recorder.bat**.
   - If Python isn't installed, the window tells you the command to run: `winget install Python.Python.3.13`.
3. If FFmpeg isn't installed, click the button at the top of the app. It runs `winget install Gyan.FFmpeg` for you.

**F9** starts and stops recording from anywhere, even while the window is minimised.

## How to install on Linux

1. Download the folder (green **Code** button → **Download ZIP**, or `git clone https://github.com/Jangaleez/tools.git`).
2. Open a terminal in the `screen-recorder` folder and run:
   ```
   bash install-linux.sh
   ```
   This installs FFmpeg, Python's Tk, and the GStreamer/PipeWire parts used for Wayland recording, then adds **Screen Recorder & Editor** to your app menu.
3. Open it from your app menu.

### Wayland vs X11

The app detects which one you're using and shows it at the top of the window.

- **Wayland** is the default on KDE Plasma 6 (TUXEDO OS, Kubuntu) and GNOME. When you click Start, your desktop shows its own **"share your screen"** window, where you pick a screen or a window. That's a Wayland security feature that stops apps from secretly recording you. Recording a dragged-out area isn't possible on Wayland, so pick **One window** instead.
- **X11** works the same as on Windows, including **Part of the screen**.

**Computer sound** on Linux is listed as **"Monitor of …"**: pick the speakers or headphones you're listening through.

Linux desktops don't let apps use global hotkeys, so **F9** only works while the app's window is focused. You can also stop a Wayland recording from your desktop's own "stop sharing" button. What was recorded is kept.
