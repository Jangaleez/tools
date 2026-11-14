"""Linux helpers for Screen Recorder & Editor (Debian, Ubuntu and their relatives).

- Sound: lists microphones and "what you hear" sources through PulseAudio/PipeWire (pactl).
- X11 desktops: the screen is captured by FFmpeg (x11grab).
- Wayland desktops (KDE Plasma 6, GNOME): apps aren't allowed to grab the screen
  directly, so we ask the desktop through the standard ScreenCast portal. The
  desktop shows its own "what do you want to share?" window, then hands over a
  PipeWire video stream, which GStreamer records.
"""
import itertools
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

APT_PACKAGES = ["ffmpeg", "python3-tk", "python3-gi", "pulseaudio-utils", "gstreamer1.0-tools",
                "gstreamer1.0-pipewire", "gstreamer1.0-plugins-good", "gstreamer1.0-plugins-ugly",
                "gstreamer1.0-pulseaudio", "xdg-desktop-portal"]


def is_wayland():
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def videos_dir():
    try:
        r = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=3)
        p = r.stdout.strip()
        if r.returncode == 0 and p and p != str(Path.home()):
            return Path(p)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Videos"


# ---------------------------------------------------------------- sound

def audio_sources():
    """[(label, pulse_name, is_monitor)] — monitors are "what you hear" from speakers/headphones."""
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(["pactl", "list", "sources"], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for block in re.split(r"\n(?=Source #)", out):
        name = re.search(r"^\s*Name: (.+)$", block, re.M)
        desc = re.search(r"^\s*Description: (.+)$", block, re.M)
        if not name:
            continue
        name = name.group(1).strip()
        label = desc.group(1).strip() if desc else name
        monitor = name.endswith(".monitor")
        if monitor and not label.lower().startswith("monitor"):
            label = f"Monitor of {label}"
        found.append((label, name, monitor))
    return found


def missing_tools(wayland):
    need = {"ffmpeg": "ffmpeg", "pactl": "pulseaudio-utils"}
    if wayland:
        need["gst-launch-1.0"] = "gstreamer1.0-tools"
    missing = [pkg for cmd, pkg in need.items() if not shutil.which(cmd)]
    if wayland:
        try:
            import gi  # noqa: F401
        except ImportError:
            missing.append("python3-gi")
        if shutil.which("gst-inspect-1.0"):
            for element, pkg in (("pipewiresrc", "gstreamer1.0-pipewire"), ("x264enc", "gstreamer1.0-plugins-ugly"),
                                 ("matroskamux", "gstreamer1.0-plugins-good"), ("pulsesrc", "gstreamer1.0-pulseaudio")):
                if subprocess.run(["gst-inspect-1.0", "--exists", element]).returncode != 0:
                    missing.append(pkg)
    return list(dict.fromkeys(missing))


# ---------------------------------------------------------------- Wayland: ScreenCast portal

_tokens = itertools.count(1)  # every portal request needs a fresh token


class PortalError(RuntimeError):
    pass


class ScreenCast:
    """One screen-sharing session with the desktop. Keep the object alive while recording."""

    def __init__(self):
        self.conn = None
        self.session = None
        self.fd = None
        self.node = None
        self.size = None

    def open(self, window=False, cursor=True, timeout=120):
        """Shows the desktop's own picker. Blocks until the user chooses (or cancels)."""
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError):
            raise PortalError("python3-gi is not installed. Run the Linux installer (install-linux.sh) first.")

        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            sender = self.conn.get_unique_name()[1:].replace(".", "_")

            def call(method, params, with_fds=False):
                token = f"screenrec{os.getpid()}_{next(_tokens)}"
                path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
                result, loop = {}, GLib.MainLoop.new(ctx, False)

                def on_response(_c, _s, _p, _i, _sig, args):
                    result["code"], result["data"] = args.unpack()
                    loop.quit()

                sub = self.conn.signal_subscribe("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
                                                 "Response", path, None, Gio.DBusSignalFlags.NONE,
                                                 on_response)
                # handle_token goes into the options dict (always the last argument)
                *head, opts = params
                opts = dict(opts)
                opts["handle_token"] = GLib.Variant("s", token)
                args = GLib.Variant(f"({''.join(v.get_type_string() for v in head)}a{{sv}})", (*[v.unpack() for v in head], opts)) \
                    if head else GLib.Variant("(a{sv})", (opts,))
                try:
                    self.conn.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                        "org.freedesktop.portal.ScreenCast", method, args, None,
                                        Gio.DBusCallFlags.NONE, -1, None)
                except GLib.Error as e:
                    self.conn.signal_unsubscribe(sub)
                    raise PortalError(f"The desktop's screen-sharing service didn't answer ({e.message}). "
                                      "Is xdg-desktop-portal installed?")
                timer = GLib.timeout_source_new_seconds(timeout)
                timer.set_callback(lambda *_: (result.setdefault("code", 99), loop.quit(), False)[-1])
                timer.attach(ctx)
                loop.run()
                timer.destroy()
                self.conn.signal_unsubscribe(sub)
                code = result.get("code", 2)
                if code == 1:
                    raise PortalError("Screen sharing was cancelled.")
                if code != 0:
                    raise PortalError("The desktop refused screen sharing.")
                return result["data"]

            def prop(name):
                v = self.conn.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                        "org.freedesktop.DBus.Properties", "Get",
                                        GLib.Variant("(ss)", ("org.freedesktop.portal.ScreenCast", name)),
                                        None, Gio.DBusCallFlags.NONE, -1, None)
                return v.unpack()[0]

            data = call("CreateSession", [{"session_handle_token": GLib.Variant("s", f"screenrecs{os.getpid()}_{next(_tokens)}")}])
            self.session = data["session_handle"]

            types = 2 if window else 1  # 1 = monitor, 2 = window
            try:
                modes = prop("AvailableCursorModes")
            except Exception:
                modes = 1
            cursor_mode = 2 if (cursor and modes & 2) else 1  # 2 = drawn into the video, 1 = hidden
            call("SelectSources", [GLib.Variant("o", self.session),
                                   {"types": GLib.Variant("u", types), "multiple": GLib.Variant("b", False),
                                    "cursor_mode": GLib.Variant("u", cursor_mode)}])
            data = call("Start", [GLib.Variant("o", self.session), GLib.Variant("s", ""), {}])
            streams = data.get("streams") or []
            if not streams:
                raise PortalError("Nothing was chosen to record.")
            self.node, props = streams[0]
            self.size = props.get("size")

            ret, fds = self.conn.call_with_unix_fd_list_sync(
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast", "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session, {})), None, Gio.DBusCallFlags.NONE, -1, None, None)
            self.fd = fds.get(ret.unpack()[0])
            os.set_inheritable(self.fd, True)
        except PortalError:
            self.close()
            raise
        except Exception as e:
            self.close()
            raise PortalError(f"Screen sharing failed: {e}")
        finally:
            ctx.pop_thread_default()
        return self

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.conn and self.session:
            try:
                from gi.repository import Gio, GLib
                self.conn.call_sync("org.freedesktop.portal.Desktop", self.session, "org.freedesktop.portal.Session",
                                    "Close", None, None, Gio.DBusCallFlags.NONE, 2000, None)
            except Exception:
                pass
        self.session = None


