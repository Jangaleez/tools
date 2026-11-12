#!/usr/bin/env python3
"""PC Care Kit for Linux - a home-grown health and maintenance app for Debian and Ubuntu based systems
(TUXEDO OS, Linux Mint, Pop!_OS, Kubuntu...). Start it with run-pc-care-kit.sh.

Safety rules this app follows (the same as the Windows version):
  - Nothing is deleted or changed without you clicking a button and confirming.
  - Cache cleanup only removes files older than 24 hours.
  - Startup changes use the standard desktop on/off switch (a copy in ~/.config/autostart),
    so they are reversible and show up in your desktop's own Autostart settings.
  - Anything needing administrator rights asks for your password through the desktop (pkexec),
    and shows exactly which command it will run first.
  - No kernel tweaks, no service changes, no config file editing.
"""
import configparser
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "PC Care Kit"
HOME = Path.home()
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "pc-care-kit"
REPORT = DATA_DIR / "last-check.json"
USER_AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "autostart"
SYSTEM_AUTOSTART = [Path(p) / "autostart" for p in os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":") if p]
SYSTEMD_USER = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"
TIMER = "pc-care-kit-weekly"
DAY = 24 * 3600


# ==========================================================================
# Logic (no user interface in this section)
# ==========================================================================

def run(cmd, timeout=60, env=None):
    """Run a command, return (exit code, output). Never raises for missing programs."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C", **(env or {})})
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 1, str(e)


def has(cmd):
    return shutil.which(cmd) is not None


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def folder_size(path, older_than=None, stop=None):
    """Total bytes of files under path (not following links or crossing into other drives)."""
    total, files = 0, 0
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return 0, 0
    cutoff = time.time() - older_than if older_than else None
    stack = [str(path)]
    while stack:
        if stop and stop.is_set():
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_dir(follow_symlinks=False):
                            if st.st_dev == dev:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            if cutoff is None or st.st_mtime < cutoff:
                                total += st.st_blocks * 512 if st.st_blocks else st.st_size
                                files += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, files


def os_release():
    info = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(p).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k] = v.strip().strip('"')
            break
        except OSError:
            pass
    return info


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def meminfo():
    m = {}
    for line in (read("/proc/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            m[k] = int(parts[0]) * 1024
    return m


def restart_pending():
    """Ubuntu/Debian mark this file when an update needs a restart; also catch a newer kernel installed."""
    if Path("/run/reboot-required").exists() or Path("/var/run/reboot-required").exists():
        return True
    running = platform.release()
    kernels = sorted(Path("/boot").glob("vmlinuz-*"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    if kernels and not Path(f"/boot/vmlinuz-{running}").exists():
        return False  # unusual layout (e.g. some btrfs/systemd-boot setups) - don't guess
    if kernels:
        newest = kernels[-1].name.removeprefix("vmlinuz-")
        return newest != running and kernels[-1].stat().st_mtime > Path(f"/boot/vmlinuz-{running}").stat().st_mtime
    return False


REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "jfs", "zfs", "bcachefs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk"}


def mounted_drives():
    seen, out = set(), []
    for line in (read("/proc/mounts", "") or "").splitlines():
        dev, mnt, fs = line.split()[:3]
        mnt = mnt.replace("\\040", " ")
        if fs not in REAL_FS or dev in seen or mnt.startswith(("/snap", "/var/snap", "/run/credentials")):
            continue
        if mnt.startswith("/boot") and mnt != "/boot":  # the small EFI partition is managed by the system
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        if not st.f_blocks:
            continue
        seen.add(dev)
        out.append((mnt, dev, fs, st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize))
    return out


def physical_disks():
    disks = []
    for b in sorted(Path("/sys/block").glob("*")):
        name = b.name
        if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
            continue
        size = int(read(b / "size", "0") or 0) * 512
        if size < 1 << 30:
            continue
        rot = read(b / "queue/rotational", "0") == "1"
        model = read(b / "device/model", "") or name
        if name.startswith(("vd", "xvd")):
            kind = "Virtual disk"
        else:
            kind = "NVMe SSD" if name.startswith("nvme") else ("HDD" if rot else "SSD")
        disks.append({"name": name, "model": model.strip(), "size": size, "kind": kind})
    return disks


def battery():
    for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        for full, design in (("energy_full", "energy_full_design"), ("charge_full", "charge_full_design")):
            f, d = read(b / full), read(b / design)
            if f and d and int(d) > 0:
                return 100 * int(f) / int(d), read(b / "capacity"), read(b / "status")
    return None


def max_temperature():
    best = None
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        t = read(z / "temp")
        if t and t.lstrip("-").isdigit():
            c = int(t) / 1000
            if 0 < c < 150 and (best is None or c > best[0]):
                best = (c, read(z / "type", "sensor"))
    for h in Path("/sys/class/hwmon").glob("hwmon*"):
        for t in h.glob("temp*_input"):
            v = read(t)
            if v and v.lstrip("-").isdigit():
                c = int(v) / 1000
                if 0 < c < 150 and (best is None or c > best[0]):
                    best = (c, read(h / "name", "sensor"))
    return best


UNIT = re.compile(r"^[\w@.\\:-]+\.(service|socket|mount|timer|path|target|scope|slice|swap|automount|device)$")


def failed_services():
    out = []
    for scope in ([], ["--user"]):
        code, text = run(["systemctl", *scope, "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
        for line in text.splitlines():
            first = line.replace("●", "").split()[:1]
            if first and UNIT.match(first[0]):
                out.append(first[0])
    return out


def journal_errors_this_boot():
    code, text = run(["journalctl", "-b", "-p", "3", "-q", "--no-pager", "-o", "cat"], timeout=20)
    if code != 0:
        return None
    if "not seeing messages from other users and the system" in text.lower():
        return None
    return [l for l in text.splitlines() if l.strip()]


def crash_reports():
    return [p for p in Path("/var/crash").glob("*.crash")] if Path("/var/crash").is_dir() else []


def apt_upgradable():
    code, text = run(["apt", "list", "--upgradable"], timeout=60)
    if code != 0:
        return None
    return [l.split("/")[0] for l in text.splitlines() if "/" in l and "[upgradable" in l]


def flatpak_updates():
    if not has("flatpak"):
        return None
    code, text = run(["flatpak", "remote-ls", "--updates", "--columns=application"], timeout=90)
    if code != 0:
        return None
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("Application")]


def snap_updates():
    if not has("snap"):
        return None
    code, text = run(["snap", "refresh", "--list"], timeout=60)
    if "All snaps up to date" in text:
        return []
    if code != 0:
        return None
    return [l.split()[0] for l in text.splitlines()[1:] if l.strip()]


def autoremove_list():
    code, text = run(["apt-get", "-s", "autoremove"], timeout=60)
    return [l.split()[1] for l in text.splitlines() if l.startswith("Remv ")] if code == 0 else []


def apt_cache_size():
    return folder_size("/var/cache/apt/archives")[0]


def journal_size():
    code, text = run(["journalctl", "--disk-usage"], timeout=15)
    m = re.search(r"take up ([\d.]+)([KMGT]?)", text)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return float(m.group(1)) * mult


def trash_dirs():
    out = [HOME / ".local/share/Trash"]
    for mnt, *_ in mounted_drives():
        for p in (Path(mnt) / f".Trash-{os.getuid()}", Path(mnt) / ".Trash" / str(os.getuid())):
            if p.is_dir():
                out.append(p)
    return [p for p in out if p.is_dir()]


# ---------------------------------------------------------------- startup apps (XDG autostart)

def current_desktops():
    return [d.strip().lower() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d.strip()]


def read_desktop(path):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
        return dict(cp["Desktop Entry"]) if cp.has_section("Desktop Entry") else {}
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}


def startup_items():
    entries = {}
    for d in SYSTEM_AUTOSTART:
        for f in sorted(d.glob("*.desktop")):
            entries.setdefault(f.name, {"system": f, "user": None})
    for f in sorted(USER_AUTOSTART.glob("*.desktop")):
        entries.setdefault(f.name, {"system": None, "user": None})["user"] = f
    desks = current_desktops()
    items = []
    for name, src in entries.items():
        effective = src["user"] or src["system"]
        e = read_desktop(effective)
        if not e:
            continue
        only = [x.lower() for x in e.get("OnlyShowIn", "").split(";") if x]
        never = [x.lower() for x in e.get("NotShowIn", "").split(";") if x]
        if desks and ((only and not set(only) & set(desks)) or (set(never) & set(desks))):
            continue  # not used on this desktop
        enabled = e.get("Hidden", "false").lower() != "true" and \
            e.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
        items.append({"file": name, "name": e.get("Name", name.removesuffix(".desktop")),
                      "command": e.get("Exec", ""), "enabled": enabled,
                      "where": "Just you" if src["user"] and not src["system"] else "All users",
                      "system": src["system"], "user": src["user"]})
    return sorted(items, key=lambda i: i["name"].lower())


def set_startup(item, enable):
    """The standard reversible way: a per-user copy with Hidden=true/false. System files are never touched."""
    USER_AUTOSTART.mkdir(parents=True, exist_ok=True)
    target = USER_AUTOSTART / item["file"]
    src = item["user"] or item["system"]
    lines = Path(src).read_text(encoding="utf-8", errors="replace").splitlines()
    out, in_entry, done = [], False, set()
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_entry and "Hidden" not in done:
                out.append(f"Hidden={'false' if enable else 'true'}")
                done.add("Hidden")
            in_entry = s == "[Desktop Entry]"
        elif in_entry and re.match(r"^Hidden\s*=", s):
            line = f"Hidden={'false' if enable else 'true'}"
            done.add("Hidden")
        elif in_entry and re.match(r"^X-GNOME-Autostart-enabled\s*=", s):
            line = f"X-GNOME-Autostart-enabled={'true' if enable else 'false'}"
        out.append(line)
    if in_entry and "Hidden" not in done:
        out.append(f"Hidden={'false' if enable else 'true'}")
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- health check

def row(area, item, value, status="OK", advice=""):
    return {"area": area, "item": item, "value": value, "status": status, "advice": advice}


def health(include_updates=False):
    rows = []
    osr = os_release()
    rows.append(row("System", "Linux", f"{osr.get('PRETTY_NAME', 'Linux')} (kernel {platform.release()})", "Info"))
    try:
        up = float(read("/proc/uptime", "0").split()[0])
        days = up / DAY
        if days > 7:
            rows.append(row("System", "Time since last restart", f"{days:.0f} days", "Warning",
                            "Restart the PC. A restart finishes installing updates (like a new kernel) and clears memory."))
        else:
            rows.append(row("System", "Time since last restart", f"{days:.1f} days"))
    except (ValueError, IndexError):
        pass
    if restart_pending():
        rows.append(row("System", "Restart pending", "Yes", "Warning",
                        "Updates are waiting for a restart to take effect (often a new kernel or security fix)."))
    else:
        rows.append(row("System", "Restart pending", "No"))

    m = meminfo()
    if m.get("MemTotal"):
        total, avail = m["MemTotal"], m.get("MemAvailable", m.get("MemFree", 0))
        used_pct = 100 * (1 - avail / total)
        text = f"{used_pct:.0f}% of {total / 1024 ** 3:.1f} GB in use"
        if used_pct > 85:
            rows.append(row("Memory", "RAM", text, "Warning", "Memory is nearly full. Close unused apps and browser tabs, and trim the Startup tab."))
        elif total < 7.5 * 1024 ** 3:
            rows.append(row("Memory", "RAM", text, "Info", "Under 8 GB of RAM. Adding memory will help more than any software change."))
        else:
            rows.append(row("Memory", "RAM", text))
        st, sf = m.get("SwapTotal", 0), m.get("SwapFree", 0)
        if st and (st - sf) / st > 0.5 and used_pct > 70:
            rows.append(row("Memory", "Swap", f"{fmt_size(st - sf)} of {fmt_size(st)} in use", "Warning",
                            "The PC is short of memory and using the slower swap space. Close some apps."))

    model = next((l.split(":", 1)[1].strip() for l in (read("/proc/cpuinfo", "") or "").splitlines()
                  if l.startswith("model name")), platform.machine())
    load = os.getloadavg()[0]
    cpus = os.cpu_count() or 1
    rows.append(row("Processor", model, f"{min(100, 100 * load / cpus):.0f}% average load ({cpus} threads)", "Info"))
    t = max_temperature()
    if t:
        if t[0] >= 90:
            rows.append(row("Processor", "Hottest sensor", f"{t[0]:.0f} °C ({t[1]})", "Warning",
                            "Something is running very hot. Check the fans and air vents aren't blocked or dusty."))
        else:
            rows.append(row("Processor", "Hottest sensor", f"{t[0]:.0f} °C ({t[1]})"))

    for mnt, dev, fs, size, free in mounted_drives():
        pct = 100 * free / size
        text = f"{fmt_size(free)} free of {fmt_size(size)} ({pct:.0f}%)"
        label = "System drive (/)" if mnt == "/" else f"Drive {mnt}"
        if pct < 10:
            rows.append(row("Storage", label, text, "Problem", f"{label} is almost full. Use the Cleanup and Disk Space tabs to free room."))
        elif pct < 20:
            rows.append(row("Storage", label, text, "Warning", f"{label} is getting full. Use the Cleanup and Disk Space tabs."))
        else:
            rows.append(row("Storage", label, text))
    for d in physical_disks():
        text = f"{d['kind']}, {fmt_size(d['size'])}"
        if d["kind"] == "HDD":
            rows.append(row("Storage", d["model"], text, "Info",
                            "This is a hard disk. If Linux runs from it, moving to an SSD is the biggest speed upgrade available."))
        else:
            rows.append(row("Storage", d["model"], text + " (health check on the Maintenance tab)"))

    b = battery()
    if b:
        pct, charge, status = b
        text = f"{pct:.0f}% of original capacity" + (f", {charge}% charged ({status})" if charge else "")
        if pct < 60:
            rows.append(row("Battery", "Battery health", text, "Warning", "The battery holds much less charge than when new. Consider a replacement."))
        else:
            rows.append(row("Battery", "Battery health", text))

    failed = failed_services()
    if failed:
        rows.append(row("Reliability", "Failed services", ", ".join(failed[:4]) + (" ..." if len(failed) > 4 else ""), "Warning",
                        "Some background services failed to start. See details on the Maintenance tab."))
    else:
        rows.append(row("Reliability", "Failed services", "0"))
    errs = journal_errors_this_boot()
    rows.append(row("Reliability", "Logged errors since start-up", "Not readable without admin" if errs is None else str(len(errs)), "Info"))
    crashes = crash_reports()
    if crashes:
        rows.append(row("Reliability", "App crash reports", str(len(crashes)), "Info",
                        "Some programs crashed recently (reports in /var/crash). If it keeps happening, install updates."))

    cache = folder_size(HOME / ".cache", older_than=DAY)[0]
    if cache > 1024 ** 3:
        rows.append(row("Housekeeping", "Old app cache files", fmt_size(cache), "Warning", "Over 1 GB of old cache files. Clear them on the Cleanup tab."))
    else:
        rows.append(row("Housekeeping", "Old app cache files", fmt_size(cache)))
    trash = sum(folder_size(p)[0] for p in trash_dirs())
    if trash > 1024 ** 3:
        rows.append(row("Housekeeping", "Trash", fmt_size(trash), "Warning", "Over 1 GB in the Trash. Empty it on the Cleanup tab."))
    else:
        rows.append(row("Housekeeping", "Trash", fmt_size(trash)))
    enabled = sum(1 for i in startup_items() if i["enabled"])
    if enabled > 10:
        rows.append(row("Housekeeping", "Apps starting with your desktop", str(enabled), "Warning",
                        "Many apps start when you log in. Turn off the ones you don't need on the Startup tab."))
    else:
        rows.append(row("Housekeeping", "Apps starting with your desktop", str(enabled)))

    if include_updates:
        ups = apt_upgradable()
        if ups:
            rows.append(row("Software", "System updates (apt)", str(len(ups)), "Warning",
                            f"{len(ups)} system updates are ready. Install them from the Updates tab."))
        elif ups is not None:
            rows.append(row("Software", "System updates (apt)", "0 (as of the last refresh)"))
        fl = flatpak_updates()
        if fl:
            rows.append(row("Software", "Flatpak app updates", str(len(fl)), "Warning", f"{len(fl)} Flatpak app updates are ready. Install them from the Updates tab."))
        elif fl is not None:
            rows.append(row("Software", "Flatpak app updates", "0"))
    return rows


def background_check():
    """Weekly timer mode: write a report and pop up a notification if anything needs attention. Changes nothing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = health(include_updates=True)
    advice = [r["advice"] for r in rows if r["advice"] and r["status"] != "Info"]
    REPORT.write_text(json.dumps({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "count": len(advice),
                                  "suggestions": advice}, indent=2))
    if advice and has("notify-send"):
        run(["notify-send", "-a", APP_NAME, "-i", "computer", APP_NAME,
             f"{len(advice)} suggestion(s) for your PC. Open PC Care Kit to see them."])


