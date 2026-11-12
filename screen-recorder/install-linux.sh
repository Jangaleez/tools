#!/usr/bin/env bash
# Screen Recorder & Editor - Linux installer (Debian, Ubuntu, TUXEDO OS, Mint, Pop!_OS...)
# Installs what the app needs with apt, then adds it to your app menu.
# Run it from the folder it's in:   bash install-linux.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer is for Debian/Ubuntu-based systems (it uses apt)."
  exit 1
fi

PACKAGES=(ffmpeg python3-tk python3-gi pulseaudio-utils gstreamer1.0-tools gstreamer1.0-pipewire
          gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-pulseaudio xdg-desktop-portal
          xdg-user-dirs xdg-utils)

echo "Screen Recorder & Editor needs these packages:"
printf '  %s\n' "${PACKAGES[@]}"
echo
echo "Installing them with apt (you'll be asked for your password)..."
sudo apt-get update
sudo apt-get install -y "${PACKAGES[@]}"

chmod +x "$HERE/run-screen-recorder.sh"

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/screen-recorder.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Recorder & Editor
Comment=Record the screen, then trim, cut, speed up, rotate or join videos
Exec="$HERE/run-screen-recorder.sh"
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;Recorder;
StartupWMClass=screen-recorder
EOF
update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo
echo "Done. Open \"Screen Recorder & Editor\" from your app menu,"
echo "or run:  $HERE/run-screen-recorder.sh"
echo "(If you move this folder, run the installer again so the menu entry follows it.)"
