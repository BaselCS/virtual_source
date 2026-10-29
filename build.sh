#!/usr/bin/env bash
# Build VirtualSource standalone binary using uv and PyInstaller
set -e

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Syncing dependencies with uv..."
uv sync

echo "==> Building standalone binary with PyInstaller..."
uv run --with pyinstaller pyinstaller \
    --noconfirm \
    --onedir \
    --windowed \
    --name virtual-source \
    --add-data "assets/icon.png:assets" \
    --icon "assets/icon.png" \
    android_virtual_cam_mic.py

echo "==> Build finished successfully! Output in dist/virtual-source"
