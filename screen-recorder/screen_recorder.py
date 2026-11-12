"""Screen Recorder & Editor - record the screen, then trim, cut, speed up, rotate or join videos.

Everything runs on this PC using FFmpeg. Start it with "Run Screen Recorder.bat".
"""
import base64
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Screen Recorder & Editor"
WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
if LINUX:
    import linux_support as lx
    WAYLAND = lx.is_wayland()
else:
    lx = None
    WAYLAND = False
HOTKEY_HINT = "Press F9 to start and stop from anywhere." if WINDOWS else "Press F9 in this window to start and stop."
NOWIN = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v", ".flv", ".mpg", ".mpeg")
CRF = {"High": "18", "Balanced": "23", "Small file": "28"}
NONE = "None"


# --------------------------------------------------------------------------
# Logic (no user interface in this section)
# --------------------------------------------------------------------------

def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:  # where winget puts it; a running app may not see the new PATH yet
        link = Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        if link.exists():
            return str(link)
    return None


def unique_path(folder, stem, ext):
    """A path that does not exist yet, so nothing is ever overwritten."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"{stem}{ext}"
    n = 1
    while candidate.exists():
        candidate = folder / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def parse_time(text):
    """'90', '1:30', '1:02:03.5' -> seconds."""
    text = str(text).strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?(:\d+(\.\d+)?){0,2}", text):
        raise ValueError(f'"{text}" is not a time. Use seconds (90) or minutes:seconds (1:30).')
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return secs


def fmt_time(secs):
    secs = max(0.0, float(secs))
    m, s = divmod(secs, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def probe(ffmpeg, path):
    """Duration, picture size and whether there is sound, read from FFmpeg's description of the file."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, creationflags=NOWIN,
                       stdin=subprocess.DEVNULL)
    text = r.stderr.decode("utf-8", "replace")
    info = {"duration": 0.0, "width": 0, "height": 0, "audio": False, "video": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line and not info["video"]:
            size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line.split("Video:", 1)[1])
            if size:
                info.update(video=True, width=int(size.group(1)), height=int(size.group(2)))
        elif "Audio:" in line:
            info["audio"] = True
    rot = re.search(r"rotation of (-?\d+)", text)
    if rot and abs(int(rot.group(1))) % 180 == 90:  # phone videos stored sideways
        info["width"], info["height"] = info["height"], info["width"]
    if not info["video"]:
        raise ValueError("That file has no video in it, or it could not be read.")
    return info


AUDIO_IDS = {}  # label shown in the list -> the name FFmpeg/GStreamer needs (Linux)


def list_audio_devices(ffmpeg, kind="all"):
    """Names of the sound inputs on offer. On Linux, kind picks "mic" or "system" (what you hear)."""
    if LINUX:
        found = lx.audio_sources()
        AUDIO_IDS.update({label: name for label, name, _ in found})
        if kind == "mic":
            return [label for label, _, mon in found if not mon]
        if kind == "system":
            return [label for label, _, mon in found if mon]
        return [label for label, _, _ in found]
    if not WINDOWS:
        return []
    r = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    devices, section = [], None
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        if "DirectShow video devices" in line:
            section = "video"
        elif "DirectShow audio devices" in line:
            section = "audio"
        m = re.search(r'"([^"]+)"', line)
        if not m or "Alternative name" in line:
            continue
        kind = "audio" if "(audio)" in line else "video" if "(video)" in line else section
        if kind == "audio" and m.group(1) not in devices:
            devices.append(m.group(1))
    return devices


def even(n):
    return int(n) // 2 * 2


