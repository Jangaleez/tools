"""Screen Recorder & Editor - record the screen, then trim, cut, speed up, rotate or join videos.

Everything runs on this PC using FFmpeg. Start it with "Run Screen Recorder.bat".
"""
import base64
import bisect
import math
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
import wave
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
LOOPBACK = "What this PC is playing (no setup needed)"


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


def record_command(ffmpeg, out, region=None, fps=30, quality="Balanced", audio=(), mouse=True, screen=None, detailed=False):
    """region is None for the whole desktop, or (x, y, width, height).
    detailed keeps FFmpeg's start-up report, which says exactly when the first frame was captured."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info" if detailed else "error", "-nostats", "-y"]
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
    # a keyframe every 2 seconds keeps jumping around the video quick when editing
    cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", CRF[quality], "-g", str(int(fps) * 2), "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    return cmd + [str(out)]


def remux_command(ffmpeg, src, dst, audio_to_aac=False):
    if audio_to_aac:  # Wayland recordings use Opus sound; AAC plays everywhere
        return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c:v", "copy", "-c:a", "aac",
                "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c", "copy",
            "-movflags", "+faststart", str(dst)]


# ---- computer sound on Windows, straight from the speakers (no Stereo Mix needed) ----

def clocks():
    """The same moment on the three clocks FFmpeg might use, so recordings can be lined up afterwards."""
    return (time.time(), time.monotonic(), time.perf_counter())


class TimedWav:
    """Writes sound to a WAV file and fills any gaps with silence, so it stays in step with the video.

    Windows sends nothing at all while the PC is silent, so without this the sound would drift ahead."""

    def __init__(self, path, rate, channels, sampwidth=2):
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(channels)
        self.wav.setsampwidth(sampwidth)
        self.wav.setframerate(rate)
        self.rate, self.bpf = rate, channels * sampwidth
        self.start = None  # clocks() at the first sample
        self.frames = 0

    def add(self, now, data):
        n = len(data) // self.bpf
        if not n:
            return
        if self.start is None:
            self.start = tuple(c - n / self.rate for c in now)
        else:
            gap = (now[2] - self.start[2]) - (self.frames + n) / self.rate
            if gap > 0.25:
                self.silence(gap)
        self.wav.writeframes(data[: n * self.bpf])
        self.frames += n

    def silence(self, secs):
        left = int(secs * self.rate)
        self.frames += left
        block = bytes(self.bpf * self.rate // 10)
        while left > 0:
            take = min(left, self.rate // 10)
            self.wav.writeframes(block[: take * self.bpf])
            left -= take

    def close(self):
        self.wav.close()


class LoopbackRecorder:
    """Records whatever the default speakers or headphones are playing, using Windows' WASAPI loopback."""

    def __init__(self, path):
        self.path = Path(path)
        self.q = queue.Queue()
        self.pa = self.stream = self.out = self.thread = self.writer = None

    @staticmethod
    def available():
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except Exception:
            return False

    def start(self):
        import pyaudiowpatch as pa
        self.mod = pa
        self.pa = pa.PyAudio()
        try:
            wasapi = self.pa.get_host_api_info_by_type(pa.paWASAPI)
            speakers = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loop = speakers if speakers.get("isLoopbackDevice") else next(
                (d for d in self.pa.get_loopback_device_info_generator() if speakers["name"] in d["name"]), None)
            if loop is None:
                raise RuntimeError(f'Windows did not offer a way to record "{speakers["name"]}"')
            rate, channels = int(loop["defaultSampleRate"]), max(1, int(loop["maxInputChannels"]))
            self.writer = TimedWav(self.path, rate, channels)
            self.thread = threading.Thread(target=self.write_loop, daemon=True)
            self.thread.start()
            self.stream = self.pa.open(format=pa.paInt16, channels=channels, rate=rate, input=True,
                                       input_device_index=loop["index"], frames_per_buffer=1024, stream_callback=self.on_sound)
            self.keep_awake(speakers)
        except Exception:
            self.stop()
            raise

    def on_sound(self, data, frames, info, status):
        self.q.put((clocks(), data))
        return (None, self.mod.paContinue)

    def write_loop(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            self.writer.add(*item)

    def keep_awake(self, speakers):
        """Play silence so Windows keeps sending sound even when nothing else is playing (gaps are filled anyway)."""
        if speakers.get("isLoopbackDevice"):
            return
        try:
            channels = max(1, int(speakers["maxOutputChannels"]))
            pa = self.mod
            self.out = self.pa.open(format=pa.paInt16, channels=channels, rate=int(speakers["defaultSampleRate"]), output=True,
                                    output_device_index=speakers["index"], frames_per_buffer=1024,
                                    stream_callback=lambda d, n, i, st: (bytes(n * channels * 2), pa.paContinue))
        except Exception:
            self.out = None

    def stop(self):
        """Stops recording and returns clocks() at the first sound sample, or None if nothing was captured."""
        for stream in (self.stream, self.out):
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self.stream = self.out = None
        if self.thread:
            self.q.put(None)
            self.thread.join(10)
            self.thread = None
        start = None
        if self.writer:
            start = self.writer.start
            try:
                self.writer.close()
            except Exception:
                start = None
            self.writer = None
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None
        return start


def video_start(log):
    """When the first video frame was captured, from FFmpeg's start-up report."""
    m = re.search(r"Input #0.*?start: (-?\d+(?:\.\d+)?)", log, re.S)
    return float(m.group(1)) if m else None


def sound_offset(first_frame, launched, sound_start):
    """Seconds the computer sound starts after the first video frame (negative means before).

    FFmpeg stamps frames with one of several clocks; the right one is whichever puts the first
    frame just after the moment FFmpeg was launched."""
    if first_frame is not None:
        for k in range(3):
            if -1.0 < first_frame - launched[k] < 20.0:
                return sound_start[k] - first_frame
    return sound_start[0] - launched[0]


def add_sound_command(ffmpeg, video, wav, dst, offset, has_sound):
    """Mix the computer sound into a finished recording, lined up by offset seconds. The picture is copied as it is."""
    if offset >= 0:
        align = f"adelay=delays={int(round(offset * 1000))}:all=1"
    else:
        align = f"atrim=start={-offset:.3f},asetpts=PTS-STARTPTS"
    if has_sound:  # a microphone was recorded too: mix the two
        graph = f"[1:a]{align}[pc];[0:a][pc]amix=inputs=2:duration=first:dropout_transition=0,apad[a]"
    else:
        graph = f"[1:a]{align},apad[a]"
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(wav), "-filter_complex", graph,
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-shortest",
            "-movflags", "+faststart", str(dst)]


