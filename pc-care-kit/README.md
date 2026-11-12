# PC Care Kit

A home-grown PC health and maintenance app for Windows 11, in one window with tabs. It uses only what ships with Windows (PowerShell 5.1, WPF and winget), so there's nothing to install.

## Tabs

| Tab | What it does |
|---|---|
| **Overview** | A health summary of the PC |
| **Advisor** | Suggestions based on what it finds |
| **Startup** | Turn apps that launch at startup on or off |
| **Cleanup** | Clear out old temporary files |
| **Disk Space** | See what's taking up space, folder by folder |
| **Updates** | Check for and install app updates with winget |
| **Maintenance** | System file check (SFC), Windows image repair (DISM), read-only disk check, drive optimisation, restore points, and an optional weekly check |

## Safety rules

- Nothing is deleted or changed until you click a button and confirm.
- Temp cleanup only removes files older than 24 hours.
- Startup changes use the same on/off switch as Task Manager, so they can be undone.
- No registry cleaning, no service tweaks and no driver changes.

## How to run

1. Download `PCCareKit.ps1` and `Start PC Care Kit.bat` into the same folder.
2. Double-click **Start PC Care Kit.bat**.
3. Click **Yes** when Windows asks for administrator permission. If you click **No**, it still runs, but some readings and all-user startup changes aren't available.

Windows may warn that the files came from the internet. Right-click each file, choose **Properties**, tick **Unblock**, then click **OK**.
