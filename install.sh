#!/bin/zsh
# Sets up the venv, builds CanvasReminders.app, and installs the hourly launchd job.
#   ./install.sh              install / reinstall
#   ./install.sh --build-only build venv + app bundle, skip launchd
#   ./install.sh --uninstall  remove the launchd job
set -e
DIR="${0:A:h}"
LABEL="com.pkroberg.canvas-reminders"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$DIR/CanvasReminders.app"
LOG="$HOME/Library/Logs/canvas-reminders.log"

if [[ "$1" == "--uninstall" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL (state and reminders left untouched)."
  exit 0
fi

[[ -f "$DIR/config.toml" ]] || { echo "Create config.toml first (cp config.example.toml config.toml)"; exit 1; }

# 1. venv
[[ -x "$DIR/.venv/bin/python" ]] || /opt/homebrew/bin/python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# 2. App bundle: gives macOS a bundle with a Reminders usage string to attribute
#    the permission to. A bare python binary can't show the permission prompt.
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>CanvasReminders</string>
  <key>CFBundleExecutable</key><string>CanvasReminders</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSRemindersUsageDescription</key><string>Syncs Canvas assignments into Reminders.</string>
  <key>NSRemindersFullAccessUsageDescription</key><string>Syncs Canvas assignments into Reminders.</string>
</dict>
</plist>
PLISTEOF
cat > "$APP/Contents/MacOS/CanvasReminders" <<EXEEOF
#!/bin/zsh
exec "$DIR/.venv/bin/python" "$DIR/canvas_to_reminders.py" "\$@"
EXEEOF
chmod +x "$APP/Contents/MacOS/CanvasReminders"
codesign -s - --force "$APP" 2>/dev/null
[[ "$1" == "--build-only" ]] && { echo "Built $APP"; exit 0; }

# 3. launchd job: hourly, plus at login. launchd also runs a missed interval after wake.
mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
sed -e "s#__APP__#$APP#g" -e "s#__LOG__#$LOG#g" "$DIR/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Installed $LABEL. First run is happening now; approve the Reminders prompt if one appears."
echo "Log: $LOG"
