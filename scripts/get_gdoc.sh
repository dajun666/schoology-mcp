#!/usr/bin/env bash
#
# Download a single Google Docs/Sheets/Slides file by URL (or bare file ID).
#
#   ./scripts/get_gdoc.sh 'https://docs.google.com/document/d/<file-id>/edit?tab=t.0'
#   ./scripts/get_gdoc.sh <file-id>
#   ./scripts/get_gdoc.sh -f pdf -o ~/Desktop '<url>'
#
# Native Google files have no download URL — they must be exported through the
# Drive API, which is what `rclone backend copyid` does. The doc may live in any
# of the configured accounts (or merely be shared with one), so every remote is
# tried until one can read it. See scripts/backup_gdocs.sh for bulk export.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RCLONE="$ROOT/tools/rclone"
export RCLONE_CONFIG="$ROOT/tools/rclone.conf"

REMOTES=(gdrive2 gdrive)   # tried in order
OUT="$ROOT/gdocs-single"
FORMAT=""                  # empty -> inferred from the URL's app

usage() { sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -f|--format) FORMAT="$2"; shift 2 ;;
    -o|--out)    OUT="$2";    shift 2 ;;
    -h|--help)   usage ;;
    -*)          echo "unknown option: $1" >&2; usage ;;
    *)           break ;;
  esac
done
[ $# -eq 1 ] || usage
INPUT="$1"

# URL shapes: /document/d/<id>/edit, /spreadsheets/d/<id>, /file/d/<id>/view,
# /open?id=<id>, /uc?id=<id>. Anything that is already just an id passes through.
case "$INPUT" in
  *"/d/"*)  ID="${INPUT#*/d/}"; ID="${ID%%/*}"; ID="${ID%%\?*}" ;;
  *"id="*)  ID="${INPUT#*id=}"; ID="${ID%%&*}"; ID="${ID%%#*}" ;;
  *)        ID="$INPUT" ;;
esac
case "$ID" in
  *[!A-Za-z0-9_-]*|"") echo "无法从输入中解析文件 ID: $INPUT" >&2; exit 1 ;;
esac

# Docs export to markdown; Sheets/Slides have no markdown form, so pick the
# format Google actually offers for that app.
if [ -z "$FORMAT" ]; then
  case "$INPUT" in
    *spreadsheets*) FORMAT=xlsx ;;
    *presentation*) FORMAT=pptx ;;
    *)              FORMAT=md ;;
  esac
fi

[ -x "$RCLONE" ] || { echo "rclone not found at $RCLONE" >&2; exit 1; }
mkdir -p "$OUT"
before="$(mktemp)"; after="$(mktemp)"
trap 'rm -f "$before" "$after"' EXIT
ls -A "$OUT" > "$before"

echo "文件 ID: $ID"
echo "导出格式: $FORMAT"
err=""
for r in "${REMOTES[@]}"; do
  printf '尝试 %s: ... ' "$r"
  # a trailing slash on the destination tells copyid to keep the original title
  if err="$("$RCLONE" backend copyid "$r:" --drive-export-formats "$FORMAT" --tpslimit 8 \
             "$ID" "$OUT/" 2>&1)"; then
    echo "✓"
    ls -A "$OUT" > "$after"
    # copyid prints nothing, so recover the title it chose: a new directory
    # entry, or — when overwriting an earlier download — the freshest file
    name="$(comm -13 "$before" "$after" | head -1)"
    [ -n "$name" ] || name="$(ls -t "$OUT" | head -1)"
    echo "→ $OUT/$name"
    [ -f "$OUT/$name" ] && du -h "$OUT/$name" | cut -f1 | xargs -I{} echo "  大小 {}"
    exit 0
  fi
  echo "✗"
done

echo "所有账号都读不到这个文件。" >&2
echo "$err" | tail -3 >&2
# 403 here means the owner turned off "viewers can download, print, copy" —
# no API can export it; the text has to be copied out of the browser by hand.
exit 1
