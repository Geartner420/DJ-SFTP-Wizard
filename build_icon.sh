#!/bin/bash
set -e

cd "$(dirname "$0")"

SOURCE_SVG="assets/app_icon.svg"
SOURCE_PNG="assets/app_icon.png"
ICONSET_DIR="build/DJ SFTP Wizard.iconset"
PNG_1024="build/app_icon_1024.png"
ICNS_PATH="assets/app_icon.icns"

mkdir -p build assets
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"

if [ -f "$SOURCE_PNG" ]; then
  cp "$SOURCE_PNG" "$PNG_1024"
elif [ -f "$SOURCE_SVG" ]; then
  qlmanage -t -s 1024 -o build "$SOURCE_SVG" >/dev/null 2>&1
  mv "build/$(basename "$SOURCE_SVG").png" "$PNG_1024"
else
  echo "Kein Icon-Quellbild gefunden."
  echo "Erwartet wird assets/app_icon.png oder assets/app_icon.svg"
  exit 1
fi

sips -z 16 16 "$PNG_1024" --out "$ICONSET_DIR/icon_16x16.png" >/dev/null
sips -z 32 32 "$PNG_1024" --out "$ICONSET_DIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$PNG_1024" --out "$ICONSET_DIR/icon_32x32.png" >/dev/null
sips -z 64 64 "$PNG_1024" --out "$ICONSET_DIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$PNG_1024" --out "$ICONSET_DIR/icon_128x128.png" >/dev/null
sips -z 256 256 "$PNG_1024" --out "$ICONSET_DIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$PNG_1024" --out "$ICONSET_DIR/icon_256x256.png" >/dev/null
sips -z 512 512 "$PNG_1024" --out "$ICONSET_DIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$PNG_1024" --out "$ICONSET_DIR/icon_512x512.png" >/dev/null
cp "$PNG_1024" "$ICONSET_DIR/icon_512x512@2x.png"

iconutil -c icns "$ICONSET_DIR" -o "$ICNS_PATH"

echo "Fertig: $ICNS_PATH"
