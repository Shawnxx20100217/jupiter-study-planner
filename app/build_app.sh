#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUT_ROOT=${JUPITER_APP_OUT_DIR:-$(CDPATH= cd -- "$PLUGIN_ROOT/.." && pwd)}
OUT="$OUT_ROOT/Jupiter 作业管家.app"
CONTENTS="$OUT/Contents"
BIN="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
rm -rf "$OUT"
mkdir -p "$BIN" "$RES"
/usr/bin/swiftc "$SCRIPT_DIR/main.swift" -o "$BIN/JupiterStudyPlanner" -framework Cocoa
if [ -f "$SCRIPT_DIR/AppIcon.icns" ]; then
  cp "$SCRIPT_DIR/AppIcon.icns" "$RES/AppIcon.icns"
fi
cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>JupiterStudyPlanner</string>
<key>CFBundleIdentifier</key><string>local.jupiter.study-planner</string>
<key>CFBundleName</key><string>Jupiter 作业管家</string>
<key>CFBundleDisplayName</key><string>Jupiter 作业管家</string>
<key>CFBundleShortVersionString</key><string>0.3.0</string>
<key>CFBundleVersion</key><string>20260930</string>
<key>CFBundleIconFile</key><string>AppIcon.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
if command -v codesign >/dev/null 2>&1; then
  # Finder metadata on copied assets can make ad-hoc signing fail on macOS.
  xattr -cr "$OUT" 2>/dev/null || true
  codesign --force --deep --sign - "$OUT"
  codesign --verify --deep --strict "$OUT"
fi
printf '%s\n' "$OUT"
