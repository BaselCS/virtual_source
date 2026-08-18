#!/usr/bin/python3
"""
Android Virtual Webcam & Microphone — PyQt6 GUI for PipeWire/Arch Linux.
VirtualSource Console Theme matching modern dark dashboard layout.
"""

import os
import select
import subprocess
import sys
import ctypes
import signal
from datetime import datetime

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QTextEdit,
    QMessageBox,
    QComboBox,
    QFrame,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SINK_NAME = "Phone_Audio_Sink"
REMAP_NAME = "Phone_Virtual_Microphone"

MODE_VIDEO_AUDIO = "Video + Audio"
MODE_VIDEO_ONLY = "Video Only"
MODE_AUDIO_ONLY = "Audio Only"

AUDIO_SRC_MIC = "Microphone (Phone Mic)"
AUDIO_SRC_OUTPUT = "Internal Audio (Device Apps)"


def _set_pdeathsig():
    """Ensure child process is killed when parent dies (Linux only)."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(1, signal.SIGKILL)
    except Exception:
        pass


def list_android_cameras() -> list[tuple[str, str]]:
    """
    Run `scrcpy --list-cameras` and return a list of (camera_id, display_label).
    """
    cameras = []
    try:
        proc = subprocess.run(
            ["scrcpy", "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in output.splitlines():
            line_str = line.strip()
            if "--camera-id=" in line_str:
                parts = line_str.split("--camera-id=")
                if len(parts) > 1:
                    rest = parts[1].strip()
                    cam_id = rest.split()[0]

                    label = f"Camera {cam_id}"
                    if "(" in rest:
                        inside = rest[rest.find("(") + 1 :]
                        if ")" in inside:
                            inside = inside[: inside.rfind(")")]
                        details = []
                        for chunk in inside.split(","):
                            item = chunk.strip()
                            if item in ("front", "back", "external") or "x" in item:
                                details.append(item)
                        if details:
                            label += f" ({', '.join(details)})"

                    cameras.append((cam_id, label))
    except Exception:
        pass
    return cameras


def _scrcpy_cmd(
    mode: str,
    no_preview: bool = False,
    camera_id: str | None = None,
    audio_source: str = "mic",
    flip_h: bool = False,
    flip_v: bool = False,
) -> list[str]:
    cmd = ["scrcpy"]
    cam_flags = []
    if camera_id and camera_id not in ("front", "back", "default"):
        cam_flags = [f"--camera-id={camera_id}"]
    elif camera_id == "back":
        cam_flags = ["--camera-facing=back"]
    else:
        cam_flags = ["--camera-facing=front"]

    if flip_h and flip_v:
        cam_flags.append("--capture-orientation=180")
    elif flip_h:
        cam_flags.append("--capture-orientation=flip0")
    elif flip_v:
        cam_flags.append("--capture-orientation=flip180")

    audio_src_flag = f"--audio-source={audio_source}"

    if mode == MODE_VIDEO_ONLY:
        cmd += [
            "--video-source=camera",
            *cam_flags,
            "--v4l2-sink=/dev/video0",
            "--no-audio",
        ]
    elif mode == MODE_AUDIO_ONLY:
        cmd += [
            "--no-video",
            audio_src_flag,
            "--audio-codec=opus",
            "--audio-buffer=50",
        ]
    else:
        cmd += [
            "--video-source=camera",
            *cam_flags,
            "--v4l2-sink=/dev/video0",
            audio_src_flag,
            "--audio-codec=opus",
            "--audio-buffer=50",
        ]

    if no_preview:
        cmd.append("--no-window")

    return cmd


def _need_audio(mode: str) -> bool:
    return mode != MODE_VIDEO_ONLY


# ---------------------------------------------------------------------------
# Audio Helpers
# ---------------------------------------------------------------------------
def _run_pactl(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip() or str(exc)
        raise RuntimeError(f"pactl {' '.join(args)} failed: {msg}") from exc
    return proc.stdout.strip()


def _list_sources() -> str:
    try:
        out = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except Exception as exc:
        return f"  (error listing sources: {exc})"


def _cleanup_stale_audio_modules() -> list[str]:
    """Unload any leftover Phone_Audio_Sink or Phone_Virtual_Microphone modules."""
    unloaded = []
    try:
        out = subprocess.run(
            ["pactl", "list", "modules", "short"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                mod_id = parts[0]
                mod_args = line.lower()
                if "phone_audio_sink" in mod_args or "phone_virtual_microphone" in mod_args:
                    try:
                        _run_pactl(["unload-module", mod_id])
                        unloaded.append(mod_id)
                    except Exception:
                        pass
    except Exception:
        pass
    return unloaded


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
class StreamWorker(QThread):
    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scrcpy_proc: subprocess.Popen | None = None
        self._null_sink_module_id: int | None = None
        self._remap_source_module_id: int | None = None
        self._pw_loopback_proc: subprocess.Popen | None = None
        self._loopback_module_id: int | None = None
        self._running = False
        self._mode = MODE_VIDEO_AUDIO
        self._no_preview = False
        self._stop_mic_preview = False
        self._camera_id: str | None = None
        self._audio_source: str = "mic"
        self._set_default_mic: bool = False
        self._mute_speaker_preview: bool = True
        self._restart_requested: bool = False
        self._flip_h: bool = False
        self._flip_v: bool = False

    def start_stream(
        self,
        mode: str,
        camera_id: str | None = None,
        audio_source: str = "mic",
        flip_h: bool = False,
        flip_v: bool = False,
        no_preview: bool = False,
        stop_mic_preview: bool = False,
    ) -> None:
        self._mode = mode
        self._no_preview = no_preview
        self._stop_mic_preview = stop_mic_preview
        self._camera_id = camera_id
        self._audio_source = audio_source
        self._flip_h = flip_h
        self._flip_v = flip_v
        self._set_default_mic = False
        self._mute_speaker_preview = True
        self._running = True
        self.start()

    def stop_stream(self) -> None:
        self._running = False
        self._kill_scrcpy()
        self._unload_modules()

    def stop_cam_preview(self) -> None:
        if not self._no_preview:
            self.log_message.emit("[SCRCPY] Stopping camera preview (restarting headless)...")
            self._no_preview = True
            self._restart_requested = True
            if self._scrcpy_proc:
                self._scrcpy_proc.terminate()

    def start_cam_preview(self) -> None:
        if self._no_preview:
            self.log_message.emit("[SCRCPY] Starting camera preview (restarting with window)...")
            self._no_preview = False
            self._restart_requested = True
            if self._scrcpy_proc:
                self._scrcpy_proc.terminate()

    def update_flip(self, flip_h: bool, flip_v: bool) -> None:
        if self._flip_h == flip_h and self._flip_v == flip_v:
            return
        self._flip_h = flip_h
        self._flip_v = flip_v
        if self._running and self._scrcpy_proc:
            self.log_message.emit("[SCRCPY] Restarting to apply new orientation...")
            self.status_changed.emit("Restarting…")
            self._restart_requested = True
            self._scrcpy_proc.terminate()

    def stop_mic_preview(self) -> None:
        self._stop_mic_preview = True
        if self._loopback_module_id is not None:
            self.log_message.emit("[AUDIO] Stopping microphone preview...")
            try:
                _run_pactl(["unload-module", str(self._loopback_module_id)])
            except Exception as exc:
                self.log_message.emit(f"[AUDIO] Warning: {exc}")
            self._loopback_module_id = None

    def start_mic_preview(self) -> None:
        self._stop_mic_preview = False
        if self._running and self._loopback_module_id is None and _need_audio(self._mode):
            self.log_message.emit("[AUDIO] Starting audio preview (loopback to speakers) …")
            try:
                out_loopback = _run_pactl(
                    [
                        "load-module",
                        "module-loopback",
                        f"source={REMAP_NAME}",
                        "latency_msec=50",
                    ]
                )
                self._loopback_module_id = int(out_loopback)
            except Exception as exc:
                self.log_message.emit(f"[AUDIO] Warning creating loopback: {exc}")

    def run(self) -> None:
        try:
            if _need_audio(self._mode):
                self._setup_audio()
                self._dump_audio_state()
            else:
                self.log_message.emit("[AUDIO] Skipped")

            while self._running:
                self._start_scrcpy()
                self._monitor_scrcpy()
                if self._restart_requested:
                    self._restart_requested = False
                    continue
                else:
                    break
        except RuntimeError as exc:
            self.log_message.emit(f"[ERROR] {exc}")
            self.error_occurred.emit(str(exc))
        except Exception as exc:
            self.log_message.emit(f"[ERROR] Unexpected: {exc}")
            self.error_occurred.emit(f"Unexpected error: {exc}")
        finally:
            self._unload_modules()
            self.finished.emit()

    def _setup_audio(self) -> None:
        stale = _cleanup_stale_audio_modules()
        if stale:
            self.log_message.emit(f"[AUDIO] Cleaned up {len(stale)} stale module(s)")

        self.log_message.emit("[AUDIO] Creating null sink …")
        sink_props = (
            f"device.description=\"{SINK_NAME}\" "
            f"media.class=\"Audio/Sink\" "
            f"device.icon_name=\"audio-sink\""
        )
        out = _run_pactl(
            [
                "load-module",
                "module-null-sink",
                f"sink_name={SINK_NAME}",
                f"sink_properties={sink_props}",
            ]
        )
        self._null_sink_module_id = int(out)
        self.log_message.emit(f"  → null sink module id {self._null_sink_module_id}")

        self.log_message.emit("[AUDIO] Creating virtual microphone source …")
        source_props = (
            f"device.description=\"{REMAP_NAME}\" "
            f"media.class=\"Audio/Source\" "
            f"device.class=\"sound\" "
            f"device.icon_name=\"audio-input-microphone\""
        )
        out_remap = _run_pactl(
            [
                "load-module",
                "module-remap-source",
                f"master={SINK_NAME}.monitor",
                f"source_name={REMAP_NAME}",
                f"source_properties={source_props}",
            ]
        )
        self._remap_source_module_id = int(out_remap)
        self.log_message.emit(
            f"  → remap source module id {self._remap_source_module_id}"
        )

        if not self._stop_mic_preview:
            self.log_message.emit("[AUDIO] Starting audio preview (loopback to speakers) …")
            try:
                out_loopback = _run_pactl(
                    [
                        "load-module",
                        "module-loopback",
                        f"source={REMAP_NAME}",
                        "latency_msec=50",
                    ]
                )
                self._loopback_module_id = int(out_loopback)
            except Exception as exc:
                self.log_message.emit(f"[AUDIO] Warning creating loopback: {exc}")
        else:
            self.log_message.emit("[AUDIO] Audio preview skipped (Stop Mic Preview is ON)")

    def _dump_audio_state(self) -> None:
        self.log_message.emit("[AUDIO] PipeWire nodes:")
        for node in self._get_virtual_nodes():
            self.log_message.emit(f"  {node}")
        self.log_message.emit("[AUDIO] PulseAudio sources:")
        for line in _list_sources().splitlines():
            self.log_message.emit(f"  {line}")
        self.log_message.emit(
            f"[AUDIO] Ready — <b>{REMAP_NAME}</b> is active and recognized by OS."
        )

    @staticmethod
    def _get_virtual_nodes() -> list[str]:
        out = []
        try:
            pw = subprocess.run(
                ["pw-cli", "list-objects", "Node"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            capture = False
            for line in pw.splitlines():
                stripped = line.strip()
                if not stripped:
                    capture = False
                    continue
                if any(k in stripped.lower() for k in ("phone_audio", "phone_virtual")):
                    capture = True
                if capture:
                    out.append(stripped)
        except Exception:
            pass
        return out

    def _start_scrcpy(self) -> None:
        cmd = _scrcpy_cmd(
            self._mode, self._no_preview, self._camera_id, self._audio_source,
            self._flip_h, self._flip_v
        )
        self.log_message.emit(f"[SCRCPY] Launching: {' '.join(cmd)}")
        self.status_changed.emit("Running…")

        env = {**os.environ}
        set_audio_env = _need_audio(self._mode)
        if set_audio_env:
            env["SDL_AUDIODRIVER"] = "pulseaudio"
            env["PULSE_SINK"] = SINK_NAME
            env["PIPEWIRE_TARGET"] = SINK_NAME
            env["PIPEWIRE_NODE"] = SINK_NAME
            env["PULSE_PROP"] = "media.role=filter"

        try:
            self._scrcpy_proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=_set_pdeathsig,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "scrcpy not found.  Install it with:\n  sudo pacman -S scrcpy"
            ) from exc

        self.log_message.emit(f"[SCRCPY] Started (pid {self._scrcpy_proc.pid})")

    def _ensure_scrcpy_audio_routed(self) -> bool:
        found = False
        try:
            out = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
            current_id = None
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("Sink Input #"):
                    current_id = stripped.split("#")[1].strip()
                if "scrcpy" in stripped.lower() and current_id:
                    self.log_message.emit(
                        f"[AUDIO] Routing scrcpy audio stream #{current_id} to {SINK_NAME} …"
                    )
                    subprocess.run(
                        ["pactl", "move-sink-input", current_id, SINK_NAME],
                        capture_output=True,
                        timeout=3,
                    )
                    found = True
                    break

            pw_out = subprocess.run(
                ["pw-link", "-l"], capture_output=True, text=True, timeout=3
            ).stdout
            current_src = None
            for line in pw_out.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if "|->" in stripped:
                    dst = stripped.split("|->")[1].strip()
                    if current_src and "scrcpy" in current_src.lower():
                        if SINK_NAME not in dst:
                            if self._mute_speaker_preview:
                                self.log_message.emit(
                                    f"[AUDIO] Muting speaker preview: {current_src} -x {dst}"
                                )
                                subprocess.run(
                                    ["pw-link", "-d", current_src, dst], capture_output=True
                                )
                            ch = (
                                "FL"
                                if "FL" in current_src
                                else ("FR" if "FR" in current_src else "")
                            )
                            if ch:
                                subprocess.run(
                                    ["pw-link", current_src, f"{SINK_NAME}:playback_{ch}"],
                                    capture_output=True,
                                )
                            found = True
                elif "|<-" not in stripped:
                    current_src = stripped
        except Exception as exc:
            self.log_message.emit(f"[AUDIO] Reroute warning: {exc}")
        return found

    def _monitor_scrcpy(self) -> None:
        assert self._scrcpy_proc is not None
        assert self._scrcpy_proc.stdout is not None

        fd = self._scrcpy_proc.stdout.fileno()
        poller = select.poll()
        poller.register(fd, select.POLLIN)

        routed = False
        loop_count = 0

        while self._running:
            events = poller.poll(500)
            loop_count += 1
            if _need_audio(self._mode) and (loop_count % 4 == 0 or not routed):
                if self._ensure_scrcpy_audio_routed():
                    routed = True

            if events:
                line = self._scrcpy_proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.log_message.emit(f"[SCRCPY] {text}")

        remaining = self._scrcpy_proc.stdout.read()
        if remaining:
            for line in remaining.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    self.log_message.emit(f"[SCRCPY] {line}")

        exit_code = self._scrcpy_proc.wait()
        self.log_message.emit(f"[SCRCPY] Exited with code {exit_code}")

        if self._running and exit_code != 0 and not self._restart_requested:
            self.error_occurred.emit(
                f"scrcpy stopped unexpectedly (exit code {exit_code})"
            )

    def _kill_scrcpy(self) -> None:
        if self._scrcpy_proc is None:
            return
        pid = self._scrcpy_proc.pid
        self.log_message.emit(f"[SCRCPY] Terminating pid {pid} …")
        self._scrcpy_proc.terminate()
        try:
            self._scrcpy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.log_message.emit("[SCRCPY] Killing (SIGKILL) …")
            self._scrcpy_proc.kill()
            self._scrcpy_proc.wait(timeout=3)
        self._scrcpy_proc = None

    def _unload_modules(self) -> None:
        if self._loopback_module_id is not None:
            try:
                self.log_message.emit("[AUDIO] Unloading loopback preview module …")
                _run_pactl(["unload-module", str(self._loopback_module_id)])
            except Exception:
                pass
            self._loopback_module_id = None

        if self._pw_loopback_proc is not None:
            pid = self._pw_loopback_proc.pid
            self.log_message.emit(f"[AUDIO] Killing pw-loopback pid {pid} …")
            self._pw_loopback_proc.kill()
            try:
                self._pw_loopback_proc.wait(timeout=3)
                self.log_message.emit("[AUDIO] pw-loopback terminated.")
            except subprocess.TimeoutExpired:
                self.log_message.emit("[AUDIO] pw-loopback ignored SIGKILL")
            self._pw_loopback_proc = None

        if self._remap_source_module_id is not None:
            try:
                self.log_message.emit(
                    f"[AUDIO] Unloading remap-source module {self._remap_source_module_id} …"
                )
                _run_pactl(["unload-module", str(self._remap_source_module_id)])
                self.log_message.emit("[AUDIO] Virtual microphone unloaded.")
            except RuntimeError as exc:
                self.log_message.emit(f"[AUDIO] Warning: {exc}")
            self._remap_source_module_id = None

        if self._null_sink_module_id is not None:
            try:
                self.log_message.emit(
                    f"[AUDIO] Unloading null-sink module {self._null_sink_module_id} …"
                )
                _run_pactl(["unload-module", str(self._null_sink_module_id)])
                self.log_message.emit("[AUDIO] Null-sink unloaded.")
            except RuntimeError as exc:
                self.log_message.emit(f"[AUDIO] Warning: {exc}")
            self._null_sink_module_id = None

        _cleanup_stale_audio_modules()


# ---------------------------------------------------------------------------
# Custom UI Components matching Modern Dashboard Aesthetics
# ---------------------------------------------------------------------------

class ToggleSwitch(QWidget):
    """Modern iOS/Android style animated toggle switch."""
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None, checked=False):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if self._checked != checked:
            self._checked = checked
            self.update()
            self.toggled.emit(self._checked)

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.update()

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Track Color
        if not self.isEnabled():
            bg_color = QColor("#1e293b")
        elif self._checked:
            bg_color = QColor("#6366f1")  # Vibrant indigo / purple
        else:
            bg_color = QColor("#334155")  # Dark gray off

        # Draw track
        painter.setBrush(bg_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 12, 12)

        # Draw thumb knob
        thumb_color = QColor("#ffffff") if self.isEnabled() else QColor("#94a3b8")
        painter.setBrush(thumb_color)
        thumb_x = self.width() - 21 if self._checked else 3
        painter.drawEllipse(thumb_x, 3, 18, 18)


class SegmentedControl(QWidget):
    """Modern Segmented Mode Selector Bar."""
    modeChanged = pyqtSignal(str)

    def __init__(self, modes: list[str], current_mode: str = "", parent=None):
        super().__init__(parent)
        self._modes = modes
        self._buttons: dict[str, QPushButton] = {}
        self._current_mode = current_mode or (modes[0] if modes else "")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.setStyleSheet("""
            SegmentedControl {
                background-color: #0f172a;
                border-radius: 8px;
                border: 1px solid #1e293b;
            }
        """)

        for mode in self._modes:
            btn = QPushButton(mode)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda checked, m=mode: self.set_current_mode(m))
            self._buttons[mode] = btn
            layout.addWidget(btn)

        self._update_styles()

    def currentMode(self) -> str:
        return self._current_mode

    def set_current_mode(self, mode: str) -> None:
        if mode in self._modes and self._current_mode != mode:
            self._current_mode = mode
            self._update_styles()
            self.modeChanged.emit(mode)

    def _update_styles(self) -> None:
        for mode, btn in self._buttons.items():
            is_active = (mode == self._current_mode)
            btn.setChecked(is_active)
            if is_active:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #6366f1;
                        color: #ffffff;
                        border-radius: 6px;
                        font-weight: bold;
                        font-size: 13px;
                        border: none;
                        padding: 0 16px;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #94a3b8;
                        border-radius: 6px;
                        font-size: 13px;
                        border: none;
                        padding: 0 16px;
                    }
                    QPushButton:hover {
                        background-color: #1e293b;
                        color: #f8fafc;
                    }
                """)