def worst_line(lines):
    """The most useful line from FFmpeg's messages to show when something goes wrong."""
    for line in reversed(lines):
        if re.search(r"error|could not|cannot|failed|invalid|denied|not found|no such", line, re.I):
            return line.strip()
    return lines[-1].strip() if lines else ""


ROTATE = {"None": [], "90 degrees right": ["transpose=1"], "90 degrees left": ["transpose=2"],
          "Upside down": ["hflip", "vflip"], "Mirror": ["hflip"]}
VOLUME = {"Keep": 1.0, "Mute": 0.0, "Quieter (50%)": 0.5, "Louder (150%)": 1.5, "Louder (200%)": 2.0}
SIZES = {"Original": 0, "1080p": 1080, "720p": 720, "480p": 480}
SPEEDS = ["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"]


def normalise(cuts, dur, min_len=0.05):
    """Sort the parts marked for deletion, keep them inside the video, and merge any that touch or overlap."""
    out = []
    for s, e in sorted((max(0.0, min(a, b)), min(dur, max(a, b))) for a, b in cuts):
        if e - s < min_len:
            continue
        if out and s <= out[-1][1] + 0.01:
            out[-1] = [out[-1][0], max(out[-1][1], e)]
        else:
            out.append([s, e])
    return out


def keep_segments(cuts, dur):
    """The parts of the video that are left once the marked parts are deleted, as (start, end) pairs."""
    keep, pos = [], 0.0
    for s, e in normalise(cuts, dur):
        if s - pos >= 0.05:
            keep.append((pos, s))
        pos = max(pos, e)
    if dur - pos >= 0.05:
        keep.append((pos, dur))
    return keep


def edit_command(ffmpeg, src, dst, info, cuts=(), speed=1.0, rotate="None", volume="Keep", size="Original",
                 fmt="MP4", quality="Balanced"):
    """Returns (command, length of the result in seconds). cuts are the (start, end) parts to delete."""
    dur = info["duration"]
    keep = keep_segments(cuts, dur)
    if not keep:
        raise ValueError("That would delete the whole video. Leave at least part of it unmarked.")

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
    if len(keep) == 1:  # one piece left: jump straight to it, which is much faster
        s, e = keep[0]
        if s > 0.001:
            cmd += ["-ss", f"{s:.3f}"]
        if e < dur - 0.001:
            cmd += ["-to", f"{e:.3f}"]
        cmd += ["-i", str(src)]
        graph = f"[0:v]{','.join(vf)}[v]"
        if sound:
            graph += f";[0:a]{','.join(af) or 'anull'}[a]"
    else:  # several pieces: cut each one out and join them back together
        cmd += ["-i", str(src)]
        parts, labels = [], ""
        for i, (s, e) in enumerate(keep):
            parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
            labels += f"[v{i}]"
            if sound:
                parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
                labels += f"[a{i}]"
        parts.append(f"{labels}concat=n={len(keep)}:v=1:a={1 if sound else 0}[vj]{'[aj]' if sound else ''}")
        parts.append(f"[vj]{','.join(vf)}[v]")
        if sound:
            parts.append(f"[aj]{','.join(af) or 'anull'}[a]")
        graph = ";".join(parts)
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if sound:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    if not gif:
        cmd += ["-c:v", "libx264", "-crf", CRF[quality], "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    return cmd + [str(dst)], sum(e - s for s, e in keep) / speed


def ffplay_path(ffmpeg):
    """FFplay comes with FFmpeg and plays a video in its own window."""
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffplay.exe" if WINDOWS else "ffplay")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffplay")


def cut_filters(cuts, dur):
    """FFplay filters that skip the marked parts while playing, so you can check an edit before saving it."""
    cuts = normalise(cuts, dur)
    keep = keep_segments(cuts, dur)
    if not keep:
        return None
    if not cuts:
        return "null", "anull"
    inside = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep)
    # move every later frame back by the length of the parts deleted before it, so the playback has no gaps
    removed = "+".join(f"gte(T,{e:.3f})*{e - s:.3f}" for s, e in cuts)
    shift = f"(T-({removed}))/TB"
    return f"select='{inside}',setpts='{shift}'", f"aselect='{inside}',asetpts='{shift}'"


