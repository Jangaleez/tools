# Tools

Free, home-grown tools by [Jangaleez](https://github.com/Jangaleez) for **Windows** and **Linux** (Debian, Ubuntu, TUXEDO OS, Kubuntu, Mint and similar). Each folder is a separate tool with its own instructions.

| Tool | What it is | Windows | Linux |
|---|---|---|---|
| **PC Care Kit** | A tabbed app for PC health and maintenance: startup apps, cleanup, disk space, updates and repairs | [pc-care-kit](pc-care-kit/) (no install needed) | [pc-care-kit-linux](pc-care-kit-linux/) |
| **Screen Recorder & Editor** | Record the screen, then trim, cut, speed up, rotate or join videos | [screen-recorder](screen-recorder/) | Same folder: run `install-linux.sh` |

## Downloading

**Windows:** open the tool's folder above, click each file, then click the **download** button (↓) at the top right of the file view. Keep all of a tool's files in the same folder. To get everything at once, use the green **Code** button → **Download ZIP**.

**Linux:** in a terminal, run:
```
git clone https://github.com/Jangaleez/tools.git
cd tools/screen-recorder && bash install-linux.sh        # Screen Recorder & Editor
cd ../pc-care-kit-linux && bash install-linux.sh         # PC Care Kit
```
Both installers use `apt` and add the tool to your app menu. To update later, run `git pull` in the `tools` folder.

## Licence

MIT. Use, change and share freely. See [LICENSE](LICENSE).
