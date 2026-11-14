# Screen Recorder & Editor

Record your screen, then trim, cut, speed up, rotate or join the videos. Everything runs locally on your PC using FFmpeg, so nothing is uploaded.

It runs on **Windows** and **Linux** (Debian, Ubuntu, TUXEDO OS, Kubuntu, Mint and similar). It's the same app on both.

## Tabs

| Tab | What it does |
|---|---|
| **Record** | Record the whole screen, part of it (Windows and Linux X11) or one window (Linux Wayland). You can set the frame rate and quality, record from a microphone and computer sound, and choose whether to show the mouse pointer |
| **Edit** | Mark the parts to delete on a filmstrip, then change speed, rotate, sound or size |
| **Join clips** | Combine several videos into one |

Edited videos are saved as new files, so your originals are never overwritten.

## Editing a video

The Edit tab works like the iPhone's video editor: the video is laid out as a filmstrip under the preview.

- **Hover** anywhere on the filmstrip to see that moment in a pop-up snippet and in the big preview. The snippet turns red when that moment is marked for deletion.
- **Drag across the filmstrip** to mark a part to delete. It turns red.
- **Drag a red part's edges** to make it bigger or smaller. Drag its middle to move it.
- **Right-click** a red part (or select it and press **Delete**) to unmark it. **Ctrl+Z** undoes.
- **Scroll the mouse wheel** over the filmstrip to zoom in for precise cuts; **Shift + scroll** or the scrollbar moves along.
- **Click** the filmstrip to move the playhead, and use the **arrow keys** to step through it (hold Shift for bigger steps).
- **Preview with cuts** (or **Space**) plays from the playhead with the red parts skipped, so you can check the edit before saving.
- You can also type exact times for the selected part.

**Save edited video** joins the remaining parts into a new file next to the original, called "name edited".

## How to run on Windows

1. Download `screen_recorder.py` and `Run Screen Recorder.bat` into the same folder.
2. Double-click **Run Screen Recorder.bat**.
   - If Python isn't installed, the window tells you the command to run: `winget install Python.Python.3.13`.
3. If FFmpeg isn't installed, click the button at the top of the app. It runs `winget install Gyan.FFmpeg` for you.

**F9** starts and stops recording from anywhere, even while the window is minimised.

### Recording sound on Windows

- **Computer sound** is set to **What this PC is playing** by default. It records whatever comes out of your speakers or headphones (games, videos, calls) straight from Windows, so Stereo Mix isn't needed. Pick **None** to record without it.
- **Microphone** records your voice. If you choose both, they're mixed together.
- The first time you start the app, `Run Screen Recorder.bat` installs the small part that does this (`PyAudioWPatch`). If that install fails, the app still works with a microphone or Stereo Mix.
- If you switch speakers or headphones, the next recording uses whichever is the Windows default when you press Start.

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
