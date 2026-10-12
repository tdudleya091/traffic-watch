#!/usr/bin/env bash
# Build a release zip from the committed tree, after a secret scan.
#   scripts/make_release.sh        -> dist/traffic-watch-<version>.zip + dist/RELEASE_NOTES-<version>.md
# Publishing (gh repo create / gh release create) is a separate, deliberate step.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' trafficwatch/__init__.py)"
NAME="traffic-watch-$VERSION"
CONFIG="${TRAFFIC_WATCH_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/traffic-watch}"

fail() { echo "RELEASE ABORTED: $*" >&2; exit 1; }

git rev-parse --git-dir >/dev/null 2>&1 || fail "not a git repository"
[ -z "$(git status --porcelain)" ] || fail "uncommitted changes; commit or stash them first"

# ---- secret scan ----------------------------------------------------------------
# 1) the actual key values on this machine must not appear in any tracked file
patterns="$(mktemp)"; trap 'rm -f "$patterns"' EXIT
for f in .env "$CONFIG/keys.env"; do
  [ -f "$f" ] || continue
  sed -n 's/^\(OPENSKY_CLIENT_ID\|OPENSKY_CLIENT_SECRET\|AISSTREAM_API_KEY\)=["]\{0,1\}\([^"]*\)["]\{0,1\}$/\2/p' "$f" \
    | awk 'length($0) >= 8' >> "$patterns"
done
if [ -s "$patterns" ] && git ls-files -z | xargs -0 grep -I -l -F -f "$patterns" -- 2>/dev/null; then
  fail "a real API key value appears in the tracked files listed above"
fi
# 2) no key-like assignments anywhere in tracked files (the .env.example template has empty values)
if git ls-files -z | xargs -0 grep -I -n -E \
    '(OPENSKY_CLIENT_SECRET|AISSTREAM_API_KEY|OPENSKY_CLIENT_ID)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_-]{8,}' -- 2>/dev/null; then
  fail "key-like assignment found in tracked files (above)"
fi
# 3) no secret files tracked
if git ls-files | grep -E '(^|/)\.env$|keys\.env$|credentials\.json$'; then
  fail "a secrets file is tracked by git"
fi
echo "Secret scan: clean."

# ---- build --------------------------------------------------------------------
mkdir -p dist
git archive --format=zip --prefix="$NAME/" -o "dist/$NAME.zip" HEAD
# double-check the archive itself
if [ -s "$patterns" ] && unzip -p "dist/$NAME.zip" | grep -a -q -F -f "$patterns"; then
  rm -f "dist/$NAME.zip"; fail "key value found inside the built zip"
fi
# release notes: the README changelog entry for this version
awk -v v="**$VERSION**" '$0 == v {on=1; next} on && /^\*\*[0-9]/ {exit} on' README.md > "dist/RELEASE_NOTES-$VERSION.md"
{ echo; echo "Install: unzip, run \`./install.sh\`, then open Traffic Watch from your app menu. See README for API keys."; } \
  >> "dist/RELEASE_NOTES-$VERSION.md"
echo "Built dist/$NAME.zip ($(du -h "dist/$NAME.zip" | cut -f1)) and dist/RELEASE_NOTES-$VERSION.md"
