#!/usr/bin/env bash
# Copy the working repo into the backup mirror ../Vidure-secondary-brain (plain files, no git history).
# Never edit the mirror directly. data/ is excluded (added by hand at the end); docs/, CLAUDE.md and LICENSE are not mirrored.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIRROR="$(dirname "$ROOT")/Vidure-secondary-brain"
mkdir -p "$MIRROR"
rsync -a --delete \
  --exclude '/.git/' \
  --exclude '/data/' \
  --exclude '/docs/' \
  --exclude '/CLAUDE.md' \
  --exclude '/LICENSE' \
  --exclude '/.venv/' \
  --exclude '/decision-brain/' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.DS_Store' \
  "$ROOT/" "$MIRROR/"
echo "mirror synced -> $MIRROR ($(date '+%Y-%m-%d %H:%M:%S'))"
