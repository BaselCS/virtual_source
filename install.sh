#!/usr/bin/env bash
# Install VirtualSource as a system desktop application for current user
set -e

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Creating required directories..."
mkdir -p ~/.local/bin
mkdir -p ~/.local/share/applications
mkdir -p ~/.local/share/icons/hicolor/48x48/apps
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
mkdir -p ~/.local/share/icons
mkdir -p ~/.local/share/pixmaps

echo "==> Installing icons..."
cp "$SCRIPT_DIR/assets/icon.png" ~/.local/share/icons/hicolor/48x48/apps/virtual-source.png
cp "$SCRIPT_DIR/assets/icon.png" ~/.local/share/icons/hicolor/scalable/apps/virtual-source.png
cp "$SCRIPT_DIR/assets/icon.png" ~/.local/share/icons/virtual-source.png
cp "$SCRIPT_DIR/assets/icon.png" ~/.local/share/pixmaps/virtual-source.png

echo "==> Installing launcher script..."
chmod +x "$SCRIPT_DIR/virtual-source"
cp "$SCRIPT_DIR/virtual-source" ~/.local/bin/virtual-source
chmod +x ~/.local/bin/virtual-source

echo "==> Installing desktop entry..."
cp "$SCRIPT_DIR/virtual-source.desktop" ~/.local/share/applications/virtual-source.desktop
chmod +x ~/.local/share/applications/virtual-source.desktop

echo "==> Updating desktop and icon database..."
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database ~/.local/share/applications || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
fi

echo "==> VirtualSource installed successfully!"
echo "You can now run 'virtual-source' from anywhere or launch 'VirtualSource' from your application menu."
