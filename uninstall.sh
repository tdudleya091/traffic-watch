#!/usr/bin/env bash
# Removes the Traffic Watch app-menu entry and icon. Asks before deleting your saved API keys.
# The app folder itself (with .venv and data/) is left for you to delete.
set -euo pipefail
CONFIG="${TRAFFIC_WATCH_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/traffic-watch}"

rm -f "$HOME/.local/share/applications/traffic-watch.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/traffic-watch.svg"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Removed the app-menu entry."

if [ -d "$CONFIG" ]; then
  read -r -p "Also delete your saved API keys and settings in $CONFIG? [y/N] " answer
  case "$answer" in
    [yY]*) rm -rf "$CONFIG"; echo "Deleted $CONFIG." ;;
    *) echo "Kept $CONFIG." ;;
  esac
fi
echo "To remove the app completely, delete this folder: $(cd "$(dirname "$0")" && pwd)"