def record_command(ffmpeg, out, region=None, fps=30, quality="Balanced", audio=(), mouse=True, screen=None):
    """region is None for the whole desktop, or (x, y, width, height)."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if WINDOWS:
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if mouse else "0"]
        if region:
            x, y, w, h = region
            cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{even(w)}x{even(h)}"]
        cmd += ["-i", "desktop"]
        for name in audio:
            cmd += ["-f", "dshow", "-thread_queue_size", "1024", "-i", f"audio={name}"]
    else:  # Linux with an X11 desktop
        x, y, w, h = region or screen or (0, 0, 1920, 1080)
        cmd += ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "1" if mouse else "0",
                "-video_size", f"{even(w)}x{even(h)}", "-i", f"{os.environ.get('DISPLAY', ':0')}+{x},{y}"]
        for name in audio:
            cmd += ["-f", "pulse", "-thread_queue_size", "1024", "-i", AUDIO_IDS.get(name, name)]
    if len(audio) > 1:
        mix = "".join(f"[{i + 1}:a]" for i in range(len(audio)))
        cmd += ["-filter_complex", f"{mix}amix=inputs={len(audio)}:duration=longest[a]", "-map", "0:v", "-map", "[a]"]
    # even picture size is required by the MP4 video format
    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", CRF[quality], "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    return cmd + [str(out)]


def remux_command(ffmpeg, src, dst, audio_to_aac=False):
    if audio_to_aac:  # Wayland recordings use Opus sound; AAC plays everywhere
        return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c:v", "copy", "-c:a", "aac",
                "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c", "copy",
            "-movflags", "+faststart", str(dst)]


ROTATE = {"None": [], "90 degrees right": ["transpose=1"], "90 degrees left": ["transpose=2"],
          "Upside down": ["hflip", "vflip"], "Mirror": ["hflip"]}
VOLUME = {"Keep": 1.0, "Mute": 0.0, "Quieter (50%)": 0.5, "Louder (150%)": 1.5, "Louder (200%)": 2.0}
SIZES = {"Original": 0, "1080p": 1080, "720p": 720, "480p": 480}
SPEEDS = ["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"]


def edit_command(ffmpeg, src, dst, info, start=0.0, end=None, mode="keep", speed=1.0, rotate="None",
                 volume="Keep", size="Original", fmt="MP4", quality="Balanced"):
    """Returns (command, length of the result in seconds)."""
    dur = info["duration"]
    end = dur if end is None else min(end, dur)
    start = max(0.0, start)
    if end - start < 0.05:
        raise ValueError("The end needs to be after the start.")
    if mode == "remove":  # removing from the very start or to the very end is the same as keeping the rest
        if start <= 0.05 and end >= dur - 0.05:
            raise ValueError("That would remove the whole video.")
        if start <= 0.05:
            mode, start, end = "keep", end, dur
        elif end >= dur - 0.05:
            mode, start, end = "keep", 0.0, start

    gif = fmt == "GIF"
    sound = info["audio"] and not gif and VOLUME[volume] > 0
    vf, af = [], []
    if speed != 1.0:
        vf.append(f"setpts=PTS/{speed}")
        af.append(f"atempo={speed}")
    vf += ROTATE[rotate]
    if gif:
        vf += ["fps=12", f"scale=-2:{SIZES[size] or 480}:flags=lanczos"]
    else:
        vf.append(f"scale=-2:{SIZES[size]}" if SIZES[size] else "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if VOLUME[volume] != 1.0:
        af.append(f"volume={VOLUME[volume]}")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
    if mode == "keep":
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        if end < dur:
            cmd += ["-to", f"{end:.3f}"]
        cmd += ["-i", str(src)]
        graph = f"[0:v]{','.join(vf)}[v]"
        if sound:
            graph += f";[0:a]{','.join(af) or 'anull'}[a]"
        length = (end - start) / speed
    else:
        cmd += ["-i", str(src)]
        graph = (f"[0:v]trim=end={start:.3f},setpts=PTS-STARTPTS[v0];[0:v]trim=start={end:.3f},setpts=PTS-STARTPTS[v1];"
                 f"[v0][v1]concat=n=2:v=1:a=0,{','.join(vf)}[v]")
        if sound:
            graph += (f";[0:a]atrim=end={start:.3f},asetpts=PTS-STARTPTS[a0];[0:a]atrim=start={end:.3f},asetpts=PTS-STARTPTS[a1];"
                      f"[a0][a1]concat=n=2:v=0:a=1{',' + ','.join(af) if af else ''}[a]")
        length = (dur - (end - start)) / speed
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if sound:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    if not gif:
        cmd += ["-c:v", "libx264", "-crf", CRF[quality], "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    return cmd + [str(dst)], length


def join_command(ffmpeg, clips, dst, quality="Balanced"):
    """clips is a list of (path, info). Every clip is fitted to the first clip's picture size."""
    w, h = even(clips[0][1]["width"]), even(clips[0][1]["height"])
    sound = any(info["audio"] for _, info in clips)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
    parts, labels = [], ""
    for i, (path, info) in enumerate(clips):
        cmd += ["-i", str(path)]
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                     f"setsar=1,fps=30,format=yuv420p[v{i}]")
        labels += f"[v{i}]"
        if sound:
            if info["audio"]:
                parts.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")
            else:  # silent clip: fill with silence so the sound stays in step
                parts.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={info['duration']:.3f}[a{i}]")
            labels += f"[a{i}]"
    parts.append(f"{labels}concat=n={len(clips)}:v=1:a={1 if sound else 0}[v]{'[a]' if sound else ''}")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]"]
    if sound:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-crf", CRF[quality], "-preset", "medium", "-movflags", "+faststart", str(dst)]
    return cmd, sum(info["duration"] for _, info in clips)


def run_ffmpeg(cmd, total=0.0, on_progress=None, stop=None):
    """Run a command built with -progress, reporting 0..1 as it goes. Raises on failure or when stopped."""
    dst = Path(cmd[-1])
    with tempfile.TemporaryFile() as errfile:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errfile, creationflags=NOWIN)
        for raw in proc.stdout:
            if stop is not None and stop.is_set():
                proc.kill()
                break
            line = raw.decode("ascii", "replace").strip()
            if line.startswith(("out_time_us=", "out_time_ms=")) and total > 0 and on_progress:
                try:
                    on_progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
                except ValueError:
                    pass
        proc.wait()
        if stop is not None and stop.is_set():
            dst.unlink(missing_ok=True)
            raise InterruptedError
        if proc.returncode != 0:
            errfile.seek(0)
            lines = errfile.read().decode("utf-8", "replace").strip().splitlines()
            dst.unlink(missing_ok=True)
            raise RuntimeError(lines[-1] if lines else "FFmpeg could not finish this video.")
    return dst


