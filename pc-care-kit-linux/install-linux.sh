#!/usr/bin/env bash
# PC Care Kit - Linux installer (Debian, Ubuntu, TUXEDO OS, Mint, Pop!_OS...)
# Installs the few small packages it uses with apt, then adds it to your app menu.
# Run it from the folder it's in:   bash install-linux.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer is for Debian/Ubuntu-based systems (it uses apt)."
  exit 1
fi

# pkexec asks for your password in a normal window when an action needs admin rights.
# Its package is called "pkexec" on newer systems and "policykit-1" on older ones.
PKEXEC=pkexec
if ! apt-cache show pkexec >/dev/null 2>&1; then PKEXEC=policykit-1; fi
PACKAGES=(python3-tk "$PKEXEC" smartmontools libnotify-bin xdg-utils)

echo "PC Care Kit needs these packages:"
printf '  %s\n' "${PACKAGES[@]}"
echo
echo "Installing them with apt (you'll be asked for your password)..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${PACKAGES[@]}"

chmod +x "$HERE/run-pc-care-kit.sh"

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/pc-care-kit.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PC Care Kit
Comment=PC health checks, cleanup, startup apps, updates and maintenance
Exec="$HERE/run-pc-care-kit.sh"
Icon=preferences-system
Terminal=false
Categories=System;Utility;
StartupWMClass=pc-care-kit
DESKTOP
update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo
echo "Done. Open \"PC Care Kit\" from your app menu,"
echo "or run:  $HERE/run-pc-care-kit.sh"
echo "(If you move this folder, run the installer again so the menu entry follows it.)"
