#!/usr/bin/env bash
# Proves the computer can do its job: fonts resolve, the browser screenshots, HyperFrames lints
# and renders, ffprobe confirms the output. Exit 1 on any failure.
set -euo pipefail
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
probe_dir=/opt/design-agent/probe

for family in "Inter" "IBM Plex Mono" "Fira Code" "Fraunces"; do
  got="$(fc-match -f '%{family[0]}' "$family")"
  [ "$got" = "$family" ] || { echo "font did not resolve: $family -> $got" >&2; exit 1; }
done

chrome-headless-shell --no-sandbox --disable-gpu --hide-scrollbars --window-size=800,600 \
  --screenshot="$work/page.png" "file://$probe_dir/page.html" >/dev/null 2>&1
[ "$(head -c 8 "$work/page.png" | od -An -tx1 | tr -d ' \n')" = "89504e470d0a1a0a" ] \
  || { echo "browser screenshot failed" >&2; exit 1; }

mkdir -p "$work/film"
cp "$probe_dir/composition.html" "$work/film/index.html"
cp /opt/design-agent/render/node_modules/gsap/dist/gsap.min.js "$work/film/gsap.min.js"
hyperframes lint "$work/film" >/dev/null
hyperframes render "$work/film" --quality draft --fps 30 --output "$work/probe.mp4" --quiet >/dev/null
duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$work/probe.mp4")"
size="$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "$work/probe.mp4")"
python3 - "$duration" "$size" <<'PY'
import sys
duration, size = float(sys.argv[1]), sys.argv[2]
assert abs(duration - 2.0) <= 1 / 30 + 1e-6, f"duration {duration}"
assert size == "1920x1080", f"size {size}"
PY

printf 'shape: rectangle\na -> b\n' >"$work/probe.d2"
d2 --layout=dagre "$work/probe.d2" "$work/probe.svg" >/dev/null 2>&1
grep -q '<svg' "$work/probe.svg"

node -e "require('/opt/design-agent/render/node_modules/sharp'); require('/opt/design-agent/render/node_modules/echarts')"

echo "probe ok: node $(node --version), hyperframes $(hyperframes --version), d2 $(d2 --version)," \
  "$(ffmpeg -version | head -1 | cut -d' ' -f1-3), $(chrome-headless-shell --version 2>/dev/null | head -1)"