def frame_png(ffmpeg, src, at, box=(480, 270)):
    """One frame of the video as PNG bytes, shrunk to fit the preview box."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, at):.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={box[0]}:{box[1]}:force_original_aspect_ratio=decrease",
                        "-f", "image2pipe", "-c:v", "png", "-"],
                       capture_output=True, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    return r.stdout if r.returncode == 0 and r.stdout else None


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------

def open_path(path):
    try:
        os.startfile(path) if WINDOWS else subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        messagebox.showwarning(APP_NAME, f"Could not open it: {e}")


def virtual_screen(root):
    """(x, y, width, height) of the whole desktop across every monitor."""
    if WINDOWS:
        import ctypes
        gm = ctypes.windll.user32.GetSystemMetrics
        return gm(76), gm(77), gm(78), gm(79)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class App:
    def __init__(self, root):
        self.root = root
        self.inbox = queue.Queue()  # worker threads hand work back to the window through this
        root.title(APP_NAME)
        root.geometry("860x720")
        root.minsize(760, 620)
        if LINUX:
            try:
                ttk.Style().theme_use("clam")  # cleaner than Tk's default look on Linux
            except tk.TclError:
                pass
        try:
            ttk.Style().configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)
        except tk.TclError:
            pass

        bar = ttk.Frame(root, padding=(10, 8, 10, 0))
        bar.pack(fill="x")
        self.ff_label = ttk.Label(bar)
        self.ff_label.pack(side="left")
        self.ff_btn = ttk.Button(bar, command=self.install_ffmpeg)
        self.ff_btn.pack(side="right")

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.record = RecordTab(self)
        self.edit = EditTab(self)
        self.join = JoinTab(self)
        for name, tab in (("Record", self.record), ("Edit", self.edit), ("Join clips", self.join)):
            self.nb.add(tab.frame, text=f"   {name}   ")
        self.check_ffmpeg()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(60, self.pump)
        if WINDOWS:
            threading.Thread(target=self.hotkey_loop, daemon=True).start()
        else:  # Linux desktops don't allow global hotkeys from apps, so F9 works while this window is focused
            root.bind_all("<F9>", lambda e: self.record.toggle())

    def ui(self, fn):
        self.inbox.put(fn)

    def pump(self):
        try:
            while True:
                self.inbox.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(60, self.pump)

    def hotkey_loop(self):
        """F9 starts and stops recording from anywhere, even when this window is minimised."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, 0, 0x78):  # 0x78 is F9
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.ui(self.record.toggle)
        except Exception:
            pass

    def check_ffmpeg(self):
        self.ffmpeg = ffmpeg_path()
        missing = lx.missing_tools(WAYLAND) if LINUX else []
        if LINUX and missing:
            self.ff_label.config(text=f"Some parts are missing: {', '.join(missing)}. Click Install for the command.",
                                 foreground="#b3261e")
            self.ff_btn.config(text="Install")
            self.ff_btn.pack(side="right")
            return None if "ffmpeg" in missing else self.ffmpeg
        if self.ffmpeg:
            ready = "FFmpeg is installed and ready."
            if LINUX:
                ready = f"Ready to record ({'Wayland' if WAYLAND else 'X11'} desktop)."
            self.ff_label.config(text=ready, foreground="#1c7548")
            self.ff_btn.pack_forget()
        else:
            self.ff_label.config(text="FFmpeg is needed to record and edit. Install it once, then click Check again.",
                                 foreground="#b3261e")
            self.ff_btn.config(text="Install FFmpeg")
        return self.ffmpeg

    def install_ffmpeg(self):
        if self.check_ffmpeg() and not (LINUX and lx.missing_tools(WAYLAND)):
            self.record.refresh_devices()
            return
        if LINUX:
            here = Path(__file__).resolve().parent
            script = here / "install-linux.sh"
            cmd = f'bash "{script}"' if script.exists() else "sudo apt install " + " ".join(lx.APT_PACKAGES)
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            messagebox.showinfo(APP_NAME, "Open a terminal and run this command (it's been copied for you, "
                                          f"paste with Ctrl+Shift+V):\n\n{cmd}\n\nThen click Check again.")
            self.ff_btn.config(text="Check again")
            return
        if not WINDOWS or not shutil.which("winget"):
            messagebox.showinfo(APP_NAME, "Install FFmpeg from ffmpeg.org, then restart this app.")
            return
        if messagebox.askokcancel(APP_NAME, "This opens a window that runs:\n\nwinget install Gyan.FFmpeg\n\n"
                                            "It downloads FFmpeg (about 100 MB) from the Windows package manager. Continue?"):
            subprocess.Popen('start "Installing FFmpeg" cmd /k winget install --id Gyan.FFmpeg -e', shell=True)
            self.ff_btn.config(text="Check again")

    def need_ffmpeg(self):
        if not self.check_ffmpeg():
            messagebox.showinfo(APP_NAME, "FFmpeg is not installed yet. Click Install FFmpeg at the top first.")
            return None
        return self.ffmpeg

    def close(self):
        if self.record.proc:
            if not messagebox.askokcancel(APP_NAME, "A recording is running. Stop it, save it and close?"):
                return
            self.record.stop(then=self.root.destroy)
            return
        self.root.destroy()


