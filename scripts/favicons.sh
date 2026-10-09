#!/usr/bin/env bash
# Render the tab icon frontend/public/favicon.svg into the files for browsers that don't take an SVG icon:
# favicon.ico (16 + 32 px, also what a page without icon links gets, like the guides) and apple-touch-icon.png
# (180 px, a home-screen icon). Uses Google Chrome, headless, to draw the SVG.
#
#   ./scripts/favicons.sh        # after changing favicon.svg; commit the files it writes
set -euo pipefail

cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PUBLIC=frontend/public
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() {  # render <size> <out.png>
  printf '<html><body style="margin:0"><img src="file://%s" width="%s" height="%s"></body></html>' \
    "$PWD/$PUBLIC/favicon.svg" "$1" "$1" > "$TMP/icon.html"
  "$CHROME" --headless --disable-gpu --hide-scrollbars --user-data-dir="$TMP/profile" --allow-file-access-from-files \
    --default-background-color=00000000 --force-device-scale-factor=1 --window-size="$1,$1" \
    --screenshot="$2" "file://$TMP/icon.html" >/dev/null 2>&1
}

render 16 "$TMP/16.png"
render 32 "$TMP/32.png"
render 180 "$PUBLIC/apple-touch-icon.png"

# An .ico may hold PNG images as they are: a 6-byte header, a 16-byte entry per image, then the images
python3 - "$TMP/16.png" "$TMP/32.png" "$PUBLIC/favicon.ico" <<'EOF'
import struct, sys
pngs = [open(p, "rb").read() for p in sys.argv[1:3]]
sizes, offset, entries = (16, 32), 6 + 16 * len(pngs), b""
for size, data in zip(sizes, pngs):
    entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
with open(sys.argv[3], "wb") as f:
    f.write(struct.pack("<HHH", 0, 1, len(pngs)) + entries + b"".join(pngs))
EOF
ls -l "$PUBLIC"/favicon* "$PUBLIC"/apple-touch-icon.png