class StatusPill(QWidget):
    """Status Pill Badge with colored indicator dot."""
    def __init__(
        self,
        text: str = "",
        active: bool = False,
        active_color: str = "#10b981",
        inactive_color: str = "#ef4444",
        parent=None,
    ):
        super().__init__(parent)
        self._text = text
        self._active = active
        self._active_color = active_color
        self._inactive_color = inactive_color
        self._label = QLabel(text)
        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)

        self.setStyleSheet("""
            StatusPill {
                background-color: #1e293b;
                border-radius: 12px;
                border: 1px solid #334155;
            }
        """)

        self._label.setStyleSheet(
            "color: #e2e8f0; font-size: 12px; font-weight: 500; border: none; background: transparent;"
        )
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        self.update_status(self._active, self._text)

    def update_status(self, active: bool, text: str | None = None) -> None:
        self._active = active
        if text is not None:
            self._text = text
            self._label.setText(self._text)
        color = self._active_color if self._active else self._inactive_color
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 4px; border: none;"
        )


class StateBadge(QWidget):
    """Badge for current execution state (IDLE / RUNNING / RESTARTING)."""
    def __init__(self, state: str = "IDLE", parent=None):
        super().__init__(parent)
        self._label = QLabel(state)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(0)

        self._label.setStyleSheet("""
            color: #ffffff;
            font-size: 11px;
            font-weight: bold;
            letter-spacing: 0.5px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._label)
        self.set_state("IDLE")

    def set_state(self, state: str) -> None:
        self._label.setText(state.upper())
        if state.upper() in ("RUNNING", "RUNNING…"):
            self.setStyleSheet("""
                StateBadge {
                    background-color: #0284c7;
                    border-radius: 10px;
                    border: none;
                }
            """)
        elif "STARTING" in state.upper() or "RESTARTING" in state.upper():
            self.setStyleSheet("""
                StateBadge {
                    background-color: #d97706;
                    border-radius: 10px;
                    border: none;
                }
            """)
        elif "ERROR" in state.upper():
            self.setStyleSheet("""
                StateBadge {
                    background-color: #dc2626;
                    border-radius: 10px;
                    border: none;
                }
            """)
        else:
            self.setStyleSheet("""
                StateBadge {
                    background-color: #0e7490;
                    border-radius: 10px;
                    border: none;
                }
            """)


class CardWidget(QFrame):
    """Custom Card container with rounded borders and dark gradient background."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            CardWidget {
                background-color: #181b26;
                border-radius: 12px;
                border: 1px solid #2e3548;
            }
            CardWidget QLabel {
                background-color: transparent;
                border: none;
            }
        """)


# ---------------------------------------------------------------------------
# Main GUI Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._worker: StreamWorker | None = None
        self._init_ui()
        self._connect_signals()
        self._start_status_timer()

    def _init_ui(self) -> None:
        self.setWindowTitle("VirtualSource Console")
        self.setMinimumSize(840, 680)

        # Central widget with background styling
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("""
            QWidget {
                background-color: #0f121d;
            }
            QLabel {
                background-color: transparent;
                border: none;
            }
        """)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        # -------------------------------------------------------------------
        # 1. Header Bar
        # -------------------------------------------------------------------
        header_layout = QHBoxLayout()

        # Logo + Title
        title_label = QLabel("VirtualSource Console")
        title_label.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #f8fafc; background: transparent; border: none;"
        )
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        # Status Pills
        self._adb_pill = StatusPill("ADB: Checking...", active=False)
        self._v4l2_pill = StatusPill("v4l2loopback: Checking...", active=False)
        self._state_badge = StateBadge("IDLE")

        header_layout.addWidget(self._adb_pill)
        header_layout.addWidget(self._v4l2_pill)
        header_layout.addWidget(self._state_badge)

        # Refresh Icon Button
        self._hdr_refresh_btn = QPushButton("Refresh")
        self._hdr_refresh_btn.setFixedHeight(34)
        self._hdr_refresh_btn.setMinimumWidth(80)
        self._hdr_refresh_btn.setToolTip("Refresh Device & Status")
        self._hdr_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hdr_refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #f8fafc;
                font-size: 13px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
        """)
        header_layout.addWidget(self._hdr_refresh_btn)

        # Terminal / Info Icon Button
        self._hdr_term_btn = QPushButton("Console")
        self._hdr_term_btn.setFixedHeight(34)
        self._hdr_term_btn.setMinimumWidth(80)
        self._hdr_term_btn.setToolTip("Console Mode")
        self._hdr_term_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hdr_term_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #f8fafc;
                font-size: 13px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
        """)
        header_layout.addWidget(self._hdr_term_btn)

        main_layout.addLayout(header_layout)

        # -------------------------------------------------------------------
        # 2. Card: Stream Control
        # -------------------------------------------------------------------
        stream_card = CardWidget()
        stream_layout = QVBoxLayout(stream_card)
        stream_layout.setContentsMargins(18, 16, 18, 16)
        stream_layout.setSpacing(12)

        # Card Title
        sc_title = QLabel("Stream Control")
        sc_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #f1f5f9; background: transparent; border: none;")
        stream_layout.addWidget(sc_title)

        stream_body = QHBoxLayout()

        # Operating Mode Selector
        mode_box = QVBoxLayout()
        mode_box.setSpacing(6)
        mode_lbl = QLabel("OPERATING MODE")
        mode_lbl.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: #94a3b8; letter-spacing: 0.5px; background: transparent; border: none;"
        )
        mode_box.addWidget(mode_lbl)

        self._segmented_mode = SegmentedControl(
            [MODE_VIDEO_AUDIO, MODE_VIDEO_ONLY, MODE_AUDIO_ONLY],
            current_mode=MODE_VIDEO_AUDIO,
        )
        mode_box.addWidget(self._segmented_mode)
        stream_body.addLayout(mode_box, stretch=1)
        stream_body.addSpacing(24)

        # Action Buttons (Start / Stop)
        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)

        self._start_btn = QPushButton("Start Stream")
        self._start_btn.setMinimumHeight(42)
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.setStyleSheet("""
            QPushButton {
                background-color: #10b981;
                color: #ffffff;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                padding: 0 24px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
            QPushButton:disabled {
                background-color: #1f2937;
                color: #4b5563;
            }
        """)

        self._stop_btn = QPushButton("Stop Stream")
        self._stop_btn.setMinimumHeight(42)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #374151;
                color: #9ca3af;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                padding: 0 24px;
                border: none;
            }
            QPushButton:enabled {
                background-color: #dc2626;
                color: #ffffff;
            }
            QPushButton:enabled:hover {
                background-color: #b91c1c;
            }
            QPushButton:disabled {
                background-color: #1f2937;
                color: #4b5563;
            }
        """)

        btn_box.addWidget(self._start_btn)
        btn_box.addWidget(self._stop_btn)
        stream_body.addLayout(btn_box)

        stream_layout.addLayout(stream_body)
        main_layout.addWidget(stream_card)

        # -------------------------------------------------------------------
        # 3. Two Column Layout: Video Settings & Audio Settings
        # -------------------------------------------------------------------
        settings_row = QHBoxLayout()
        settings_row.setSpacing(16)

        # ---------------- Video Settings Card ----------------
        video_card = CardWidget()
        video_layout = QVBoxLayout(video_card)
        video_layout.setContentsMargins(18, 16, 18, 16)
        video_layout.setSpacing(14)

        # Video Header
        v_hdr_layout = QHBoxLayout()
        v_title = QLabel("Video Settings")
        v_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #f1f5f9; background: transparent; border: none;")
        v_hdr_layout.addWidget(v_title)
        v_hdr_layout.addStretch(1)

        self._cam_refresh_btn = QPushButton("Refresh")
        self._cam_refresh_btn.setFixedHeight(28)
        self._cam_refresh_btn.setMinimumWidth(70)
        self._cam_refresh_btn.setToolTip("Refresh Camera List")
        self._cam_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cam_refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #94a3b8;
                border: none;
                font-size: 13px;
            }
            QPushButton:hover {
                color: #f8fafc;
            }
        """)
        v_hdr_layout.addWidget(self._cam_refresh_btn)
        video_layout.addLayout(v_hdr_layout)

        # Camera Dropdown Box
        cam_box = QVBoxLayout()
        cam_box.setSpacing(6)
        cam_lbl = QLabel("CAMERA SOURCE")
        cam_lbl.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: #94a3b8; letter-spacing: 0.5px; background: transparent; border: none;"
        )
        cam_box.addWidget(cam_lbl)

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumHeight(38)
        self._camera_combo.setStyleSheet("""
            QComboBox {
                background-color: #0f172a;
                border: 1px solid #2e3548;
                border-radius: 6px;
                padding: 6px 12px;
                color: #f8fafc;
                font-size: 13px;
            }
            QComboBox:hover {
                border-color: #475569;
            }
            QComboBox QAbstractItemView {
                background-color: #181b26;
                border: 1px solid #2e3548;
                selection-background-color: #6366f1;
                color: #f8fafc;
            }
        """)
        cam_box.addWidget(self._camera_combo)
        video_layout.addLayout(cam_box)

        # Toggles
        self._flip_h_switch = ToggleSwitch()
        self._flip_v_switch = ToggleSwitch()
        self._stop_cam_prev_switch = ToggleSwitch()

        def _make_toggle_row(label_text: str, switch_widget: ToggleSwitch) -> QHBoxLayout:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #e2e8f0; font-size: 13px; background: transparent; border: none;")
            row.addWidget(lbl)
            row.addStretch(1)
            row.addWidget(switch_widget)
            return row

        video_layout.addLayout(_make_toggle_row("Flip Horizontal", self._flip_h_switch))
        video_layout.addLayout(_make_toggle_row("Flip Vertical", self._flip_v_switch))
        video_layout.addLayout(_make_toggle_row("Stop Cam Preview", self._stop_cam_prev_switch))

        settings_row.addWidget(video_card, stretch=1)

        # ---------------- Audio Settings Card ----------------
        audio_card = CardWidget()
        audio_layout = QVBoxLayout(audio_card)
        audio_layout.setContentsMargins(18, 16, 18, 16)
        audio_layout.setSpacing(14)

        # Audio Header
        a_title = QLabel("Audio Settings")
        a_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #f1f5f9; background: transparent; border: none;")
        audio_layout.addWidget(a_title)

        # Audio Source Dropdown
        aud_box = QVBoxLayout()
        aud_box.setSpacing(6)
        aud_lbl = QLabel("AUDIO SOURCE")
        aud_lbl.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: #94a3b8; letter-spacing: 0.5px; background: transparent; border: none;"
        )
        aud_box.addWidget(aud_lbl)

        self._audio_src_combo = QComboBox()
        self._audio_src_combo.addItems([AUDIO_SRC_MIC, AUDIO_SRC_OUTPUT])
        self._audio_src_combo.setMinimumHeight(38)
        self._audio_src_combo.setStyleSheet("""
            QComboBox {
                background-color: #0f172a;
                border: 1px solid #2e3548;
                border-radius: 6px;
                padding: 6px 12px;
                color: #f8fafc;
                font-size: 13px;
            }
            QComboBox:hover {
                border-color: #475569;
            }
            QComboBox QAbstractItemView {
                background-color: #181b26;
                border: 1px solid #2e3548;
                selection-background-color: #6366f1;
                color: #f8fafc;
            }
        """)
        aud_box.addWidget(self._audio_src_combo)
        audio_layout.addLayout(aud_box)

        # Virtual Sink Info Box
        info_box = QFrame()
        info_box.setStyleSheet("""
            QFrame {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 8px;
                padding: 8px 12px;
            }
        """)
        info_box_layout = QVBoxLayout(info_box)
        info_box_layout.setContentsMargins(8, 8, 8, 8)
        info_box_layout.setSpacing(4)

        info_header = QLabel("Virtual Sink Identity")
        info_header.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: 500; background: transparent; border: none;")
        info_val = QLabel(REMAP_NAME)
        info_val.setStyleSheet(
            "color: #38bdf8; font-family: monospace; font-size: 13px; font-weight: bold; background: transparent; border: none;"
        )

        info_box_layout.addWidget(info_header)
        info_box_layout.addWidget(info_val)
        audio_layout.addWidget(info_box)

        # Mic Preview Toggle Switch
        self._stop_mic_prev_switch = ToggleSwitch()
        audio_layout.addLayout(_make_toggle_row("Stop Mic Preview", self._stop_mic_prev_switch))

        settings_row.addWidget(audio_card, stretch=1)
        main_layout.addLayout(settings_row)

        # -------------------------------------------------------------------
        # 4. Card: Prerequisites Setup
        # -------------------------------------------------------------------
        prereq_card = CardWidget()
        prereq_layout = QVBoxLayout(prereq_card)
        prereq_layout.setContentsMargins(18, 14, 18, 14)
        prereq_layout.setSpacing(8)

        prereq_title = QLabel("Prerequisites Setup")
        prereq_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #f1f5f9; background: transparent; border: none;")
        prereq_sub = QLabel("Run this command once per session to load the loopback module.")
        prereq_sub.setStyleSheet("font-size: 12px; color: #94a3b8; background: transparent; border: none;")

        prereq_layout.addWidget(prereq_title)
        prereq_layout.addWidget(prereq_sub)

        cmd_box = QHBoxLayout()
        cmd_box.setSpacing(8)

        cmd_text = QLabel('sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"')
        cmd_text.setStyleSheet("""
            QLabel {
                background-color: #090d16;
                border: 1px solid #1e293b;
                border-radius: 6px;
                color: #e2e8f0;
                font-family: monospace;
                font-size: 12px;
                padding: 10px 14px;
            }
        """)
        cmd_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        cmd_box.addWidget(cmd_text, stretch=1)

        self._copy_prereq_btn = QPushButton("Copy")
        self._copy_prereq_btn.setFixedHeight(38)
        self._copy_prereq_btn.setMinimumWidth(70)
        self._copy_prereq_btn.setToolTip("Copy Command")
        self._copy_prereq_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_prereq_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #f8fafc;
                font-size: 13px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #334155;
            }
        """)
        cmd_box.addWidget(self._copy_prereq_btn)
        prereq_layout.addLayout(cmd_box)
        main_layout.addWidget(prereq_card)

        # -------------------------------------------------------------------
        # 5. Card: Console Output
        # -------------------------------------------------------------------
        console_card = CardWidget()
        console_layout = QVBoxLayout(console_card)
        console_layout.setContentsMargins(18, 14, 18, 14)
        console_layout.setSpacing(10)

        c_hdr = QHBoxLayout()
        c_title = QLabel("Console Output")
        c_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #f1f5f9; background: transparent; border: none;")
        c_hdr.addWidget(c_title)
        c_hdr.addStretch(1)

        self._clear_log_btn = QPushButton("Clear")
        self._clear_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_log_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #94a3b8;
                font-size: 12px;
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f8fafc;
            }
        """)

        self._copy_log_btn = QPushButton("Copy")
        self._copy_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_log_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #94a3b8;
                font-size: 12px;
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: #334155;
                color: #f8fafc;
            }
        """)

        c_hdr.addWidget(self._clear_log_btn)
        c_hdr.addWidget(self._copy_log_btn)
        console_layout.addLayout(c_hdr)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(140)
        self._log_view.setStyleSheet("""
            QTextEdit {
                background-color: #090d16;
                border: 1px solid #1e293b;
                border-radius: 8px;
                color: #e2e8f0;
                font-family: monospace;
                font-size: 12px;
                padding: 10px;
            }
        """)
        console_layout.addWidget(self._log_view, stretch=1)
        main_layout.addWidget(console_card, stretch=1)

        # Initial camera list populate & status check
        self._on_refresh_cameras()
        self._check_system_status()

    # ------------------------------------------------------------------
    # Signal Connections
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        self._hdr_refresh_btn.clicked.connect(self._check_system_status)
        self._cam_refresh_btn.clicked.connect(self._on_refresh_cameras)

        self._flip_h_switch.toggled.connect(self._on_flip_changed)
        self._flip_v_switch.toggled.connect(self._on_flip_changed)
        self._stop_cam_prev_switch.toggled.connect(self._on_stop_cam_preview_toggled)
        self._stop_mic_prev_switch.toggled.connect(self._on_stop_mic_preview_toggled)

        self._copy_prereq_btn.clicked.connect(self._copy_prereq_command)
        self._clear_log_btn.clicked.connect(self._log_view.clear)
        self._copy_log_btn.clicked.connect(self._copy_logs)

    # ------------------------------------------------------------------
    # Status Polling Timer
    # ------------------------------------------------------------------
    def _start_status_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(3000)  # Check every 3s
        self._timer.timeout.connect(self._check_system_status)
        self._timer.start()

    @pyqtSlot()
    def _check_system_status(self) -> None:
        adb_connected = self._adb_device_connected()
        v4l2_loaded = self._check_v4l2loopback()

        self._adb_pill.update_status(
            adb_connected, "ADB: Connected" if adb_connected else "ADB: Disconnected"
        )
        self._v4l2_pill.update_status(
            v4l2_loaded, "v4l2loopback: Loaded" if v4l2_loaded else "v4l2loopback: Not Loaded"
        )

    # ------------------------------------------------------------------
    # Actions & Slots
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_refresh_cameras(self) -> None:
        self._append_log("[GUI] Querying cameras via `scrcpy --list-cameras`...")
        cameras = list_android_cameras()
        self._camera_combo.clear()
        if cameras:
            for cam_id, label in cameras:
                self._camera_combo.addItem(label, cam_id)
            self._append_log(f"[GUI] Found {len(cameras)} camera(s).")
        else:
            self._camera_combo.addItem("Camera 0 (back, 4080x3060)", "0")
            self._camera_combo.addItem("Front Camera (Default)", "front")
            self._append_log("[GUI] Using default camera fallback.")

    @pyqtSlot(bool)
    def _on_flip_changed(self, _: bool) -> None:
        if self._worker is not None and self._worker._running:
            flip_h = self._flip_h_switch.isChecked()
            flip_v = self._flip_v_switch.isChecked()
            self._worker.update_flip(flip_h, flip_v)

    @pyqtSlot(bool)
    def _on_stop_cam_preview_toggled(self, checked: bool) -> None:
        if self._worker is not None and self._worker._running:
            if checked:
                self._worker.stop_cam_preview()
            else:
                self._worker.start_cam_preview()

    @pyqtSlot(bool)
    def _on_stop_mic_preview_toggled(self, checked: bool) -> None:
        if self._worker is not None and self._worker._running:
            if checked:
                self._worker.stop_mic_preview()
            else:
                self._worker.start_mic_preview()

    @pyqtSlot()
    def _copy_prereq_command(self) -> None:
        cmd = 'sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"'
        QApplication.clipboard().setText(cmd)
        self._copy_prereq_btn.setText("Copied")
        QTimer.singleShot(1500, lambda: self._copy_prereq_btn.setText("Copy"))

    @pyqtSlot()
    def _copy_logs(self) -> None:
        QApplication.clipboard().setText(self._log_view.toPlainText())
        self._copy_log_btn.setText("Copied")
        QTimer.singleShot(1500, lambda: self._copy_log_btn.setText("Copy"))

    def _create_worker(self) -> None:
        self._worker = StreamWorker()
        self._worker.log_message.connect(self._append_log)
        self._worker.status_changed.connect(self._on_status_changed)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)

    @pyqtSlot()
    def _on_start(self) -> None:
        mode = self._segmented_mode.currentMode()
        camera_id = self._camera_combo.currentData()
        audio_src_key = (
            "mic" if self._audio_src_combo.currentText() == AUDIO_SRC_MIC else "output"
        )
        flip_h = self._flip_h_switch.isChecked()
        flip_v = self._flip_v_switch.isChecked()
        stop_cam_preview = self._stop_cam_prev_switch.isChecked()
        stop_mic_preview = self._stop_mic_prev_switch.isChecked()

        if mode != MODE_AUDIO_ONLY:
            if not self._check_v4l2loopback():
                QMessageBox.warning(
                    self,
                    "v4l2loopback",
                    "v4l2loopback module not loaded.\n\n"
                    "Run this in a terminal first:\n"
                    "  sudo modprobe v4l2loopback exclusive_caps=1 "
                    'card_label="Phone Camera"',
                )
                return

        if not self._adb_device_connected():
            QMessageBox.warning(
                self,
                "ADB",
                "No Android device detected.\n\n"
                "Connect your phone over USB or Wi-Fi and ensure "
                "`adb devices` lists it.",
            )
            return

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._state_badge.set_state("Starting…")
        self._append_log(
            f"[GUI] Starting — mode: {mode}, camera_id: {camera_id}, audio_source: {audio_src_key}, stop_cam_prev: {stop_cam_preview}, stop_mic_prev: {stop_mic_preview}"
        )
        self._create_worker()
        self._worker.start_stream(
            mode, camera_id, audio_src_key, flip_h, flip_v, stop_cam_preview, stop_mic_preview
        )

    @pyqtSlot()
    def _on_stop(self) -> None:
        self._state_badge.set_state("Stopping…")
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._append_log("[GUI] Stopping stream worker …")
        if self._worker is not None:
            self._worker.stop_stream()

    @pyqtSlot(str)
    def _on_status_changed(self, status: str) -> None:
        self._state_badge.set_state(status)

    @pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        self._append_log(f"[ERROR] {msg}")
        self._state_badge.set_state("Error")
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    @pyqtSlot()
    def _on_worker_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._state_badge.set_state("IDLE")

    @pyqtSlot(str)
    def _append_log(self, text: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = text

        if "[INFO]" in text:
            formatted = text.replace(
                "[INFO]", f'<span style="color: #10b981; font-weight: bold;">[INFO]</span> {now}'
            )
        elif "[SYSTEM]" in text:
            formatted = text.replace(
                "[SYSTEM]", f'<span style="color: #38bdf8; font-weight: bold;">[SYSTEM]</span> {now}'
            )
        elif "[SCRCPY]" in text:
            formatted = text.replace(
                "[SCRCPY]", f'<span style="color: #a855f7; font-weight: bold;">[SCRCPY]</span>'
            )
        elif "[AUDIO]" in text:
            formatted = text.replace(
                "[AUDIO]", f'<span style="color: #f59e0b; font-weight: bold;">[AUDIO]</span>'
            )
        elif "[ERROR]" in text:
            formatted = text.replace(
                "[ERROR]", f'<span style="color: #ef4444; font-weight: bold;">[ERROR]</span> {now}'
            )
        elif "[GUI]" in text:
            formatted = text.replace(
                "[GUI]", f'<span style="color: #6366f1; font-weight: bold;">[GUI]</span>'
            )

        self._log_view.append(formatted)
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Pre-flight Checks
    # ------------------------------------------------------------------
    @staticmethod
    def _adb_device_connected() -> bool:
        try:
            out = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and "offline" not in line:
                return True
        return False

    @staticmethod
    def _check_v4l2loopback() -> bool:
        try:
            out = subprocess.run(
                ["lsmod"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            return "v4l2loopback" in out
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
def main() -> None:
    os.environ.pop("QT_STYLE_OVERRIDE", None)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QLabel {
            background-color: transparent;
            border: none;
        }
    """)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()