def gst_command(cast, out, fps=30, quality="Balanced", audio=()):
    """GStreamer pipeline recording the portal stream (plus optional sound) to an MKV file."""
    qp = {"High": 18, "Balanced": 23, "Small file": 28}.get(quality, 23)
    cmd = ["gst-launch-1.0", "-e", "-q",
           "pipewiresrc", f"fd={cast.fd}", f"path={cast.node}", "do-timestamp=true", "keepalive-time=1000",
           "always-copy=true", "!", "videoconvert", "!", "videorate", "!"]
    if cast.size:  # H.264 needs an even width and height: trim one pixel if needed
        w, h = cast.size
        if w % 2 or h % 2:
            cmd += ["videocrop", f"right={w % 2}", f"bottom={h % 2}", "!"]
    cmd += [f"video/x-raw,format=I420,framerate={int(fps)}/1", "!",
           "x264enc", "speed-preset=ultrafast", "tune=zerolatency", "pass=quant", f"quantizer={qp}", f"key-int-max={int(fps) * 2}", "!",
           "h264parse", "!", "queue", "!", "mux."]
    if audio:
        if len(audio) > 1:
            cmd += ["audiomixer", "name=amix", "!", "audioconvert", "!", "opusenc", "bitrate=160000", "!", "queue", "!", "mux."]
            for name in audio:
                cmd += ["pulsesrc", f"device={name}", "do-timestamp=true", "!", "audioconvert", "!", "audioresample", "!",
                        "audio/x-raw,rate=48000,channels=2", "!", "queue", "!", "amix."]
        else:
            cmd += ["pulsesrc", f"device={audio[0]}", "do-timestamp=true", "!", "audioconvert", "!", "audioresample", "!",
                    "opusenc", "bitrate=160000", "!", "queue", "!", "mux."]
    cmd += ["matroskamux", "name=mux", "!", "filesink", f"location={out}"]
    return cmd


def run_in_thread(fn, on_done):
    """Run fn() off the UI thread; on_done(result, error) afterwards."""
    def work():
        try:
            on_done(fn(), None)
        except Exception as e:  # noqa: BLE001
            on_done(None, e)
    threading.Thread(target=work, daemon=True).start()
