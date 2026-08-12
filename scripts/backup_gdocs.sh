#!/usr/bin/env bash
#
# Export every Google Doc from the configured Drive accounts as Markdown.
#
#   ./scripts/backup_gdocs.sh            # all accounts
#   ./scripts/backup_gdocs.sh gdrive2    # just one
#
# Google Drive allows several files to share one name inside a folder; a local
# filesystem does not. Plain `rclone copy` resolves that by keeping the first
# one and logging "Duplicate object found in source - ignoring", silently
# dropping the rest. So after the bulk copy this script re-fetches every
# duplicated path by file ID under a "<name> (YYYY-MM-DD).md" name, then drops
# the ambiguous plain-named file once its checksum matches one of the dated
# copies. Re-running is safe: the copy is incremental (modtime-based) and the
# duplicate pass is idempotent.
#
# Credentials live in tools/rclone.conf (git-ignored). Set up a new account with:
#   RCLONE_CONFIG=$PWD/tools/rclone.conf ./tools/rclone config
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RCLONE="$ROOT/tools/rclone"
export RCLONE_CONFIG="$ROOT/tools/rclone.conf"

# remote | destination directory (relative to repo root) | extra rclone flags
# Files other people shared with you live outside your own Drive tree, so they
# need a second pass over the same remote with --drive-shared-with-me.
ACCOUNTS=(
  "gdrive|gdocs-backup|"
  "gdrive2|gdocs-backup-school|"
  "gdrive2|gdocs-backup-school-shared|--drive-shared-with-me"
)

# --include "*.md" filters on the name *after* the export extension is added,
# so this restricts the run to Google Docs and skips Sheets/Slides/binaries.
# --tpslimit stays under Drive's ~10 transactions/second quota.
COMMON=(
  --drive-export-formats md
  --include "*.md"
  --drive-skip-shortcuts
  --tpslimit 8
  --transfers 4
  --checkers 8
)

[ -x "$RCLONE" ] || { echo "rclone not found at $RCLONE" >&2; exit 1; }
[ -f "$RCLONE_CONFIG" ] || { echo "no rclone config at $RCLONE_CONFIG" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

checksum() {
  if command -v md5 >/dev/null 2>&1; then md5 -q "$1"; else md5sum "$1" | cut -d' ' -f1; fi
}

backup_account() {
  local remote="$1" dest="$ROOT/$2"
  local list="$TMP/$2.tsv" dups="$TMP/$2.dups"
  local extra=(); [ -n "${3:-}" ] && extra=("$3")

  echo "==> $remote: ${3:-} -> ${2}"
  mkdir -p "$dest"
  # a doc whose owner disabled download/copy returns 403 and is skipped; the
  # rest of the batch still lands, so don't let one failure abort the run
  "$RCLONE" copy "$remote:" "$dest" "${COMMON[@]}" "${extra[@]}" \
    --stats-one-line --stats 10s || echo "    ⚠ 部分文档导出失败（见上方 ERROR 行）"

  "$RCLONE" lsf -R "$remote:" "${COMMON[@]}" "${extra[@]}" \
    --files-only --format "ipt" --separator $'\t' > "$list"
  cut -f2 "$list" | sort | uniq -d > "$dups"

  local n_dups
  n_dups=$(wc -l < "$dups" | tr -d ' ')
  [ "$n_dups" -gt 0 ] && echo "    重名路径 $n_dups 组，按文件 ID 拆分:"

  local path base id p t day out plain sum f
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    base="${path%.md}"

    # one local file per Drive version, disambiguated by modification date
    while IFS=$'\t' read -r id p t; do
      [ "$p" = "$path" ] || continue
      day="${t%% *}"
      out="$dest/$base ($day).md"
      mkdir -p "$(dirname "$out")"
      "$RCLONE" backend copyid "$remote:" --drive-export-formats md --tpslimit 8 "$id" "$out"
      echo "      ✓ $base ($day).md"
    done < "$list"

    # the bulk copy left one arbitrary version under the bare name; drop it
    # only once a dated copy is confirmed byte-identical
    plain="$dest/$path"
    if [ -f "$plain" ]; then
      sum="$(checksum "$plain")"
      for f in "$dest/$base ("*").md"; do
        [ -f "$f" ] || continue
        if [ "$(checksum "$f")" = "$sum" ]; then rm -f "$plain"; break; fi
      done
    fi
  done < "$dups"

  echo "    本地 $(find "$dest" -name '*.md' | wc -l | tr -d ' ') 篇 / Drive $(wc -l < "$list" | tr -d ' ') 篇"
}

for entry in "${ACCOUNTS[@]}"; do
  IFS='|' read -r remote dest extra <<< "$entry"
  if [ $# -gt 0 ] && [ "$1" != "$remote" ] && [ "$1" != "$dest" ]; then continue; fi
  backup_account "$remote" "$dest" "$extra"
done