def timer_enabled():
    code, _ = run(["systemctl", "--user", "is-enabled", f"{TIMER}.timer"], timeout=10)
    return code == 0


def set_timer(on):
    if on:
        SYSTEMD_USER.mkdir(parents=True, exist_ok=True)
        py = sys.executable or "/usr/bin/python3"
        (SYSTEMD_USER / f"{TIMER}.service").write_text(
            "[Unit]\nDescription=PC Care Kit weekly health and update check (reports only; changes nothing)\n\n"
            f"[Service]\nType=oneshot\nExecStart=\"{py}\" \"{Path(__file__).resolve()}\" --background\n")
        (SYSTEMD_USER / f"{TIMER}.timer").write_text(
            "[Unit]\nDescription=Run PC Care Kit weekly check\n\n"
            "[Timer]\nOnCalendar=Sun 12:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        run(["systemctl", "--user", "daemon-reload"])
        code, text = run(["systemctl", "--user", "enable", "--now", f"{TIMER}.timer"])
    else:
        code, text = run(["systemctl", "--user", "disable", "--now", f"{TIMER}.timer"])
        for ext in ("service", "timer"):
            (SYSTEMD_USER / f"{TIMER}.{ext}").unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"])
    if code != 0 and on:
        raise RuntimeError(text.strip() or "systemctl failed")


# ==========================================================================
# User interface
# ==========================================================================

def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    STATUS_ICON = {"Problem": "✖ Problem", "Warning": "▲ Warning", "Info": "ℹ Info", "OK": "✔ OK"}
    ORDER = {"Problem": 0, "Warning": 1, "Info": 2, "OK": 3}

    root = tk.Tk(className="pc-care-kit")
    root.title(APP_NAME)
    root.geometry("1000x700")
    root.minsize(860, 560)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Treeview", rowheight=24)
    style.configure("Big.TButton", padding=6)
    inbox = queue.Queue()

    def ui(fn):
        inbox.put(fn)

    def pump():
        try:
            while True:
                inbox.get_nowait()()
        except queue.Empty:
            pass
        root.after(80, pump)

    status = tk.StringVar(value="")
    busy = {"on": False}

    def background(work, done=None, message="Working..."):
        if busy["on"]:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish.")
            return
        busy["on"] = True
        status.set(message)
        root.config(cursor="watch")

        def go():
            try:
                result, err = work(), None
            except Exception as e:  # noqa: BLE001
                result, err = None, e

            def finish():
                busy["on"] = False
                root.config(cursor="")
                status.set("" if err is None else f"Problem: {err}")
                if done and err is None:
                    done(result)
                elif err is not None:
                    messagebox.showwarning(APP_NAME, str(err))
            ui(finish)
        threading.Thread(target=go, daemon=True).start()

    def confirm(text):
        return messagebox.askokcancel(APP_NAME, text)

    def table(parent, columns, widths, height=12):
        frame = ttk.Frame(parent)
        tv = ttk.Treeview(frame, columns=[c for c, _ in columns], show="headings", height=height)
        for (cid, label), w in zip(columns, widths):
            tv.heading(cid, text=label)
            tv.column(cid, width=w, stretch=True, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("Problem", foreground="#b3261e")
        tv.tag_configure("Warning", foreground="#a15c00")
        return frame, tv

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
    bottom = ttk.Frame(root, padding=(10, 4))
    bottom.pack(fill="x")
    ttk.Label(bottom, textvariable=status).pack(side="left")
    ttk.Label(bottom, text=f"{os_release().get('PRETTY_NAME', 'Linux')}  ·  {os.environ.get('XDG_CURRENT_DESKTOP', '')}",
              foreground="#666").pack(side="right")

    def tab(name):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text=f"  {name}  ")
        return f

    # ------------------------------------------------------------ Overview + Advisor
    ov = tab("Overview")
    top = ttk.Frame(ov)
    top.pack(fill="x", pady=(0, 8))
    ttk.Label(top, text="A health summary of this PC. Nothing here changes anything.").pack(side="left")
    ttk.Button(top, text="Refresh", command=lambda: refresh_health(False)).pack(side="right")
    ttk.Button(top, text="Refresh + check updates", command=lambda: refresh_health(True)).pack(side="right", padx=6)
    f, ov_tv = table(ov, [("area", "Area"), ("item", "Item"), ("value", "Value"), ("status", "Status")], [110, 280, 420, 100], 20)
    f.pack(fill="both", expand=True)

    adv = tab("Advisor")
    ttk.Label(adv, text="Suggestions based on the latest check, most important first.").pack(anchor="w", pady=(0, 8))
    f, adv_tv = table(adv, [("status", "Status"), ("item", "About"), ("advice", "Suggestion")], [100, 220, 620], 14)
    f.pack(fill="both", expand=True)
    wk = ttk.LabelFrame(adv, text="Weekly background check", padding=10)
    wk.pack(fill="x", pady=(10, 0))
    ttk.Label(wk, wraplength=900, justify="left",
              text="Runs every Sunday at 12:00 (or at next login if the PC was off), checks health and updates, "
                   "and shows a notification if something needs attention. It only reports - it never changes anything.").pack(anchor="w")
    wk_row = ttk.Frame(wk)
    wk_row.pack(fill="x", pady=(6, 0))
    wk_text = ttk.Label(wk_row, text="")
    wk_text.pack(side="left")
    ttk.Button(wk_row, text="Turn off", command=lambda: toggle_timer(False)).pack(side="right")
    ttk.Button(wk_row, text="Turn on weekly check", command=lambda: toggle_timer(True)).pack(side="right", padx=6)

    def show_timer():
        on = timer_enabled()
        last = ""
        try:
            rep = json.loads(REPORT.read_text())
            last = f"  Last check: {rep['time']}, {rep['count']} suggestion(s)."
        except (OSError, ValueError, KeyError):
            pass
        wk_text.config(text=("Weekly check is ON." if on else "Weekly check is OFF.") + last)

    def toggle_timer(on):
        try:
            set_timer(on)
            status.set("Weekly check turned on." if on else "Weekly check turned off.")
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning(APP_NAME, f"Could not change the weekly check: {e}")
        show_timer()

    def refresh_health(updates):
        def done(rows):
            ov_tv.delete(*ov_tv.get_children())
            for r in rows:
                ov_tv.insert("", "end", values=(r["area"], r["item"], r["value"], STATUS_ICON[r["status"]]), tags=(r["status"],))
            adv_tv.delete(*adv_tv.get_children())
            tips = sorted([r for r in rows if r["advice"]], key=lambda r: ORDER[r["status"]])
            if not tips:
                adv_tv.insert("", "end", values=(STATUS_ICON["OK"], "Everything", "Nothing needs attention right now."))
            for r in tips:
                adv_tv.insert("", "end", values=(STATUS_ICON[r["status"]], r["item"], r["advice"]), tags=(r["status"],))
            show_timer()
        background(lambda: health(updates), done,
                   "Checking (and looking for updates, which can take a minute)..." if updates else "Checking...")

    # ------------------------------------------------------------ Startup
    su = tab("Startup")
    ttk.Label(su, wraplength=950, justify="left",
              text="Apps that start when you log in. Turning one off only affects your account and can be undone here "
                   "or in your desktop's own Autostart settings. Nothing is uninstalled.").pack(anchor="w", pady=(0, 8))
    f, su_tv = table(su, [("state", "State"), ("name", "App"), ("where", "Set up for"), ("command", "Runs")], [90, 260, 100, 480], 16)
    f.pack(fill="both", expand=True)
    su_btns = ttk.Frame(su)
    su_btns.pack(fill="x", pady=(8, 0))
    su_items = {}

    def refresh_startup():
        su_tv.delete(*su_tv.get_children())
        su_items.clear()
        for it in startup_items():
            iid = su_tv.insert("", "end", values=("On" if it["enabled"] else "Off", it["name"], it["where"], it["command"]))
            su_items[iid] = it

    def change_startup(enable):
        sel = [su_items[i] for i in su_tv.selection() if i in su_items]
        if not sel:
            messagebox.showinfo(APP_NAME, "Click an app in the list first (Ctrl+click to pick several).")
            return
        names = "\n".join(f"  • {i['name']}" for i in sel)
        if not confirm(f"{'Turn ON' if enable else 'Turn OFF'} starting at login for:\n\n{names}"):
            return
        for it in sel:
            try:
                set_startup(it, enable)
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"Could not change {it['name']}: {e}")
        refresh_startup()
        status.set("Startup apps updated. Changes apply next time you log in.")

    ttk.Button(su_btns, text="Turn off selected", command=lambda: change_startup(False)).pack(side="left")
    ttk.Button(su_btns, text="Turn on selected", command=lambda: change_startup(True)).pack(side="left", padx=6)
    ttk.Button(su_btns, text="Refresh", command=refresh_startup).pack(side="right")

    # ------------------------------------------------------------ Cleanup
    cl = tab("Cleanup")
    ttk.Label(cl, wraplength=950, justify="left",
              text="Click Analyse to see how much space each item takes, tick what you want gone, then Clean selected. "
                   "Items marked (admin) ask for your password.").pack(anchor="w", pady=(0, 8))
    targets = [
        ("cache", "Old app cache files (older than 24 hours)", "Thumbnails and temporary data apps can rebuild.", False),
        ("trash", "Trash", "Files you've already deleted. They can't be restored after this.", False),
        ("apt", "Downloaded update files (admin)", "Copies of installed packages kept by apt. Safe to remove.", True),
        ("journal", "Old system logs - keeps the last 2 weeks (admin)", "Logs from the system journal.", True),
        ("autoremove", "Packages no longer needed (admin)", "Leftovers such as old kernels apt marked as unused.", True),
        ("flatpak", "Unused Flatpak runtimes", "Shared parts no installed Flatpak app uses any more.", False),
    ]
    cl_vars, cl_sizes, cl_detail = {}, {}, {}
    grid = ttk.Frame(cl)
    grid.pack(fill="x")
    for r, (key, label, desc, _admin) in enumerate(targets):
        v = tk.BooleanVar(value=key in ("cache",))
        cl_vars[key] = v
        ttk.Checkbutton(grid, text=label, variable=v).grid(row=r, column=0, sticky="w", pady=3)
        cl_sizes[key] = ttk.Label(grid, text="-", width=16)
        cl_sizes[key].grid(row=r, column=1, sticky="w", padx=10)
        ttk.Label(grid, text=desc, foreground="#666").grid(row=r, column=2, sticky="w")
    if not has("flatpak"):
        cl_vars["flatpak"].set(False)
    cl_btns = ttk.Frame(cl)
    cl_btns.pack(fill="x", pady=10)
    ttk.Button(cl_btns, text="Analyse", command=lambda: analyse()).pack(side="left")
    ttk.Button(cl_btns, text="Clean selected", style="Big.TButton", command=lambda: clean()).pack(side="left", padx=6)
    cl_log = tk.Text(cl, height=10, wrap="word", state="disabled", relief="flat", background="#f6f6f6")
    cl_log.pack(fill="both", expand=True)

    def log_to(widget, text):
        widget.config(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.config(state="disabled")

    def analyse():
        def work():
            out = {}
            out["cache"] = folder_size(HOME / ".cache", older_than=DAY)
            out["trash"] = (sum(folder_size(p)[0] for p in trash_dirs()), 0)
            out["apt"] = (apt_cache_size(), 0)
            out["journal"] = (journal_size(), 0)
            rm = autoremove_list()
            out["autoremove"] = (None, len(rm))
            cl_detail["autoremove"] = rm
            return out

        def done(out):
            for key, (size, count) in out.items():
                if key == "autoremove":
                    cl_sizes[key].config(text=f"{count} package(s)")
                elif key == "cache":
                    cl_sizes[key].config(text=f"{fmt_size(size)}")
                else:
                    cl_sizes[key].config(text="?" if size is None else fmt_size(size))
            cl_sizes["flatpak"].config(text="Shown when cleaning" if has("flatpak") else "Flatpak not installed")
            status.set("Analysis done.")
        background(work, done, "Measuring...")

    def clean():
        chosen = [k for k, v in cl_vars.items() if v.get()]
        if not chosen:
            messagebox.showinfo(APP_NAME, "Tick at least one item first.")
            return
        lines = [label for k, label, _, _ in targets if k in chosen]
        extra = ""
        if "autoremove" in chosen and cl_detail.get("autoremove"):
            extra = "\n\nPackages that will be removed:\n  " + ", ".join(cl_detail["autoremove"][:30])
        admin = [k for k in chosen if k in ("apt", "journal", "autoremove")]
        cmds = []
        if "apt" in admin:
            cmds.append("apt-get clean")
        if "journal" in admin:
            cmds.append("journalctl --vacuum-time=2weeks")
        if "autoremove" in admin:
            cmds.append("DEBIAN_FRONTEND=noninteractive apt-get -y autoremove")
        if cmds:
            extra += "\n\nThese run as administrator (you'll be asked for your password):\n  " + "\n  ".join(cmds)
        if not confirm("Clean these items?\n\n  • " + "\n  • ".join(lines) + extra):
            return

        def work():
            report = []
            if "cache" in chosen:
                freed, n = 0, 0
                cutoff = time.time() - DAY
                for dirpath, dirnames, filenames in os.walk(HOME / ".cache", topdown=False):
                    for fn in filenames:
                        p = os.path.join(dirpath, fn)
                        try:
                            st = os.lstat(p)
                            if st.st_mtime < cutoff:
                                os.unlink(p)
                                freed += st.st_size
                                n += 1
                        except OSError:
                            pass
                    for dn in dirnames:  # remove folders left empty
                        try:
                            os.rmdir(os.path.join(dirpath, dn))
                        except OSError:
                            pass
                report.append(f"App cache: removed {n} old files ({fmt_size(freed)}).")
            if "trash" in chosen:
                freed = 0
                for t in trash_dirs():
                    for sub in ("files", "info", "expunged"):
                        d = t / sub
                        if d.is_dir():
                            freed += folder_size(d)[0]
                            for child in d.iterdir():
                                try:
                                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
                                except OSError:
                                    pass
                report.append(f"Trash: emptied ({fmt_size(freed)}).")
            if cmds:
                code, text = run(["pkexec", "sh", "-c", " && ".join(cmds)], timeout=1800)
                if code in (126, 127):
                    report.append("Admin items: cancelled (password not given).")
                else:
                    report.append(f"Admin items: {'done' if code == 0 else 'finished with errors'}.\n{text.strip()[-1500:]}")
            if "flatpak" in chosen and has("flatpak"):
                code, text = run(["flatpak", "uninstall", "--unused", "--assumeyes", "--noninteractive"], timeout=900)
                report.append(f"Flatpak: {'done' if code == 0 else 'problem'}.\n{text.strip()[-800:]}")
            return "\n".join(report)

        def done(text):
            log_to(cl_log, f"--- {datetime.now():%H:%M} ---\n{text}\n\n")
            analyse_after()

        background(work, done, "Cleaning...")

    def analyse_after():
        root.after(200, analyse)

    # ------------------------------------------------------------ Disk Space
    ds = tab("Disk Space")
    ds_top = ttk.Frame(ds)
    ds_top.pack(fill="x", pady=(0, 8))
    ds_path = tk.StringVar(value=str(HOME))
    ttk.Label(ds_top, text="Folder:").pack(side="left")
    ttk.Entry(ds_top, textvariable=ds_path).pack(side="left", fill="x", expand=True, padx=6)
    ttk.Button(ds_top, text="Browse", command=lambda: (lambda d: d and (ds_path.set(d), scan()))(filedialog.askdirectory(initialdir=ds_path.get()))).pack(side="left")
    ttk.Button(ds_top, text="Scan", command=lambda: scan()).pack(side="left", padx=6)
    f, ds_tv = table(ds, [("size", "Size"), ("share", "Share"), ("name", "Name"), ("kind", "Type")], [110, 160, 520, 100], 18)
    f.pack(fill="both", expand=True)
    ds_btns = ttk.Frame(ds)
    ds_btns.pack(fill="x", pady=(8, 0))
    ttk.Button(ds_btns, text="Up a level", command=lambda: (ds_path.set(str(Path(ds_path.get()).parent)), scan())).pack(side="left")
    ttk.Button(ds_btns, text="Open in file manager", command=lambda: open_selected()).pack(side="left", padx=6)
    ttk.Label(ds_btns, text="Double-click a folder to look inside it. This tab never deletes anything.", foreground="#666").pack(side="right")
    ds_paths = {}

    def scan():
        base = Path(ds_path.get()).expanduser()
        if not base.is_dir():
            messagebox.showinfo(APP_NAME, "That folder doesn't exist.")
            return

        def work():
            out = []
            try:
                entries = list(os.scandir(base))
            except OSError as e:
                raise RuntimeError(f"Can't read that folder: {e.strerror}")
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        size = folder_size(e.path)[0]
                        out.append((size, e.name, "Folder", e.path))
                    elif e.is_file():
                        st = e.stat()
                        out.append((st.st_blocks * 512 or st.st_size, e.name, "File", e.path))
                except OSError:
                    pass
            return sorted(out, reverse=True)

        def done(items):
            ds_tv.delete(*ds_tv.get_children())
            ds_paths.clear()
            total = sum(i[0] for i in items) or 1
            for size, name, kind, path in items:
                share = size / total
                bar = "█" * round(share * 12)
                iid = ds_tv.insert("", "end", values=(fmt_size(size), f"{bar} {share * 100:.0f}%", name, kind))
                ds_paths[iid] = (path, kind)
            status.set(f"{base}: {fmt_size(total)} in {len(items)} items.")
        background(work, done, f"Measuring {base} ...")

    def open_selected():
        sel = ds_tv.selection()
        target = ds_paths[sel[0]][0] if sel else ds_path.get()
        if sel and ds_paths[sel[0]][1] == "File":
            target = str(Path(target).parent)
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ds_double(_e):
        sel = ds_tv.selection()
        if sel and ds_paths.get(sel[0], ("", ""))[1] == "Folder":
            ds_path.set(ds_paths[sel[0]][0])
            scan()
    ds_tv.bind("<Double-1>", ds_double)

    # ------------------------------------------------------------ Updates
    upd = tab("Updates")
    ttk.Label(upd, wraplength=950, justify="left",
              text="Checks system packages (apt), Flatpak apps and Snaps. Installing uses apt's safe \"upgrade\", which "
                   "never removes packages - if some are held back, finish them in your system's own updater.").pack(anchor="w", pady=(0, 8))
    up_btns = ttk.Frame(upd)
    up_btns.pack(fill="x")
    ttk.Button(up_btns, text="Check for updates", command=lambda: check_updates()).pack(side="left")
    ttk.Button(up_btns, text="Update all", style="Big.TButton", command=lambda: update_all()).pack(side="left", padx=6)
    f, up_tv = table(upd, [("source", "From"), ("name", "Name")], [140, 700], 10)
    f.pack(fill="both", expand=True, pady=8)
    up_log = tk.Text(upd, height=8, wrap="word", state="disabled", relief="flat", background="#f6f6f6")
    up_log.pack(fill="both", expand=True)

    def stream(cmd, widget, done=None, message="Working..."):
        """Run a command and show its output live (used for long admin jobs)."""
        def work():
            log = lambda t: ui(lambda: log_to(widget, t))  # noqa: E731
            log(f"$ {' '.join(cmd)}\n")
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env={**os.environ, "LC_ALL": "C"})
            except FileNotFoundError:
                log(f"{cmd[0]} is not installed.\n")
                return 127
            for line in p.stdout:
                log(line)
            code = p.wait()
            log("Cancelled (password not given).\n\n" if code in (126, 127) and cmd[0] == "pkexec"
                else f"Finished{' with errors' if code else ''}.\n\n")
            return code
        background(work, done, message)

    def check_updates():
        if not confirm("Refreshing the list of system updates needs administrator rights (it runs: apt-get update). Continue?"):
            return

        def work():
            code, text = run(["pkexec", "apt-get", "update"], timeout=600)
            if code in (126, 127):
                raise RuntimeError("Cancelled (password not given).")
            return {"apt": apt_upgradable() or [], "flatpak": flatpak_updates(), "snap": snap_updates()}

        def done(res):
            up_tv.delete(*up_tv.get_children())
            n = 0
            for src, label in (("apt", "System (apt)"), ("flatpak", "Flatpak"), ("snap", "Snap")):
                for name in res.get(src) or []:
                    up_tv.insert("", "end", values=(label, name))
                    n += 1
            if n == 0:
                up_tv.insert("", "end", values=("", "Everything is up to date."))
            status.set(f"{n} update(s) available.")
        background(work, done, "Checking for updates...")

    def update_all():
        parts = ["apt-get update", "DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confdef "
                 "-o Dpkg::Options::=--force-confold upgrade"]
        msg = "Install all updates?\n\nAs administrator:\n  " + "\n  ".join(parts)
        if has("flatpak"):
            msg += "\n\nThen: flatpak update"
        if not confirm(msg):
            return

        def after(_code):
            if has("flatpak"):
                root.after(100, lambda: stream(["flatpak", "update", "-y", "--noninteractive"], up_log,
                                               lambda _c: check_after(), "Updating Flatpak apps..."))
            else:
                check_after()

        def check_after():
            status.set("Updates finished. Restart if the Overview says a restart is pending.")
        stream(["pkexec", "sh", "-c", " && ".join(parts)], up_log, after, "Installing system updates...")

    # ------------------------------------------------------------ Maintenance
    mt = tab("Maintenance")
    ttk.Label(mt, wraplength=950, justify="left",
              text="Checks and repairs. Read-only checks are safe to run any time. Results appear below.").pack(anchor="w", pady=(0, 8))
    mt_grid = ttk.Frame(mt)
    mt_grid.pack(fill="x")
    mt_log = tk.Text(mt, height=10, wrap="word", state="disabled", relief="flat", background="#f6f6f6")

    def drives_for_smart():
        return [f"/dev/{d['name']}" for d in physical_disks()]

    def smart_check():
        if not has("smartctl"):
            messagebox.showinfo(APP_NAME, "This needs smartmontools. Run install-linux.sh again, or: sudo apt install smartmontools")
            return
        devs = drives_for_smart()
        if confirm("Read each drive's built-in health report (read-only, needs your password)?\n\n  " + "\n  ".join(devs)):
            script = " ; ".join(f"echo '== {d}'; smartctl -H -A {d} | grep -Ei 'overall|result|percentage used|"
                                f"available spare|media.*errors|reallocated_sector|pending_sector|temperature' " for d in devs)
            stream(["pkexec", "sh", "-c", script], mt_log, message="Reading drive health...")

    def trim():
        if confirm("Trim SSDs (tells them which space is free, which keeps them fast). Runs as administrator:\n\n  fstrim -av"):
            stream(["pkexec", "fstrim", "-av"], mt_log, message="Trimming SSDs...")

    def failed_list():
        def work():
            a = run(["systemctl", "--failed", "--no-pager"])[1]
            b = run(["systemctl", "--user", "--failed", "--no-pager"])[1]
            return f"System services:\n{a}\nYour services:\n{b}"
        background(work, lambda t: log_to(mt_log, t + "\n"), "Listing failed services...")

    def errors_list():
        def work():
            errs = journal_errors_this_boot()
            if errs is None:
                return ("Your account can't read the system log. To allow it (one-off): "
                        "sudo usermod -aG systemd-journal $USER  then log out and in.\n")
            if not errs:
                return "No errors logged since start-up.\n"
            return f"{len(errs)} error lines since start-up. The latest 40:\n" + "\n".join(errs[-40:]) + "\n"
        background(work, lambda t: log_to(mt_log, t + "\n"), "Reading the system log...")

    def packages_check():
        if confirm("Check that installed packages are complete and consistent (like Windows' system file check). "
                   "Runs as administrator:\n\n  dpkg --audit\n  apt-get check"):
            stream(["pkexec", "sh", "-c", "echo '== dpkg --audit'; dpkg --audit && echo 'No problems found.'; "
                                          "echo '== apt-get check'; apt-get check"], mt_log, message="Checking packages...")

    def repair_packages():
        if confirm("Finish any half-installed updates and fix broken dependencies. Runs as administrator:\n\n"
                   "  dpkg --configure -a\n  apt-get -f install\n\nOnly use this if updates stopped part-way or the "
                   "package check reported problems."):
            stream(["pkexec", "sh", "-c", "dpkg --configure -a && DEBIAN_FRONTEND=noninteractive apt-get -y -f install"],
                   mt_log, message="Repairing packages...")

    def snapshot():
        if has("timeshift"):
            cmd = ["pkexec", "timeshift", "--create", "--comments", "PC Care Kit", "--scripted"]
        elif has("snapper"):
            cmd = ["pkexec", "snapper", "create", "--description", "PC Care Kit"]
        else:
            messagebox.showinfo(APP_NAME, "No snapshot tool found. Install Timeshift (sudo apt install timeshift) "
                                          "and set it up once - then this button makes a restore point.")
            return
        if confirm("Create a system snapshot (a restore point you can roll back to)? Runs as administrator:\n\n  " + " ".join(cmd[1:])):
            stream(cmd, mt_log, message="Creating a snapshot...")

    buttons = [
        ("Drive health (SMART)", smart_check, "Read-only. Shows each drive's own health and wear report."),
        ("Failed services", failed_list, "Read-only. Lists background services that didn't start."),
        ("Errors since start-up", errors_list, "Read-only. The latest serious errors from the system log."),
        ("Check packages", packages_check, "Read-only. Like Windows' SFC: checks installed software is complete."),
        ("Repair packages", repair_packages, "Finishes interrupted updates and fixes broken dependencies."),
        ("Trim SSDs", trim, "Keeps SSDs fast. Most systems also do this weekly by themselves."),
        ("Create snapshot", snapshot, "A restore point using Timeshift or Snapper, if set up."),
    ]
    for r, (label, fn, desc) in enumerate(buttons):
        ttk.Button(mt_grid, text=label, width=24, command=fn).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Label(mt_grid, text=desc, foreground="#666").grid(row=r, column=1, sticky="w", padx=(10, 0))
    mt_log.pack(fill="both", expand=True, pady=(10, 0))

    if not has("pkexec"):
        status.set("pkexec is missing, so admin actions won't work. Run install-linux.sh.")
    root.after(80, pump)
    root.after(200, lambda: refresh_health(False))
    root.after(300, refresh_startup)
    root.mainloop()


if __name__ == "__main__":
    if "--background" in sys.argv:
        background_check()
    else:
        try:
            gui()
        except Exception:
            import traceback
            log = DATA_DIR / "error.log"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc())
            print(f"PC Care Kit hit a problem. Details: {log}", file=sys.stderr)
            raise
