# Android Virtual Camera & Microphone

A PyQt6 GUI application for Arch Linux / PipeWire to bridge Android camera and microphone streams via `scrcpy` into a virtual webcam (`/dev/video0`) and virtual microphone (`Phone_Virtual_Microphone`).

For complete developer and agent documentation, see [summary.md](file:///home/basel/Work/python/virtual_source/summary.md).

## Requirements

To run this application, ensure the following are installed on your Arch Linux system:

### System Packages
- **`scrcpy`**: For mirroring Android video and audio (`sudo pacman -S scrcpy`).
- **`android-tools`**: Provides `adb` for Android device communication (`sudo pacman -S android-tools`).
- **`v4l2loopback-dkms`**: Kernel module for the virtual webcam (`sudo pacman -S v4l2loopback-dkms`).
- **PipeWire / PulseAudio Utils**: Standard on modern Arch installs. Specifically requires `pw-cli`, `pw-link`, and `pactl`.

### Python Environment
- **Python 3.10+**: Standard Python interpreter.
- **`uv`**: Fast Python package installer and runner (`sudo pacman -S uv` or install via curl).
- **`PyQt6`**: Python GUI framework (automatically installed when running via `uv run` if specified in a `pyproject.toml`, or install manually if needed).

## Quick Start

```bash
# 1. Load kernel module (run once per boot)
sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"

# 2. Run application
uv run python android_virtual_cam_mic.py
```