def labelled(parent, text, widget_factory, col, row=0):
    cell = ttk.Frame(parent)
    cell.grid(row=row, column=col, sticky="w", padx=(0, 16), pady=3)
    ttk.Label(cell, text=text).pack(anchor="w")
    w = widget_factory(cell)
    w.pack(anchor="w")
    return w


def combo(parent, text, values, default, col, row=0, width=16):
    var = tk.StringVar(value=default)
    box = labelled(parent, text, lambda p: ttk.Combobox(p, textvariable=var, values=values, state="readonly", width=width), col, row)
    return var, box


class RecordTab:
    def __init__(self, app):
        self.app = app
        self.proc = None
        self.errfile = None
        self.region = None
        self.started = 0.0
        self.last_file = None
        self.counting = False
        f = self.frame = ttk.Frame(app.nb, padding=14)
        f.columnconfigure(0, weight=1)

        intro = "Record the whole screen or just part of it. " + HOTKEY_HINT
        if WAYLAND:
            intro = ("Record a whole screen or one window. When you start, your desktop asks what to share — "
                     "that's normal on Wayland. " + HOTKEY_HINT)
        ttk.Label(f, text=intro,
                  wraplength=780, justify="left").grid(row=0, column=0, sticky="w", pady=(0, 10))

        area = ttk.LabelFrame(f, text="What to record", padding=10)
        area.grid(row=1, column=0, sticky="ew")
        self.area_mode = tk.StringVar(value="full")
        ttk.Radiobutton(area, text="Whole screen", variable=self.area_mode, value="full").grid(row=0, column=0, sticky="w")
        if WAYLAND:
            ttk.Radiobutton(area, text="One window", variable=self.area_mode, value="window").grid(row=0, column=1, sticky="w", padx=(16, 6))
            self.area_label = ttk.Label(area, text="You pick which screen or window in your desktop's sharing dialog.", foreground="#666")
            self.area_label.grid(row=0, column=3, padx=10)
        else:
            ttk.Radiobutton(area, text="Part of the screen", variable=self.area_mode, value="part").grid(row=0, column=1, sticky="w", padx=(16, 6))
            ttk.Button(area, text="Select area", command=self.select_area).grid(row=0, column=2)
            self.area_label = ttk.Label(area, text="No area selected yet.", foreground="#666")
            self.area_label.grid(row=0, column=3, padx=10)

        opts = ttk.LabelFrame(f, text="Picture", padding=10)
        opts.grid(row=2, column=0, sticky="ew", pady=10)
        self.fps, _ = combo(opts, "Frames per second", ["15", "30", "60"], "30", 0, width=8)
        self.quality, _ = combo(opts, "Quality", list(CRF), "Balanced", 1, width=12)
        self.mouse = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Show the mouse pointer", variable=self.mouse).grid(row=0, column=2, sticky="s", pady=4)

        snd = ttk.LabelFrame(f, text="Sound", padding=10)
        snd.grid(row=3, column=0, sticky="ew")
        dev_w = 32 if LINUX else 38
        self.mic, self.mic_box = combo(snd, "Microphone", [NONE], NONE, 0, width=dev_w)
        self.sys, self.sys_box = combo(snd, "Computer sound", [NONE], NONE, 1, width=dev_w)
        ttk.Button(snd, text="Refresh list", command=self.refresh_devices).grid(row=0, column=2, sticky="s", pady=4)
        snd_hint = ("Computer sound needs a device called Stereo Mix (or similar) switched on in Windows Sound settings, "
                    "under Recording devices. If it is not in the list, only the microphone can be recorded.")
        if LINUX:
            snd_hint = ("Computer sound records what's playing through your speakers or headphones "
                        "(listed as \"Monitor of ...\"). Pick the one you're listening on.")
        ttk.Label(snd, foreground="#666", wraplength=780, justify="left", text=snd_hint
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        beh = ttk.Frame(f)
        beh.grid(row=4, column=0, sticky="ew", pady=10)
        beh.columnconfigure(3, weight=1)
        self.countdown = tk.BooleanVar(value=True)
        self.minimise = tk.BooleanVar(value=True)
        ttk.Checkbutton(beh, text="3 second countdown", variable=self.countdown).grid(row=0, column=0)
        ttk.Checkbutton(beh, text="Minimise this window while recording", variable=self.minimise).grid(row=0, column=1, padx=16)
        ttk.Label(beh, text="Save to:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.outdir = tk.StringVar(value=str((lx.videos_dir() if LINUX else Path.home() / "Videos") / "Screen Recordings"))
        ttk.Entry(beh, textvariable=self.outdir).grid(row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(beh, text="Browse", command=self.browse).grid(row=1, column=4, padx=(6, 0), pady=(8, 0))

        go = ttk.Frame(f)
        go.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.btn = ttk.Button(go, text="Start recording (F9)", style="Big.TButton", command=self.toggle)
        self.btn.pack(side="left")
        self.timer = ttk.Label(go, text="", font=("Consolas", 16, "bold"))
        self.timer.pack(side="left", padx=16)

        self.status = ttk.Label(f, text="", wraplength=780, justify="left")
        self.status.grid(row=6, column=0, sticky="w", pady=(12, 6))
        after = ttk.Frame(f)
        after.grid(row=7, column=0, sticky="w")
        self.play_btn = ttk.Button(after, text="Play it", state="disabled", command=lambda: open_path(self.last_file))
        self.folder_btn = ttk.Button(after, text="Open folder", state="disabled", command=lambda: open_path(self.last_file.parent))
        self.edit_btn = ttk.Button(after, text="Edit it", state="disabled", command=self.send_to_editor)
        for b in (self.play_btn, self.folder_btn, self.edit_btn):
            b.pack(side="left", padx=(0, 6))
        self.refresh_devices()

    def browse(self):
        folder = filedialog.askdirectory(title="Choose where recordings are saved")
        if folder:
            self.outdir.set(folder)

    def refresh_devices(self):
        ff = self.app.ffmpeg if hasattr(self.app, "ffmpeg") else ffmpeg_path()
        for var, box, kind in ((self.mic, self.mic_box, "mic"), (self.sys, self.sys_box, "system")):
            if LINUX:
                devices = list_audio_devices(ff, kind)
            else:
                devices = list_audio_devices(ff) if ff else []
            box.config(values=[NONE] + devices)
            if var.get() not in devices:
                var.set(NONE)

    def select_area(self):
        root = self.app.root
        vx, vy, vw, vh = virtual_screen(root)
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.3)
        except tk.TclError:
            pass
        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(vw // 2, 60, fill="white", font=("Segoe UI", 20, "bold"),
                           text="Drag a box over the area to record.  Esc cancels.")
        drag = {}

        def press(e):
            drag.update(x=e.x, y=e.y, rx=e.x_root, ry=e.y_root)
            drag["id"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="white", width=3, fill="#3a8")

        def move(e):
            if "id" in drag:
                canvas.coords(drag["id"], drag["x"], drag["y"], e.x, e.y)

        def release(e):
            if "id" not in drag:
                return
            x, y = min(drag["rx"], e.x_root), min(drag["ry"], e.y_root)
            w, h = even(abs(e.x_root - drag["rx"])), even(abs(e.y_root - drag["ry"]))
            top.destroy()
            if w < 64 or h < 64:
                messagebox.showinfo(APP_NAME, "That area is too small. Drag a bigger box.")
                return
            self.region = (x, y, w, h)
            self.area_mode.set("part")
            self.area_label.config(text=f"{w} x {h} pixels", foreground="")

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", move)
        canvas.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", lambda e: top.destroy())
        top.focus_force()

    def toggle(self):
        if self.proc:
            self.stop()
        elif not self.counting:
            self.start()

    def start(self):
        ff = self.app.need_ffmpeg()
        if not ff:
            return
        region = None
        if self.area_mode.get() == "part":
            if not self.region:
                messagebox.showinfo(APP_NAME, "Click Select area first, then drag a box over what you want to record.")
                return
            region = self.region
        audio = [v.get() for v in (self.mic, self.sys) if v.get() != NONE]
        audio = list(dict.fromkeys(audio))
        try:
            self.raw = unique_path(self.outdir.get(), datetime.now().strftime("Recording %Y-%m-%d %H.%M.%S"), ".mkv")
        except OSError as e:
            messagebox.showwarning(APP_NAME, f"Could not use that save folder: {e}")
            return
        self.counting = True
        self.btn.config(state="disabled")
        for b in (self.play_btn, self.folder_btn, self.edit_btn):
            b.config(state="disabled")
        self.status.config(text="", foreground="")
        if WAYLAND:
            self.start_wayland(audio)
            return
        screen = virtual_screen(self.app.root) if LINUX and not region else None
        cmd = record_command(ff, self.raw, region, int(self.fps.get()), self.quality.get(), audio, self.mouse.get(), screen)
        self.count(3 if self.countdown.get() else 0, cmd)

    def start_wayland(self, audio):
        """Ask the desktop for a screen or window first (it shows its own dialog), then count down."""
        self.timer.config(text="Choose what to share...")
        window = self.area_mode.get() == "window"
        mouse = self.mouse.get()
        cast = lx.ScreenCast()
        audio = [AUDIO_IDS.get(a, a) for a in audio]

        def done(result, error):
            def ui():
                if error:
                    self.counting = False
                    self.failed(str(error))
                    return
                self.cast = result
                cmd = lx.gst_command(result, self.raw, int(self.fps.get()), self.quality.get(), audio)
                self.count(3 if self.countdown.get() else 0, cmd)
            self.app.ui(ui)

        lx.run_in_thread(lambda: cast.open(window=window, cursor=mouse), done)

    def count(self, n, cmd):
        if n > 0:
            self.timer.config(text=f"Starting in {n}")
            self.app.root.after(1000, lambda: self.count(n - 1, cmd))
            return
        self.counting = False
        if self.minimise.get():
            self.app.root.iconify()
            self.app.root.update()
            time.sleep(0.4)  # let the window finish minimising so it is not in the first frames
        self.errfile = tempfile.TemporaryFile()
        extra = {"pass_fds": (self.cast.fd,)} if WAYLAND and getattr(self, "cast", None) else {}
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errfile,
                                         creationflags=NOWIN, **extra)
        except OSError as e:
            self.failed(str(e))
            return
        self.started = time.time()
        self.btn.config(text="Stop recording (F9)", state="normal")
        self.tick()

    def tick(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:  # the recorder stopped by itself
            raw = Path(self.raw)
            if raw.exists() and raw.stat().st_size > 64 * 1024 and time.time() - self.started > 2:
                # e.g. "Stop sharing" was clicked in the desktop's own panel: keep what was recorded
                self.stop()
                return
            self.errfile.seek(0)
            lines = self.errfile.read().decode("utf-8", "replace").strip().splitlines()
            self.proc = None
            Path(self.raw).unlink(missing_ok=True)
            self.failed(lines[-1] if lines else "The recording stopped unexpectedly.")
            return
        self.timer.config(text="REC  " + fmt_time(time.time() - self.started))
        self.app.root.after(250, self.tick)

    def close_cast(self):
        cast, self.cast = getattr(self, "cast", None), None
        if cast:
            cast.close()

    def failed(self, reason):
        self.close_cast()
        self.app.root.deiconify()
        self.timer.config(text="")
        self.btn.config(text="Start recording (F9)", state="normal")
        hint = "  Check the chosen sound device is plugged in and not in use." if "audio" in reason.lower() or "dshow" in reason.lower() else ""
        self.status.config(text=f"Recording could not start: {reason}{hint}", foreground="#b3261e")

    def stop(self, then=None):
        proc, self.proc = self.proc, None
        if not proc:
            return
        self.btn.config(state="disabled")
        self.timer.config(text="Saving...")
        self.app.root.deiconify()
        ff = self.app.ffmpeg
        raw = Path(self.raw)

        gst = WAYLAND

        def finish():
            try:
                if gst:
                    proc.send_signal(signal.SIGINT)  # with -e, GStreamer finishes the file properly
                else:
                    proc.stdin.write(b"q")  # asks FFmpeg to finish the file properly
                    proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait()
            self.app.ui(self.close_cast)
            final, note = raw, ""
            if raw.exists() and raw.stat().st_size > 0:
                mp4 = unique_path(raw.parent, raw.stem, ".mp4")
                r = subprocess.run(remux_command(ff, raw, mp4, audio_to_aac=gst), capture_output=True, creationflags=NOWIN,
                                   stdin=subprocess.DEVNULL)
                if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
                    raw.unlink(missing_ok=True)
                    final = mp4
                else:
                    mp4.unlink(missing_ok=True)
                    note = " (kept as MKV because converting to MP4 failed)"
                self.app.ui(lambda: self.saved(final, note))
            else:
                self.app.ui(lambda: self.failed("nothing was captured."))
            if then:
                self.app.ui(then)

        threading.Thread(target=finish, daemon=True).start()

    def saved(self, path, note):
        self.last_file = path
        self.timer.config(text="")
        self.btn.config(text="Start recording (F9)", state="normal")
        self.status.config(text=f"Saved {path.name}  ({human_size(path.stat().st_size)}){note}", foreground="#1c7548")
        for b in (self.play_btn, self.folder_btn, self.edit_btn):
            b.config(state="normal")

    def send_to_editor(self):
        self.app.nb.select(self.app.edit.frame)
        self.app.edit.load(self.last_file)


class Worker:
    """Shared Export/Stop/progress row for the Edit and Join tabs."""

    def __init__(self, app, parent, row, label):
        self.app = app
        self.stop_flag = threading.Event()
        self.out = None
        bar = ttk.Frame(parent)
        bar.grid(row=row, column=0, sticky="ew", pady=(10, 4))
        bar.columnconfigure(2, weight=1)
        self.btn = ttk.Button(bar, text=label, style="Big.TButton")
        self.btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(bar, text="Stop", state="disabled", command=self.stop_flag.set)
        self.stop_btn.grid(row=0, column=1, padx=6)
        self.bar = ttk.Progressbar(bar, maximum=1.0)
        self.bar.grid(row=0, column=2, sticky="ew")
        self.status = ttk.Label(parent, text="", wraplength=780, justify="left")
        self.status.grid(row=row + 1, column=0, sticky="w")
        after = ttk.Frame(parent)
        after.grid(row=row + 2, column=0, sticky="w", pady=(4, 0))
        self.play_btn = ttk.Button(after, text="Play it", state="disabled", command=lambda: open_path(self.out))
        self.folder_btn = ttk.Button(after, text="Open folder", state="disabled", command=lambda: open_path(self.out.parent))
        self.play_btn.pack(side="left", padx=(0, 6))
        self.folder_btn.pack(side="left")

    def run(self, cmd, total):
        self.stop_flag.clear()
        self.btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.play_btn.config(state="disabled")
        self.folder_btn.config(state="disabled")
        self.bar.config(value=0)
        self.status.config(text="Working...", foreground="")

        def work():
            try:
                out = run_ffmpeg(cmd, total, lambda p: self.app.ui(lambda: self.bar.config(value=p)), self.stop_flag)
                self.app.ui(lambda: self.done(out))
            except InterruptedError:
                self.app.ui(lambda: self.end("Stopped. Nothing was saved.", ""))
            except Exception as e:
                msg = str(e)
                self.app.ui(lambda: self.end(f"That did not work: {msg}", "#b3261e"))

        threading.Thread(target=work, daemon=True).start()

    def end(self, text, colour):
        self.btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.config(text=text, foreground=colour)

    def done(self, out):
        self.out = out
        self.bar.config(value=1.0)
        self.end(f"Saved {out.name}  ({human_size(out.stat().st_size)}) in {out.parent}", "#1c7548")
        self.play_btn.config(state="normal")
        self.folder_btn.config(state="normal")


class EditTab:
    def __init__(self, app):
        self.app = app
        self.src = None
        self.info = None
        self.photo = None
        self.preview_job = 0
        f = self.frame = ttk.Frame(app.nb, padding=14)
        f.columnconfigure(0, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="Open video", command=self.open).pack(side="left")
        self.file_label = ttk.Label(top, text="Open a video to trim, cut, speed up, rotate or resize it.", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        pv = ttk.Frame(f)
        pv.grid(row=1, column=0, sticky="ew", pady=10)
        pv.columnconfigure(1, weight=1)
        self.preview = tk.Label(pv, width=480, height=270, bg="#1b1b1b", fg="#bbb", text="Preview", image="")
        self.blank = tk.PhotoImage(width=480, height=270)
        self.preview.config(image=self.blank, compound="center")
        self.preview.grid(row=0, column=0, rowspan=4)
        side = ttk.Frame(pv)
        side.grid(row=0, column=1, sticky="nw", padx=(14, 0))
        self.pos_label = ttk.Label(side, text="0:00.0", font=("Consolas", 14, "bold"))
        self.pos_label.pack(anchor="w")
        ttk.Label(side, text="Drag the slider to find a moment,\nthen mark it as the start or end.", foreground="#666").pack(anchor="w", pady=(2, 8))
        ttk.Button(side, text="Set start here", width=18, command=lambda: self.start.set(fmt_time(self.pos.get()))).pack(anchor="w", pady=2)
        ttk.Button(side, text="Set end here", width=18, command=lambda: self.end.set(fmt_time(self.pos.get()))).pack(anchor="w", pady=2)
        ttk.Button(side, text="Play in media player", width=18, command=lambda: self.src and open_path(self.src)).pack(anchor="w", pady=(10, 2))
        self.pos = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(f, from_=0, to=1, variable=self.pos, command=self.slid)
        self.slider.grid(row=2, column=0, sticky="ew")
        self.slider.bind("<ButtonRelease-1>", lambda e: self.show_frame())
        self.slider.bind("<KeyRelease>", lambda e: self.show_frame())

        sel = ttk.LabelFrame(f, text="Section", padding=10)
        sel.grid(row=3, column=0, sticky="ew", pady=10)
        self.start = tk.StringVar(value="0:00.0")
        self.end = tk.StringVar(value="0:00.0")
        labelled(sel, "Start", lambda p: ttk.Entry(p, textvariable=self.start, width=12), 0)
        labelled(sel, "End", lambda p: ttk.Entry(p, textvariable=self.end, width=12), 1)
        self.mode = tk.StringVar(value="keep")
        ttk.Radiobutton(sel, text="Keep only this section (trim)", variable=self.mode, value="keep").grid(row=0, column=2, sticky="s", pady=4)
        ttk.Radiobutton(sel, text="Remove this section (cut it out)", variable=self.mode, value="remove").grid(row=0, column=3, sticky="s", padx=14, pady=4)

        opts = ttk.LabelFrame(f, text="Changes", padding=10)
        opts.grid(row=4, column=0, sticky="ew")
        self.speed, _ = combo(opts, "Speed", SPEEDS, "1x", 0, width=8)
        self.rotate, _ = combo(opts, "Rotate", list(ROTATE), "None", 1, width=16)
        self.volume, _ = combo(opts, "Sound", list(VOLUME), "Keep", 2, width=14)
        self.size, _ = combo(opts, "Picture size", list(SIZES), "Original", 3, width=10)
        self.fmt, _ = combo(opts, "Save as", ["MP4", "GIF"], "MP4", 0, row=1, width=8)
        self.quality, _ = combo(opts, "Quality", list(CRF), "Balanced", 1, row=1, width=12)

        self.worker = Worker(app, f, 5, "Save edited video")
        self.worker.btn.config(command=self.export)

    def open(self):
        path = filedialog.askopenfilename(title="Open a video", filetypes=[("Videos", " ".join("*" + e for e in VIDEO_EXT)), ("All files", "*.*")])
        if path:
            self.load(Path(path))

    def load(self, path):
        ff = self.app.need_ffmpeg()
        if not ff:
            return
        try:
            info = probe(ff, path)
        except Exception as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        if info["duration"] <= 0:
            messagebox.showwarning(APP_NAME, "The length of that video could not be read, so it cannot be edited here.")
            return
        self.src, self.info = Path(path), info
        self.file_label.config(foreground="", text=f"{self.src.name}   {fmt_time(info['duration'])} long, {info['width']} x {info['height']}"
                                                   f"{'' if info['audio'] else ', no sound'}")
        self.slider.config(to=info["duration"])
        self.pos.set(0.0)
        self.start.set("0:00.0")
        self.end.set(fmt_time(info["duration"]))
        self.pos_label.config(text="0:00.0")
        self.show_frame()

    def slid(self, _=None):
        self.pos_label.config(text=fmt_time(self.pos.get()))

    def show_frame(self):
        if not self.src:
            return
        self.preview_job += 1
        job, src, at, ff = self.preview_job, self.src, self.pos.get(), self.app.ffmpeg
        at = min(at, max(0.0, self.info["duration"] - 0.1))

        def work():
            png = frame_png(ff, src, at)
            if png:
                self.app.ui(lambda: self.set_frame(job, png))

        threading.Thread(target=work, daemon=True).start()

    def set_frame(self, job, png):
        if job != self.preview_job:  # a newer frame was asked for
            return
        try:
            self.photo = tk.PhotoImage(data=base64.b64encode(png))
            self.preview.config(image=self.photo, text="")
        except tk.TclError:
            pass

    def export(self):
        ff = self.app.need_ffmpeg()
        if not ff:
            return
        if not self.src:
            messagebox.showinfo(APP_NAME, "Open a video first.")
            return
        try:
            gif = self.fmt.get() == "GIF"
            out = unique_path(self.src.parent, f"{self.src.stem} edited", ".gif" if gif else ".mp4")
            cmd, total = edit_command(ff, self.src, out, self.info, parse_time(self.start.get()), parse_time(self.end.get()),
                                      self.mode.get(), float(self.speed.get().rstrip("x")), self.rotate.get(),
                                      self.volume.get(), self.size.get(), self.fmt.get(), self.quality.get())
        except (ValueError, OSError) as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        self.worker.run(cmd, total)


class JoinTab:
    def __init__(self, app):
        self.app = app
        self.clips = []
        f = self.frame = ttk.Frame(app.nb, padding=14)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, wraplength=780, justify="left",
                  text="Join several clips into one video, in the order shown. Every clip is fitted to the picture size of the first one."
                  ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        box = ttk.Frame(f)
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(box, selectmode="browse", activestyle="none", height=10)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        side = ttk.Frame(box)
        side.grid(row=0, column=1, sticky="n", padx=(8, 0))
        for text, cmd in (("Add clips", self.add), ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                          ("Remove", self.remove), ("Clear", self.clear)):
            ttk.Button(side, text=text, width=12, command=cmd).pack(pady=2)
        opts = ttk.Frame(f)
        opts.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.quality, _ = combo(opts, "Quality", list(CRF), "Balanced", 0, width=12)
        self.worker = Worker(app, f, 3, "Join clips")
        self.worker.btn.config(command=self.export)

    def add(self):
        ff = self.app.need_ffmpeg()
        if not ff:
            return
        paths = filedialog.askopenfilenames(title="Add clips", filetypes=[("Videos", " ".join("*" + e for e in VIDEO_EXT)), ("All files", "*.*")])
        bad = []
        for p in paths:
            try:
                self.clips.append((Path(p), probe(ff, p)))
            except Exception:
                bad.append(Path(p).name)
        self.refresh()
        if bad:
            messagebox.showwarning(APP_NAME, "These could not be read as video: " + ", ".join(bad))

    def refresh(self, select=None):
        self.listbox.delete(0, "end")
        for path, info in self.clips:
            self.listbox.insert("end", f"{path.name}    {fmt_time(info['duration'])}, {info['width']} x {info['height']}")
        if select is not None:
            self.listbox.selection_set(select)

    def move(self, d):
        sel = self.listbox.curselection()
        if not sel or not 0 <= sel[0] + d < len(self.clips):
            return
        i = sel[0]
        self.clips[i], self.clips[i + d] = self.clips[i + d], self.clips[i]
        self.refresh(i + d)

    def remove(self):
        sel = self.listbox.curselection()
        if sel:
            del self.clips[sel[0]]
            self.refresh()

    def clear(self):
        self.clips.clear()
        self.refresh()

    def export(self):
        ff = self.app.need_ffmpeg()
        if not ff:
            return
        if len(self.clips) < 2:
            messagebox.showinfo(APP_NAME, "Add at least two clips to join.")
            return
        try:
            out = unique_path(self.clips[0][0].parent, "Joined video", ".mp4")
        except OSError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        cmd, total = join_command(ff, self.clips, out, self.quality.get())
        self.worker.run(cmd, total)


def main():
    if WINDOWS:  # so the area you drag matches real screen pixels on scaled displays
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    root = tk.Tk(className="screen-recorder") if LINUX else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log = Path(__file__).with_name("screen-recorder-error.log")
        log.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror(APP_NAME, f"The app hit a problem and had to close.\nDetails were saved to:\n{log}")
        except Exception:
            pass
