# Project Summary: Android Virtual Camera & Microphone

This document provides a comprehensive overview of the `virtual-source` codebase for AI agents, developers, and maintainers.

---

## 📌 Project Overview

- **Repository:** `virtual-source`
- **Primary Script:** [`android_virtual_cam_mic.py`](file:///home/basel/Work/python/virtual_source/android_virtual_cam_mic.py)
- **Target OS:** Linux (Arch Linux / PipeWire / PulseAudio)
- **Environment & Package Manager:** Python $\ge$ 3.12 managed via `uv`

`virtual-source` is a PyQt6 desktop application that transforms an Android device into a high-quality Linux virtual webcam (`/dev/video0`) and virtual microphone (`Phone_Virtual_Microphone`) using `scrcpy` and `v4l2loopback`.

---

## 🛠️ Architecture & Key Components

```
+-----------------------------------------------------------------------------------+
|                                 PyQt6 GUI (MainWindow)                           |
|  - Mode Selector (Video+Audio / Video Only / Audio Only)                          |
|  - Dynamic Camera Dropdown & Refresh Button (scrcpy --list-cameras)              |
|  - Live Log Console & Status Monitor                                             |
+----------------------------------------+------------------------------------------+
                                         |
                                  spawns | StreamWorker (QThread)
                                         v
+-----------------------------------------------------------------------------------+
|                               Linux System Pipelines                              |
|                                                                                   |
|  [ Video ]  scrcpy --video-source=camera --camera-id=X ----> /dev/video0          |
|                                                              (v4l2loopback)       |
|                                                                                   |
|  [ Audio ]  scrcpy --audio-source=mic                                             |
|                   |                                                               |
|                   v                                                               |
|             Phone_Audio_Sink (module-null-sink)                                   |
|                   |                                                               |
|                   v (master=Phone_Audio_Sink.monitor)                             |
|             Phone_Virtual_Microphone (module-remap-source)                        |
|                   |                                                               |
|                   +---> Input Device for Discord, OBS, Google Meet, WebRTC, etc.   |
+-----------------------------------------------------------------------------------+
```

---

## 🔑 Crucial Knowledge for AI Agents

### 1. Audio Architecture (PipeWire / PulseAudio Integration)
- **Null Sink (`Phone_Audio_Sink`):** Created via `pactl load-module module-null-sink` with explicit `media.class="Audio/Sink"`. Receives audio stream output from `scrcpy`.
- **Virtual Microphone (`Phone_Virtual_Microphone`):** Created via `pactl load-module module-remap-source master=Phone_Audio_Sink.monitor` with `media.class="Audio/Source"`, `device.class="sound"`, and `device.icon_name="audio-input-microphone"`. These explicit PipeWire metadata attributes force the OS, web browsers (WebRTC/Chrome/Firefox), Discord, and OBS to recognize `Phone_Virtual_Microphone` as a active hardware-equivalent audio input device.
- **Audio Source Selection:** Supports selecting Android `--audio-source=mic` (Phone Microphone) or `--audio-source=output` (Internal Device Audio) in the GUI, with low-latency Opus audio encoding (`--audio-codec=opus --audio-buffer=50`).
- **Default System Microphone Assignment:** Optionally runs `pactl set-default-source Phone_Virtual_Microphone` upon stream activation.
- **Why NOT `pw-loopback node.passive=true`:** Using `pw-loopback` with `node.passive=true` caused PipeWire to suspend the audio graph unless an external active driver was running. `module-remap-source` with proper `media.class="Audio/Source"` provides a native, active Virtual Source that works out-of-the-box for all recording apps.
- **Stale Module Cleanup:** `_cleanup_stale_audio_modules()` runs on startup and teardown to prevent orphaned audio nodes from previous crashes.

### 2. Camera Discovery & Scrcpy CLI API
- **Discovery (`list_android_cameras()`):** Runs `scrcpy --list-cameras` and parses output matching `--camera-id=<id>` to extract camera ID, facing orientation (`front`/`back`), and sensor resolution (`4080x3060`, etc.).
- **Flag Construction (`_scrcpy_cmd()`):** Appends `--camera-id=<id>` when a specific camera sensor is selected, or `--camera-facing=<front|back>` for fallbacks.

### 3. System Requirements & External Binaries
- **`scrcpy` (v4.1+):** For video and microphone capturing from Android 12+ devices over ADB.
- **`v4l2loopback`:** Kernel module supplying `/dev/video0`. Command to load:
  ```bash
  sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"
  ```
- **`pactl` & `pw-cli` / `pw-link`:** For PipeWire audio module creation and routing.

---

## 📂 File Directory

| File | Description |
| :--- | :--- |
| [`android_virtual_cam_mic.py`](file:///home/basel/Work/python/virtual_source/android_virtual_cam_mic.py) | Full application source code (GUI, worker thread, camera parser, audio module manager). |
| [`pyproject.toml`](file:///home/basel/Work/python/virtual_source/pyproject.toml) | Project dependencies (`PyQt6`). |
| [`uv.lock`](file:///home/basel/Work/python/virtual_source/uv.lock) | Lockfile for environment dependencies. |
| [`summary.md`](file:///home/basel/Work/python/virtual_source/summary.md) | Agent & developer overview documentation. |

---

## 🚀 How to Run & Test

```bash
# 1. Load v4l2loopback kernel module (run once per boot)
sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"

# 2. Run application via uv
uv run python android_virtual_cam_mic.py
```