def preview_command(ffplay, src, info, cuts, start=0.0):
    filters = cut_filters(cuts, info["duration"])
    if filters is None:
        raise ValueError("Everything is marked for deletion, so there is nothing to play.")
    vf, af = filters
    cmd = [ffplay, "-hide_banner", "-loglevel", "error", "-autoexit", "-window_title", "Preview with cuts  (Space pauses, Esc closes)"]
    if start > 0.05:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-vf", vf]
    if info["audio"]:
        cmd += ["-af", af]
    return cmd + ["-i", str(src)]


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
        w = min(980, root.winfo_screenwidth() - 40)
        h = min(900, root.winfo_screenheight() - 90)
        root.geometry(f"{w}x{h}")
        root.minsize(min(800, w), min(640, h))
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
        snd_hint = ('"What this PC is playing" records the sound from your speakers or headphones, whatever is playing. '
                    "Pick None to record without it.")
        if WINDOWS and not LoopbackRecorder.available():
            snd_hint = ("To record computer sound, close this app and run Run Screen Recorder.bat again: it installs the part "
                        "that's needed (PyAudioWPatch). Until then, only a microphone or Stereo Mix can be recorded.")
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
            extra = [LOOPBACK] if kind == "system" and WINDOWS and LoopbackRecorder.available() else []
            box.config(values=[NONE] + extra + devices)
            if var.get() not in extra + devices:
                var.set(NONE)
            if extra and not getattr(self, "sys_defaulted", False):
                self.sys_defaulted = True  # record computer sound unless the user turns it off
                var.set(LOOPBACK)

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
        self.loop_wanted = self.sys.get() == LOOPBACK
        audio = [v.get() for v in (self.mic, self.sys) if v.get() not in (NONE, LOOPBACK)]
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
        cmd = record_command(ff, self.raw, region, int(self.fps.get()), self.quality.get(), audio, self.mouse.get(), screen,
                             detailed=self.loop_wanted)
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
        self.loop, self.loop_error = None, ""
        if getattr(self, "loop_wanted", False) and not WAYLAND:
            self.loop = LoopbackRecorder(Path(self.raw).with_suffix(".computer-sound.wav"))
            try:
                self.loop.start()
            except Exception as e:  # carry on without computer sound rather than not recording at all
                self.loop, self.loop_error = None, str(e) or type(e).__name__
        self.launched = clocks()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errfile,
                                         creationflags=NOWIN, **extra)
        except OSError as e:
            self.drop_loop()
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
            self.drop_loop()
            Path(self.raw).unlink(missing_ok=True)
            self.failed(worst_line(lines) or "The recording stopped unexpectedly.")
            return
        self.timer.config(text="REC  " + fmt_time(time.time() - self.started))
        self.app.root.after(250, self.tick)

    def drop_loop(self):
        loop, self.loop = getattr(self, "loop", None), None
        if loop:
            loop.stop()
            loop.path.unlink(missing_ok=True)

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
        loop, self.loop = getattr(self, "loop", None), None
        loop_error, errfile, launched = getattr(self, "loop_error", ""), self.errfile, getattr(self, "launched", None)

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
            sound_start = loop.stop() if loop else None
            final, note = raw, ""
            if loop_error:
                note = f" without computer sound ({loop_error})"
            if raw.exists() and raw.stat().st_size > 0:
                mp4 = unique_path(raw.parent, raw.stem, ".mp4")
                r = None
                if sound_start and loop.path.exists():
                    try:
                        errfile.seek(0)
                        offset = sound_offset(video_start(errfile.read().decode("utf-8", "replace")), launched, sound_start)
                        has_sound = probe(ff, raw)["audio"]
                        r = subprocess.run(add_sound_command(ff, raw, loop.path, mp4, offset, has_sound), capture_output=True,
                                           creationflags=NOWIN, stdin=subprocess.DEVNULL)
                    except Exception:
                        r = None
                    if not (r and r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0):
                        r, note = None, " without computer sound (it could not be mixed in)"
                elif loop and not loop_error:
                    note = " (no computer sound was playing)"
                if r is None:
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
            if loop:
                loop.path.unlink(missing_ok=True)
            if then:
                self.app.ui(then)

        threading.Thread(target=finish, daemon=True).start()

    def saved(self, path, note):
        self.last_file = path
        self.timer.config(text="")
        self.btn.config(text="Start recording (F9)", state="normal")
        colour = "#b26a00" if "without" in note else "#1c7548"
        self.status.config(text=f"Saved {path.name}  ({human_size(path.stat().st_size)}){note}", foreground=colour)
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
    """Video editor with a filmstrip: drag across it to mark parts to delete, hover to see any moment."""
    M = 12           # space at each end of the filmstrip so the end handles are easy to grab
    TOP = 8          # gap above the filmstrip (the playhead pokes into it)
    STRIP = 54       # filmstrip height in pixels
    RULER = 20       # time ruler under the filmstrip
    HANDLE = 10      # width of the grab handles on a marked part
    THUMB = (192, 108)  # hover snippet size; filmstrip tiles are half this
    BG, PLACEHOLDER, CUT, CUT_EDGE, PICKED, RULER_TEXT = "#1d1d21", "#34343b", "#e5484d", "#b3262b", "#ffcc33", "#a0a0aa"

    def __init__(self, app):
        self.app = app
        self.src = None
        self.info = None
        self.dur = 0.0
        self.gen = 0                 # goes up every time a video is opened, so old background work is ignored
        self.cuts = []               # [start, end] pairs marked for deletion
        self.sel = None              # index of the selected part
        self.history = []
        self.playhead = 0.0
        self.v0, self.v1 = 0.0, 1.0  # the stretch of time the filmstrip is showing
        self.hover = None
        self.drag = None
        self.thumbs = {}             # time -> (snippet image, filmstrip tile image)
        self.thumb_times = []        # the same times, sorted
        self.zoom_added = []         # extra tiles made while zoomed in, oldest first
        self.lock = threading.Lock()
        self.pending, self.pending_zoom = [], []
        self.exact_cache = {}
        self.shown = None            # (time, box) the big preview is showing or waiting for
        self.preview_img = None
        self.rough, self.rough_img = None, None
        self.jobs = {}
        self.player = None
        self.exact_req = None
        self.exact_event = threading.Event()
        threading.Thread(target=self.exact_loop, daemon=True).start()

        f = self.frame = ttk.Frame(app.nb, padding=(14, 10))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="Open video", command=self.open).pack(side="left")
        self.file_label = ttk.Label(top, text="Open a video, or record one and click Edit it.", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        self.pv = tk.Frame(f, bg="#000000", width=640, height=300)
        self.pv.grid(row=1, column=0, sticky="nsew", pady=(8, 4))
        self.pv.grid_propagate(False)
        self.pv.pack_propagate(False)
        self.pv_label = tk.Label(self.pv, bg="#000000", fg="#888", text="Preview")
        self.pv_label.pack(fill="both", expand=True)
        self.pv.bind("<Configure>", lambda e: self.later("pv", 200, self.refresh_preview))

        info = ttk.Frame(f)
        info.grid(row=2, column=0, sticky="ew")
        self.time_label = ttk.Label(info, text="", font=("Consolas", 11, "bold"))
        self.time_label.pack(side="left")
        self.state_label = ttk.Label(info, text="", foreground="#b3261e")
        self.state_label.pack(side="left", padx=10)

        self.c = tk.Canvas(f, height=self.TOP + self.STRIP + self.RULER + 4, bg=self.BG, highlightthickness=1,
                           highlightbackground=self.BG, highlightcolor=self.PICKED, takefocus=1, cursor="crosshair")
        self.c.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.scroll = ttk.Scrollbar(f, orient="horizontal", command=self.on_scroll)
        self.scroll.grid(row=4, column=0, sticky="ew")

        tools = ttk.Frame(f)
        tools.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.play_btn = ttk.Button(tools, text="▶  Preview with cuts", command=self.play_preview)
        self.play_btn.pack(side="left")
        ttk.Button(tools, text="Undo", width=6, command=self.undo).pack(side="left", padx=(8, 0))
        self.remove_btn = ttk.Button(tools, text="Unmark selected", command=self.remove_selected, state="disabled")
        self.remove_btn.pack(side="left", padx=(4, 0))
        ttk.Button(tools, text="Clear all", command=self.clear_cuts).pack(side="left", padx=(4, 0))
        ttk.Button(tools, text="+", width=3, command=lambda: self.zoom(0.5)).pack(side="right")
        ttk.Button(tools, text="Fit", width=4, command=self.fit).pack(side="right", padx=2)
        ttk.Button(tools, text="−", width=3, command=lambda: self.zoom(2)).pack(side="right")
        ttk.Label(tools, text="Zoom").pack(side="right", padx=(0, 6))

        self.summary = ttk.Label(f, text="")
        self.summary.grid(row=6, column=0, sticky="w", pady=(6, 0))
        selrow = ttk.Frame(f)
        selrow.grid(row=7, column=0, sticky="w", pady=(2, 0))
        ttk.Label(selrow, text="Selected part:").pack(side="left")
        self.sel_start, self.sel_end = tk.StringVar(), tk.StringVar()
        self.sel_entries = []
        for label, var in (("from", self.sel_start), ("to", self.sel_end)):
            ttk.Label(selrow, text=label).pack(side="left", padx=(8, 4))
            ent = ttk.Entry(selrow, textvariable=var, width=10, state="disabled")
            ent.pack(side="left")
            ent.bind("<Return>", self.apply_entries)
            ent.bind("<FocusOut>", self.apply_entries)
            self.sel_entries.append(ent)
        self.sel_len = ttk.Label(selrow, text="", foreground="#666")
        self.sel_len.pack(side="left", padx=8)

        opts = ttk.LabelFrame(f, text="Changes", padding=(10, 4))
        opts.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.speed, _ = combo(opts, "Speed", SPEEDS, "1x", 0, width=6)
        self.rotate, _ = combo(opts, "Rotate", list(ROTATE), "None", 1, width=15)
        self.volume, _ = combo(opts, "Sound", list(VOLUME), "Keep", 2, width=13)
        self.size, _ = combo(opts, "Picture size", list(SIZES), "Original", 3, width=8)
        self.fmt, _ = combo(opts, "Save as", ["MP4", "GIF"], "MP4", 4, width=5)
        self.quality, _ = combo(opts, "Quality", list(CRF), "Balanced", 5, width=10)
        self.speed.trace_add("write", lambda *a: self.update_summary())

        self.worker = Worker(app, f, 9, "Save edited video")
        self.worker.btn.config(command=self.export)

        self.bubble = tk.Toplevel(app.root)
        self.bubble.withdraw()
        self.bubble.overrideredirect(True)
        try:
            self.bubble.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.bubble_box = tk.Frame(self.bubble, bg="#111", padx=3, pady=3)
        self.bubble_box.pack()
        self.bubble_blank = tk.PhotoImage(width=self.THUMB[0], height=self.THUMB[1])
        self.bubble_img = tk.Label(self.bubble_box, bg="#111", image=self.bubble_blank, compound="center")
        self.bubble_img.pack()
        self.bubble_text = tk.Label(self.bubble_box, bg="#111", fg="#fff", font=("Segoe UI", 9, "bold"))
        self.bubble_text.pack(fill="x")

        c = self.c
        c.bind("<Configure>", lambda e: (self.redraw(strip=True), self.later("zoomthumbs", 250, self.ask_zoom_thumbs)))
        c.bind("<Motion>", self.on_motion)
        c.bind("<Leave>", self.on_leave)
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.on_right_click)
        c.bind("<MouseWheel>", lambda e: self.on_wheel(e, -1 if e.delta > 0 else 1))
        c.bind("<Shift-MouseWheel>", lambda e: self.pan(-0.15 if e.delta > 0 else 0.15))
        c.bind("<Button-4>", lambda e: self.on_wheel(e, -1))
        c.bind("<Button-5>", lambda e: self.on_wheel(e, 1))
        c.bind("<Shift-Button-4>", lambda e: self.pan(-0.15))
        c.bind("<Shift-Button-5>", lambda e: self.pan(0.15))
        for key in ("<Delete>", "<BackSpace>"):
            c.bind(key, lambda e: self.remove_selected())
        c.bind("<Control-z>", lambda e: self.undo())
        c.bind("<space>", lambda e: self.play_preview())
        c.bind("<Left>", lambda e: self.nudge(-0.1))
        c.bind("<Right>", lambda e: self.nudge(0.1))
        c.bind("<Shift-Left>", lambda e: self.nudge(-1))
        c.bind("<Shift-Right>", lambda e: self.nudge(1))
        self.update_summary()

    # ------------------------------------------------------------ helpers
    def later(self, name, ms, fn):
        """Run fn once after ms, replacing any earlier request with the same name."""
        if name in self.jobs:
            self.frame.after_cancel(self.jobs[name])
        self.jobs[name] = self.frame.after(ms, lambda: (self.jobs.pop(name, None), fn()))

    def width(self):
        return max(self.c.winfo_width(), 100)

    def x_of(self, t):
        return self.M + (t - self.v0) / (self.v1 - self.v0) * (self.width() - 2 * self.M)

    def t_of(self, x):
        t = self.v0 + (x - self.M) / (self.width() - 2 * self.M) * (self.v1 - self.v0)
        return min(max(t, 0.0), self.dur)

    def in_cut(self, t):
        return any(s <= t <= e for s, e in self.cuts)

    def nearest_thumb(self, t):
        times = self.thumb_times
        if not times:
            return None
        i = bisect.bisect_left(times, t)
        best = min((times[j] for j in (i - 1, i) if 0 <= j < len(times)), key=lambda x: abs(x - t))
        return self.thumbs[best]

    # ------------------------------------------------------------ opening a video
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
        self.gen += 1
        self.src, self.info, self.dur = Path(path), info, info["duration"]
        self.file_label.config(foreground="", text=f"{self.src.name}   {fmt_time(self.dur)} long, {info['width']} x {info['height']}"
                                                   f"{'' if info['audio'] else ', no sound'}")
        self.cuts, self.sel, self.history = [], None, []
        self.playhead, self.v0, self.v1 = 0.0, 0.0, self.dur
        self.thumbs, self.thumb_times, self.zoom_added, self.exact_cache = {}, [], [], {}
        count = int(min(240, max(40, self.dur)))
        step = self.dur / count
        times = [(i + 0.5) * step for i in range(count)]
        order, seen, stride = [], set(), 1 << (count.bit_length() - 1)
        while stride >= 1:  # rough frames across the whole video first, then fill in the gaps
            for i in range(0, count, stride):
                if i not in seen:
                    seen.add(i)
                    order.append(times[i])
            stride //= 2
        with self.lock:
            self.pending, self.pending_zoom = order, []
        for _ in range(3):
            threading.Thread(target=self.thumb_loop, args=(self.gen, ff, self.src), daemon=True).start()
        self.update_selection()
        self.update_summary()
        self.redraw(strip=True)
        self.show_time(0.0)
        self.c.focus_set()

    # ------------------------------------------------------------ background frame grabbing
    def thumb_loop(self, gen, ff, src):
        while gen == self.gen:
            with self.lock:
                if self.pending_zoom:
                    t, zoomed = self.pending_zoom.pop(0), True
                elif self.pending:
                    t, zoomed = self.pending.pop(0), False
                else:
                    return
            png = frame_png(ff, src, min(t, self.dur - 0.05), self.THUMB)
            if png:
                self.app.ui(lambda t=t, png=png, z=zoomed: self.thumb_ready(gen, t, png, z))

    def thumb_ready(self, gen, t, png, zoomed=False):
        if gen != self.gen or t in self.thumbs:
            return
        try:
            big = tk.PhotoImage(data=base64.b64encode(png))
        except tk.TclError:
            return
        self.thumbs[t] = (big, big.subsample(2))
        bisect.insort(self.thumb_times, t)
        if zoomed:
            self.zoom_added.append(t)
            while len(self.zoom_added) > 400:  # keep memory in check after lots of zooming
                old = self.zoom_added.pop(0)
                self.thumbs.pop(old, None)
                self.thumb_times.remove(old)
        self.later("strip", 120, lambda: self.redraw(strip=True))
        if self.preview_img is None and self.shown:  # nothing to show yet: use this first frame
            self.show_time(self.shown[0])

    def ask_zoom_thumbs(self):
        """When zoomed in, ask for frames that match each filmstrip tile so the strip stays accurate."""
        if not self.src or not self.thumb_times:
            return
        tile_w = self.tile_size()[0]
        span = self.v1 - self.v0
        n = int((self.width() - 2 * self.M) / tile_w) + 1
        per_tile = span * tile_w / (self.width() - 2 * self.M)
        wanted = []
        for k in range(n):
            t = self.v0 + (k + 0.5) * per_tile
            if t > self.dur:
                break
            i = bisect.bisect_left(self.thumb_times, t)
            near = min((abs(self.thumb_times[j] - t) for j in (i - 1, i) if 0 <= j < len(self.thumb_times)), default=1e9)
            if near > per_tile * 0.6:
                wanted.append(round(t, 3))
        with self.lock:
            self.pending_zoom = wanted

    def exact_loop(self):
        while True:
            self.exact_event.wait()
            self.exact_event.clear()
            req = self.exact_req
            if not req:
                continue
            gen, ff, src, t, box, dur = req
            png = frame_png(ff, src, max(0.0, min(t, dur - 0.1)), box)
            if not png and t > dur - 2:
                png = frame_png(ff, src, max(0.0, dur - 1.0), box)
            if png:
                self.app.ui(lambda gen=gen, t=t, box=box, png=png: self.exact_ready(gen, t, box, png))

    def exact_ready(self, gen, t, box, png):
        if gen != self.gen:
            return
        try:
            img = tk.PhotoImage(data=base64.b64encode(png))
        except tk.TclError:
            return
        self.exact_cache[(round(t, 2), box)] = img
        while len(self.exact_cache) > 12:
            self.exact_cache.pop(next(iter(self.exact_cache)))
        if self.shown == (t, box):
            self.set_preview(img)

    # ------------------------------------------------------------ big preview
    def preview_box(self):
        w, h = self.pv.winfo_width(), self.pv.winfo_height()
        if w < 80 or h < 60:
            return (640, 300)
        return (w // 2 * 2, h // 2 * 2)

    def set_preview(self, img):
        self.preview_img = img
        self.pv_label.config(image=img, text="")

    def show_time(self, t):
        """Show the moment t in the big preview: a quick rough frame now, the sharp frame a moment later."""
        if not self.src:
            return
        box = self.preview_box()
        self.shown = (t, box)
        self.time_label.config(text=f"{fmt_time(t)} / {fmt_time(self.dur)}")
        self.state_label.config(text="This moment is in a part marked for deletion" if self.in_cut(t) else "")
        sharp = self.exact_cache.get((round(t, 2), box))
        if sharp:
            self.set_preview(sharp)
            return
        thumb = self.nearest_thumb(t)
        if thumb:
            factor = min(box[0] // self.THUMB[0], box[1] // self.THUMB[1], 4)
            if self.rough != (thumb[0], factor) or self.preview_img is not self.rough_img:
                self.rough = (thumb[0], factor)  # only re-scale when the rough frame actually changes
                self.rough_img = thumb[0].zoom(factor) if factor >= 2 else thumb[0]
                self.set_preview(self.rough_img)
        else:
            self.preview_img = None
        self.later("exact", 110, lambda: self.request_exact(t, box))

    def request_exact(self, t, box):
        if self.shown == (t, box):
            self.exact_req = (self.gen, self.app.ffmpeg, self.src, t, box, self.dur)
            self.exact_event.set()

    def refresh_preview(self):
        if self.src:
            self.show_time(self.hover if self.hover is not None else self.playhead)

    # ------------------------------------------------------------ drawing
    def tile_size(self):
        w, h = self.info["width"] or 16, self.info["height"] or 9
        k = min(self.THUMB[0] / w, self.THUMB[1] / h)
        return max(1, round(w * k) // 2), max(1, round(h * k) // 2)

    def redraw(self, strip=False):
        c = self.c
        W = self.width()
        y0, y1 = self.TOP, self.TOP + self.STRIP
        if not self.src:
            c.delete("all")
            c.create_text(W // 2, (y0 + y1) // 2, fill=self.RULER_TEXT, font=("Segoe UI", 10),
                          text="Open a video to see its filmstrip here")
            return
        if strip:
            c.delete("strip", "ruler", "blank")
            tw, th = self.tile_size()
            span = self.v1 - self.v0
            inner = W - 2 * self.M
            x = self.M
            while x < W - self.M:
                t = self.v0 + (x - self.M + tw / 2) / inner * span
                c.create_rectangle(x, y0, x + tw, y1, fill=self.PLACEHOLDER, outline="", tags="strip")
                thumb = self.nearest_thumb(min(t, self.dur))
                if thumb:
                    c.create_image(x + tw // 2, (y0 + y1) // 2, image=thumb[1], tags="strip")
                x += tw
            end_x = self.x_of(self.dur)
            c.create_rectangle(min(end_x, W - self.M), 0, W, y1 + 2, fill=self.BG, outline="", tags="blank")
            c.create_rectangle(0, 0, self.M, y1 + 2, fill=self.BG, outline="", tags="blank")
            self.draw_ruler()
            self.scroll.set(self.v0 / self.dur, self.v1 / self.dur)
        c.delete("ov")
        for i, (s, e) in enumerate(self.cuts):
            x0, x1 = self.x_of(s), self.x_of(e)
            if x1 < 0 or x0 > W:
                continue
            picked = i == self.sel
            edge = self.PICKED if picked else self.CUT
            c.create_rectangle(x0, y0, x1, y1, fill=self.CUT, stipple="gray50", outline="", tags="ov")
            c.create_rectangle(x0, y0 - 2, x1, y0 + 1, fill=edge, outline="", tags="ov")
            c.create_rectangle(x0, y1 - 1, x1, y1 + 2, fill=edge, outline="", tags="ov")
            hw = min(self.HANDLE, max(3, (x1 - x0) / 2))
            for hx0, hx1 in ((x0 - 1, x0 + hw), (x1 - hw, x1 + 1)):
                c.create_rectangle(hx0, y0 - 2, hx1, y1 + 2, fill=edge, outline="", tags="ov")
                mid = (hx0 + hx1) / 2
                c.create_line(mid, y0 + 16, mid, y1 - 16, fill="#2a1010" if not picked else "#4a3a00", width=2, tags="ov")
            if x1 - x0 > 76:
                cx = (x0 + x1) / 2
                c.create_rectangle(cx - 34, (y0 + y1) / 2 - 10, cx + 34, (y0 + y1) / 2 + 10, fill=self.CUT_EDGE, outline="", tags="ov")
                c.create_text(cx, (y0 + y1) / 2, text=f"✕ {fmt_time(e - s)}", fill="#ffffff", font=("Segoe UI", 8, "bold"), tags="ov")
        px = self.x_of(self.playhead)
        if -2 <= px <= W + 2:
            c.create_line(px, 2, px, y1 + 4, fill="#000000", width=4, tags="ov")
            c.create_line(px, 2, px, y1 + 4, fill="#ffffff", width=2, tags="ov")
            c.create_polygon(px - 5, 0, px + 5, 0, px, 7, fill="#ffffff", outline="#000000", tags="ov")
        if self.hover is not None and self.drag is None:
            hx = self.x_of(self.hover)
            c.create_line(hx, y0, hx, y1, fill="#ffffff", dash=(3, 3), tags="ov")

    def draw_ruler(self):
        c = self.c
        span = self.v1 - self.v0
        inner = self.width() - 2 * self.M
        step = next((s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                     if s / span * inner >= 70), 7200)
        y = self.TOP + self.STRIP + 4
        t = math.floor(self.v0 / step) * step
        while t <= self.v1 + 1e-9:
            if t >= self.v0 - 1e-9:
                x = self.x_of(t)
                c.create_line(x, y, x, y + 5, fill=self.RULER_TEXT, tags="ruler")
                m, s = divmod(round(t, 1), 60)
                label = f"{int(m)}:{s:04.1f}" if step < 1 else f"{int(m)}:{int(s):02d}"
                if m >= 60:
                    label = fmt_time(t)
                c.create_text(x + 3, y + 4, text=label, anchor="nw", fill=self.RULER_TEXT, font=("Segoe UI", 8), tags="ruler")
            t += step

    # ------------------------------------------------------------ hover snippet
    def show_bubble(self, x, t):
        thumb = self.nearest_thumb(t)
        cut = self.in_cut(t)
        colour = self.CUT if cut else "#111111"
        self.bubble_box.config(bg=colour)
        self.bubble_img.config(bg=colour, image=thumb[0] if thumb else self.bubble_blank,
                              text="" if thumb else "Loading...", fg="#aaa")
        self.bubble_text.config(bg=colour, text=f"{fmt_time(t)}{'   will be deleted' if cut else ''}")
        self.bubble.update_idletasks()
        bw, bh = self.bubble.winfo_reqwidth(), self.bubble.winfo_reqheight()
        bx = int(self.c.winfo_rootx() + x - bw / 2)
        by = self.c.winfo_rooty() - bh - 6
        if by < 0:
            by = self.c.winfo_rooty() + self.c.winfo_height() + 6
        bx = max(0, min(bx, self.c.winfo_screenwidth() - bw))
        self.bubble.geometry(f"+{bx}+{by}")
        if self.bubble.state() == "withdrawn":
            self.bubble.deiconify()
            self.bubble.lift()

    def hide_bubble(self):
        if self.bubble.state() != "withdrawn":
            self.bubble.withdraw()

    # ------------------------------------------------------------ mouse and keys
    def hit(self, x, y):
        if not (self.TOP - 6 <= y <= self.TOP + self.STRIP + 6):
            return ("empty",)
        order = ([self.sel] if self.sel is not None else []) + [i for i in range(len(self.cuts)) if i != self.sel]
        for i in order:
            s, e = self.cuts[i]
            x0, x1 = self.x_of(s), self.x_of(e)
            d0, d1 = abs(x - x0), abs(x - x1)
            if min(d0, d1) <= self.HANDLE:
                return ("edge", i, 0 if d0 < d1 or (d0 == d1 and x < x0) else 1)
        for i in order:
            s, e = self.cuts[i]
            if self.x_of(s) < x < self.x_of(e):
                return ("body", i)
        return ("empty",)

    def on_motion(self, e):
        if not self.src:
            return
        kind = self.hit(e.x, e.y)[0]
        self.c.config(cursor={"edge": "sb_h_double_arrow", "body": "fleur"}.get(kind, "crosshair"))
        t = self.t_of(e.x)
        self.hover = t
        self.show_bubble(e.x, t)
        self.show_time(t)
        self.redraw()

    def on_leave(self, e):
        self.hide_bubble()
        if self.drag is None and self.hover is not None:
            self.hover = None
            self.redraw()
            self.show_time(self.playhead)

    def on_press(self, e):
        if not self.src:
            return
        self.c.focus_set()
        t = self.t_of(e.x)
        hit = self.hit(e.x, e.y)
        self.before = [list(cut) for cut in self.cuts]
        if hit[0] == "edge":
            self.sel = hit[1]
            self.drag = {"mode": "edge", "i": hit[1], "side": hit[2]}
        elif hit[0] == "body":
            s, en = self.cuts[hit[1]]
            self.sel = hit[1]
            self.drag = {"mode": "move", "i": hit[1], "grab": t - s, "len": en - s, "x": e.x, "moved": False}
        else:
            self.drag = {"mode": "new", "anchor": t, "x": e.x}
        self.update_selection()
        self.redraw()

    def on_drag(self, e):
        d = self.drag
        if not d or not self.src:
            return
        W = self.width()
        if e.x < self.M and self.v0 > 0:  # keep scrolling while dragging past the ends
            self.pan(-0.04)
        elif e.x > W - self.M and self.v1 < self.dur:
            self.pan(0.04)
        t = self.t_of(e.x)
        if d["mode"] == "new":
            if abs(e.x - d["x"]) < 4:
                return
            self.cuts.append([min(d["anchor"], t), max(d["anchor"], t)])
            self.sel = len(self.cuts) - 1
            d.update(mode="edge", i=self.sel, side=1 if t >= d["anchor"] else 0)
        if d["mode"] == "edge":
            s, en = self.cuts[d["i"]]
            if d["side"] == 0:
                s = t
            else:
                en = t
            if s > en:
                s, en = en, s
                d["side"] = 1 - d["side"]
            self.cuts[d["i"]] = [s, en]
        elif d["mode"] == "move":
            if abs(e.x - d["x"]) < 3 and not d["moved"]:
                return
            d["moved"] = True
            s = min(max(t - d["grab"], 0.0), self.dur - d["len"])
            self.cuts[d["i"]] = [s, s + d["len"]]
        self.hover = t
        self.show_bubble(e.x, t)
        self.show_time(t)
        self.update_selection()
        self.update_summary()
        self.redraw()

    def on_release(self, e):
        d, self.drag = self.drag, None
        if not d or not self.src:
            return
        if d["mode"] == "new" or (d["mode"] == "move" and not d["moved"]):
            self.playhead = self.t_of(e.x)  # a plain click moves the playhead
            if d["mode"] == "new":
                self.sel = None
        else:
            self.tidy(self.cuts[d["i"]])
        if self.cuts != self.before:
            self.history.append(self.before)
        self.update_selection()
        self.update_summary()
        self.redraw()
        self.show_time(self.t_of(e.x))

    def tidy(self, keep_selected=None):
        """Merge overlapping parts and keep the right one selected."""
        self.cuts = normalise(self.cuts, self.dur)
        self.sel = None
        if keep_selected:
            mid = (keep_selected[0] + keep_selected[1]) / 2
            self.sel = next((i for i, (s, e) in enumerate(self.cuts) if s <= mid <= e), None)

    def on_right_click(self, e):
        hit = self.hit(e.x, e.y)
        if hit[0] in ("edge", "body"):
            self.history.append([list(cut) for cut in self.cuts])
            del self.cuts[hit[1]]
            self.sel = None
            self.after_change()

    def on_wheel(self, e, direction):
        if self.src:
            self.zoom(0.8 if direction < 0 else 1.25, self.t_of(e.x))

    def nudge(self, secs):
        if self.src:
            self.playhead = min(max(self.playhead + secs, 0.0), self.dur)
            if not self.v0 <= self.playhead <= self.v1:
                span = self.v1 - self.v0
                self.set_view(self.playhead - span / 2, self.playhead + span / 2)
            self.redraw()
            self.show_time(self.playhead)

    # ------------------------------------------------------------ zoom and scroll
    def set_view(self, v0, v1):
        span = min(max(v1 - v0, min(self.dur, 1.0)), self.dur)
        v0 = min(max(v0, 0.0), self.dur - span)
        self.v0, self.v1 = v0, v0 + span
        self.redraw(strip=True)
        self.later("zoomthumbs", 250, self.ask_zoom_thumbs)

    def zoom(self, factor, anchor=None):
        if not self.src:
            return
        span = self.v1 - self.v0
        anchor = (self.v0 + self.v1) / 2 if anchor is None else anchor
        frac = (anchor - self.v0) / span
        new = span * factor
        self.set_view(anchor - frac * new, anchor - frac * new + new)

    def fit(self):
        if self.src:
            self.set_view(0.0, self.dur)

    def pan(self, fraction):
        if self.src:
            span = self.v1 - self.v0
            self.set_view(self.v0 + span * fraction, self.v1 + span * fraction)

    def on_scroll(self, *args):
        if not self.src:
            return
        span = self.v1 - self.v0
        if args[0] == "moveto":
            v0 = float(args[1]) * self.dur
            self.set_view(v0, v0 + span)
        elif args[0] == "scroll":
            self.pan(int(args[1]) * (0.9 if args[2] == "pages" else 0.1))

    # ------------------------------------------------------------ editing the marked parts
    def after_change(self):
        self.update_selection()
        self.update_summary()
        self.redraw()
        self.show_time(self.hover if self.hover is not None else self.playhead)

    def remove_selected(self):
        if self.sel is not None:
            self.history.append([list(cut) for cut in self.cuts])
            del self.cuts[self.sel]
            self.sel = None
            self.after_change()

    def clear_cuts(self):
        if self.cuts:
            self.history.append([list(cut) for cut in self.cuts])
            self.cuts, self.sel = [], None
            self.after_change()

    def undo(self):
        if self.history:
            self.cuts, self.sel = self.history.pop(), None
            self.after_change()

    def apply_entries(self, _=None):
        if self.sel is None or self.sel >= len(self.cuts):
            return
        try:
            s, e = parse_time(self.sel_start.get()), parse_time(self.sel_end.get())
        except ValueError as err:
            messagebox.showwarning(APP_NAME, str(err))
            self.update_selection()
            return
        new = [min(s, e, self.dur), min(max(s, e), self.dur)]
        if [round(v, 1) for v in new] == [round(v, 1) for v in self.cuts[self.sel]]:
            return
        self.history.append([list(cut) for cut in self.cuts])
        self.cuts[self.sel] = new
        self.tidy(new)
        self.after_change()

    def update_selection(self):
        picked = self.sel is not None and self.sel < len(self.cuts)
        for ent in self.sel_entries:
            ent.config(state="normal" if picked else "disabled")
        self.remove_btn.config(state="normal" if picked else "disabled")
        if picked:
            s, e = self.cuts[self.sel]
            self.sel_start.set(fmt_time(s))
            self.sel_end.set(fmt_time(e))
            self.sel_len.config(text=f"{fmt_time(e - s)} long.  Type exact times and press Enter, or drag the yellow edges.")
        else:
            self.sel_start.set("")
            self.sel_end.set("")
            self.sel_len.config(text="Click a red part to select it.")

    def update_summary(self):
        if not self.src:
            self.summary.config(text="Drag across the filmstrip to mark a part to delete. Hover over it to see that moment.",
                                foreground="#666")
            return
        cuts = normalise(self.cuts, self.dur)
        gone = sum(e - s for s, e in cuts)
        try:
            speed = float(self.speed.get().rstrip("x"))
        except ValueError:
            speed = 1.0
        new_len = (self.dur - gone) / speed
        if cuts:
            text = (f"{len(cuts)} part{'s' if len(cuts) != 1 else ''} marked for deletion ({fmt_time(gone)}).  "
                    f"New length {fmt_time(new_len)}.   Drag the edges to change a part, right-click it to unmark it.")
        else:
            text = (f"Drag across the filmstrip to mark a part to delete. Hover to see any moment, scroll to zoom in."
                    f"{f'  New length {fmt_time(new_len)}.' if speed != 1 else ''}")
        self.summary.config(text=text, foreground="")

    # ------------------------------------------------------------ playing and saving
    def play_preview(self):
        if not self.src:
            messagebox.showinfo(APP_NAME, "Open a video first.")
            return
        player = ffplay_path(self.app.ffmpeg)
        if not player:
            open_path(self.src)
            messagebox.showinfo(APP_NAME, "FFplay wasn't found, so the video opened in your normal player, which won't skip the marked parts.")
            return
        try:
            cmd = preview_command(player, self.src, self.info, self.cuts, self.playhead)
        except ValueError as e:
            messagebox.showinfo(APP_NAME, str(e))
            return
        if self.player and self.player.poll() is None:
            self.player.kill()
        self.player = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=NOWIN)

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
            cmd, total = edit_command(ff, self.src, out, self.info, self.cuts, float(self.speed.get().rstrip("x")),
                                      self.rotate.get(), self.volume.get(), self.size.get(), self.fmt.get(), self.quality.get())
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
