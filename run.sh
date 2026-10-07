#!/bin/zsh
# Run the sync manually, e.g. ./run.sh --dry-run
# Goes through CanvasReminders.app so the Reminders permission applies.
set -e
DIR="${0:A:h}"
OUT="$(mktemp -t canvas-reminders)"
open -W -g -n --stdout "$OUT" --stderr "$OUT" "$DIR/CanvasReminders.app" --args "$@"
cat "$OUT"
rm -f "$OUT"
