#!/usr/bin/env bash
# Traffic Watch installer (Linux, user-level; never uses sudo).
#  1. checks for the system GTK/WebKit packages the app window needs
#  2. creates .venv and installs the Python packages
#  3. downloads the aircraft / airline / ship-flag lookups
#  4. adds "Traffic Watch" to your app menu
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"   # so the app's own package imports work wherever the script is started from
PY="${TW_SYSTEM_PYTHON:-/usr/bin/python3}"   # the system Python: the GTK bindings are compiled for it

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---- 1. system packages -------------------------------------------------------
missing=()
[ -x "$PY" ] || missing+=(python3)
"$PY" -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3-venv)
"$PY" -c 'import gi' 2>/dev/null || missing+=(python3-gi)
"$PY" -c 'import gi; gi.require_version("WebKit2", "4.1"); from gi.repository import WebKit2' 2>/dev/null \
  || missing+=(gir1.2-webkit2-4.1)
if [ ${#missing[@]} -gt 0 ]; then
  say "Traffic Watch needs a few system packages first. Install them with:"
  echo "    sudo apt install ${missing[*]}"
  echo "(Fedora: sudo dnf install python3-gobject webkit2gtk4.1 · Arch: sudo pacman -S python-gobject webkit2gtk-4.1)"
  echo "Then run ./install.sh again."
  exit 1
fi

# ---- 2. Python environment ----------------------------------------------------
if [ ! -x "$DIR/.venv/bin/python" ]; then
  say "Creating the Python environment ..."
  "$PY" -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
# pywebview's GTK backend uses the system PyGObject; link just those modules into the venv.
SP="$("$DIR/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
GI_DIR="$("$PY" -c 'import gi, os; print(os.path.dirname(gi.__path__[0]))')"
for m in gi cairo; do
  [ -e "$GI_DIR/$m" ] && ln -sfn "$GI_DIR/$m" "$SP/$m"
done
"$DIR/.venv/bin/python" -c 'import gi, webview, folium' || { echo "Python packages failed to import."; exit 1; }

# ---- 3. reference data ----------------------------------------------------------
say "Downloading aircraft, airline and ship-flag lookups (about 10 MB) ..."
[ -f "$DIR/data/ref/roads.json" ] || "$DIR/.venv/bin/python" "$DIR/scripts/build_reference.py"
"$DIR/.venv/bin/python" -c 'from trafficwatch.enrich import refresh_reference; refresh_reference()'

# ---- 4. app menu entry ----------------------------------------------------------
mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$DIR/assets/traffic-watch.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/traffic-watch.svg"
cat > "$HOME/.local/share/applications/traffic-watch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Traffic Watch
Comment=Live aviation and maritime traffic around US and Japanese ports
Exec="$DIR/.venv/bin/python" "$DIR/app.py"
Path=$DIR
Icon=traffic-watch
Terminal=false
Categories=Network;
StartupWMClass=Traffic Watch
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

say "Installed. Open 'Traffic Watch' from your app menu (or run: $DIR/.venv/bin/python $DIR/app.py)."
echo "The first launch asks for your free API keys, or lets you run without keys."
