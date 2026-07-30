# Android Virtual Camera & Microphone

A PyQt6 GUI application for Arch Linux / PipeWire to bridge Android camera and microphone streams via `scrcpy` into a virtual webcam (`/dev/video0`) and virtual microphone (`Phone_Virtual_Microphone`).

For complete developer and agent documentation, see [summary.md](file:///home/basel/Work/python/virtual_source/summary.md).

## Quick Start

```bash
# 1. Load kernel module (run once per boot)
sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"

# 2. Run application
uv run python android_virtual_cam_mic.py
```
