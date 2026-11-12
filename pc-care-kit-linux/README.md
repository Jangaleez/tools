# PC Care Kit for Linux

The Linux version of [PC Care Kit](../pc-care-kit/): a tabbed PC health and maintenance app for **Debian, Ubuntu** and systems built on them, including TUXEDO OS, Kubuntu, Linux Mint and Pop!_OS. It works on KDE Plasma, GNOME and other desktops.

## Tabs

| Tab | What it does |
|---|---|
| **Overview** | A health summary: uptime, pending restart, memory, temperatures, free space on each drive, SSD/HDD, battery wear, failed services, cache and Trash size, startup apps, and updates |
| **Advisor** | Suggestions from the latest check, most important first. Also lets you turn on a weekly background check, which sends a notification if something needs attention |
| **Startup** | Turn apps that start when you log in on or off |
| **Cleanup** | Old app cache, Trash, downloaded update files, old system logs, packages no longer needed and unused Flatpak runtimes |
| **Disk Space** | See which folders take up space, and click into them |
| **Updates** | Check for and install system (apt), Flatpak and Snap updates |
| **Maintenance** | Drive health (SMART), failed services, errors since start-up, package check and repair, SSD trim, and system snapshots (Timeshift or Snapper) |

## Safety rules

- Nothing is deleted or changed until you click a button and confirm.
- Cache cleanup only removes files older than 24 hours.
- Startup changes use the standard per-user on/off switch, so they show up in your desktop's own Autostart settings and can be undone. System files are never edited.
- Anything that needs administrator rights shows the exact command first, then asks for your password in a normal window (using pkexec).
- Updates use apt's safe `upgrade`, which never removes packages. If some packages are held back, finish them in your system's own updater.
- No kernel tweaks, no service changes and no config file editing.

## How to install

1. Download this folder. Either:
   - get the whole repo (green **Code** button → **Download ZIP**) and unzip it, or
   - in a terminal, run `git clone https://github.com/Jangaleez/tools.git`.
2. Open a terminal in the `pc-care-kit-linux` folder and run:
   ```
   bash install-linux.sh
   ```
   This installs a few small packages: `python3-tk`, `pkexec`, `smartmontools`, `libnotify-bin` and `xdg-utils`. It then adds **PC Care Kit** to your app menu.
3. Open **PC Care Kit** from your app menu.

If you move the folder later, run the installer again so the menu entry follows it.

## Weekly check

The weekly check is a systemd user timer that runs every Sunday at 12:00, or at your next login if the PC was off then. It runs `pc_care_kit.py --background`, which only reports and never changes anything. You can turn it on or off on the **Advisor** tab.
