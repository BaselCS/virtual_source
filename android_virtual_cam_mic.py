#!/usr/bin/python3
"""
Android Virtual Webcam & Microphone — PyQt6 GUI for PipeWire/Arch Linux.

Three modes: video only, audio only, or both.
Wires an Android device (via scrcpy) into a v4l2loopback video device
and/or a PipeWire virtual microphone using pw-loopback.
"""

import os
import select
import subprocess
import sys

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPalette, QColor
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
    QGroupBox,
    QComboBox,
    QCheckBox,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SINK_NAME = "Phone_Audio_Sink"
REMAP_NAME = "Phone_Virtual_Microphone"

MODE_VIDEO_AUDIO = "Video + Audio"
MODE_VIDEO_ONLY = "Video Only"
MODE_AUDIO_ONLY = "Audio Only"


def list_android_cameras() -> list[tuple[str, str]]:
    """
    Run `scrcpy --list-cameras` and return a list of (camera_id, display_label).
    Example return:
    [
        ("0", "Camera 0 (back, 4080x3060)"),
        ("1", "Camera 1 (front, 3264x2448)"),
    ]
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
    mode: str, no_preview: bool = False, camera_id: str | None = None
) -> list[str]:
    cmd = ["scrcpy"]
    cam_flags = []
    if camera_id and camera_id not in ("front", "back", "default"):
        cam_flags = [f"--camera-id={camera_id}"]
    elif camera_id == "back":
        cam_flags = ["--camera-facing=back"]
    else:
        cam_flags = ["--camera-facing=front"]

    if mode == MODE_VIDEO_ONLY:
        cmd += [
            "--video-source=camera",
            *cam_flags,
            "--v4l2-sink=/dev/video0",
            "--no-audio",
        ]
    elif mode == MODE_AUDIO_ONLY:
        cmd += ["--no-video", "--audio-source=mic"]
    else:
        cmd += [
            "--video-source=camera",
            *cam_flags,
            "--v4l2-sink=/dev/video0",
            "--audio-source=mic",
        ]

    if no_preview:
        cmd.append("--no-window")
        if mode == MODE_VIDEO_AUDIO:
            cmd.append("--no-audio")

    return cmd


def _need_audio(mode: str) -> bool:
    return mode != MODE_VIDEO_ONLY


# ---------------------------------------------------------------------------
# Helpers
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
        self._pw_loopback_proc: subprocess.Popen | None = None
        self._running = False
        self._mode = MODE_VIDEO_AUDIO
        self._no_preview = False
        self._camera_id: str | None = None

    def start_stream(
        self, mode: str, no_preview: bool = False, camera_id: str | None = None
    ) -> None:
        self._mode = mode
        self._no_preview = no_preview
        self._camera_id = camera_id
        self._running = True
        self.start()

    def stop_stream(self) -> None:
        self._running = False
        self._kill_scrcpy()
        self._unload_modules()

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            skip_audio = self._no_preview and self._mode not in (MODE_AUDIO_ONLY,)
            if _need_audio(self._mode) and not skip_audio:
                self._setup_audio()
                self._dump_audio_state()
            else:
                self.log_message.emit("[AUDIO] Skipped")

            self._start_scrcpy()
            self._monitor_scrcpy()
        except RuntimeError as exc:
            self.log_message.emit(f"[ERROR] {exc}")
            self.error_occurred.emit(str(exc))
        except Exception as exc:
            self.log_message.emit(f"[ERROR] Unexpected: {exc}")
            self.error_occurred.emit(f"Unexpected error: {exc}")
        finally:
            self._unload_modules()
            self.finished.emit()

    # ------------------------------------------------------------------
    # Audio — null sink (pactl) + virtual source (pw-loopback native)
    # ------------------------------------------------------------------
    def _setup_audio(self) -> None:
        # 1. Null sink — scrcpy outputs its audio here
        self.log_message.emit("[AUDIO] Creating null sink …")
        out = _run_pactl(
            [
                "load-module",
                "module-null-sink",
                f"sink_name={SINK_NAME}",
                f"sink_properties=device.description={SINK_NAME}",
            ]
        )
        self._null_sink_module_id = int(out)
        self.log_message.emit(f"  → module id {self._null_sink_module_id}")

        # 2. Virtual source via pw-loopback — creates a native PipeWire
        #    node with media.class=Audio/Source/Virtual so EasyEffects,
        #    OBS, Meet, Discord see it as a microphone input device.
        self.log_message.emit("[AUDIO] Creating virtual microphone source …")
        pw_loopback_cmd = [
            "pw-loopback",
            "--capture-props",
            f"target.object={SINK_NAME} stream.capture.sink=true node.passive=true",
            "--playback-props",
            f"media.class=Audio/Source/Virtual node.name={REMAP_NAME} "
            f"node.description={REMAP_NAME}",
        ]
        self.log_message.emit(f"[AUDIO] pw-loopback: {' '.join(pw_loopback_cmd)}")
        try:
            self._pw_loopback_proc = subprocess.Popen(
                pw_loopback_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import time

            time.sleep(0.5)
            self.log_message.emit(f"  → pid {self._pw_loopback_proc.pid}")
        except FileNotFoundError as exc:
            raise RuntimeError(
                "pw-loopback not found.  Install it with:\n"
                "  sudo pacman -S pipewire"  # included in pipewire package
            ) from exc

    def _dump_audio_state(self) -> None:
        self.log_message.emit("[AUDIO] PipeWire nodes:")
        for node in self._get_virtual_nodes():
            self.log_message.emit(f"  {node}")
        self.log_message.emit("[AUDIO] PulseAudio sources:")
        for line in _list_sources().splitlines():
            self.log_message.emit(f"  {line}")
        self.log_message.emit(
            f"[AUDIO] Ready — select <b>{REMAP_NAME}</b> "
            f"in EasyEffects or your app's microphone input"
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

    # ------------------------------------------------------------------
    # scrcpy
    # ------------------------------------------------------------------
    def _start_scrcpy(self) -> None:
        cmd = _scrcpy_cmd(self._mode, self._no_preview, self._camera_id)
        self.log_message.emit(f"[SCRCPY] Launching: {' '.join(cmd)}")
        self.status_changed.emit("Running…")

        env = {**os.environ}
        set_audio_env = _need_audio(self._mode) and not (
            self._no_preview and self._mode == MODE_VIDEO_AUDIO
        )
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
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "scrcpy not found.  Install it with:\n  sudo pacman -S scrcpy"
            ) from exc

        self.log_message.emit(f"[SCRCPY] Started (pid {self._scrcpy_proc.pid})")

    def _ensure_scrcpy_audio_routed(self) -> bool:
        """Ensure scrcpy audio streams are routed to Phone_Audio_Sink and not speaker/easyeffects sink."""
        found = False
        try:
            # 1. Move sink input via pactl
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

            # 2. Reroute pw-link if connected to other sinks
            links = subprocess.run(
                ["pw-link", "-l"], capture_output=True, text=True, timeout=3
            ).stdout.splitlines()
            for line in links:
                if "scrcpy" in line.lower() and "|->" in line:
                    parts = line.split("|->")
                    src = parts[0].strip()
                    dst = parts[1].strip()
                    if SINK_NAME not in dst:
                        subprocess.run(["pw-link", "-d", src, dst], capture_output=True)
                        ch = "FL" if "FL" in src else ("FR" if "FR" in src else "")
                        if ch:
                            subprocess.run(
                                ["pw-link", src, f"{SINK_NAME}:playback_{ch}"],
                                capture_output=True,
                            )
                        found = True
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
            if _need_audio(self._mode) and not routed and loop_count <= 20:
                if self._no_preview:
                    routed = True
                elif self._ensure_scrcpy_audio_routed():
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

        if self._running and exit_code != 0:
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


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._worker: StreamWorker | None = None
        self._init_ui()
        self._connect_signals()

    def _init_ui(self) -> None:
        self.setWindowTitle("Android Virtual Camera & Mic")
        self.setMinimumSize(720, 560)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Status
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(
            "font-size: 16px; font-weight: bold; padding: 4px 0;"
        )
        layout.addWidget(self._status_lbl)

        # Mode selector + options + buttons
        top_row = QHBoxLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.addItems([MODE_VIDEO_AUDIO, MODE_VIDEO_ONLY, MODE_AUDIO_ONLY])
        self._mode_combo.setMinimumHeight(40)
        self._mode_combo.setMinimumWidth(150)
        top_row.addWidget(self._mode_combo)

        self._camera_combo = QComboBox()
        self._camera_combo.setMinimumHeight(40)
        self._camera_combo.setMinimumWidth(220)
        top_row.addWidget(self._camera_combo, stretch=1)

        self._refresh_cams_btn = QPushButton("🔄 Refresh")
        self._refresh_cams_btn.setMinimumHeight(40)
        self._refresh_cams_btn.setToolTip("Query connected Android device for available cameras")
        top_row.addWidget(self._refresh_cams_btn)

        self._no_preview_cb = QCheckBox("No Preview Window")
        self._no_preview_cb.setToolTip(
            "Stream video directly to /dev/video0 without opening a desktop preview window"
        )
        top_row.addWidget(self._no_preview_cb)

        self._start_btn = QPushButton("▶  Start")
        self._start_btn.setMinimumHeight(40)
        self._stop_btn = QPushButton("■  Stop")
        self._stop_btn.setMinimumHeight(40)
        self._stop_btn.setEnabled(False)
        top_row.addWidget(self._start_btn)
        top_row.addWidget(self._stop_btn)
        layout.addLayout(top_row)

        # v4l2loopback hint
        hint_group = QGroupBox("Prerequisite — v4l2loopback (run once per boot)")
        hint_layout = QVBoxLayout(hint_group)
        hint = QLabel(
            'sudo modprobe v4l2loopback exclusive_caps=1 card_label="Phone Camera"'
        )
        hint.setStyleSheet(
            "font-family: monospace; background: #2d2d2d; color: #f0f0f0;"
            " padding: 8px; border-radius: 4px;"
        )
        hint.setTextInteractionFlags(
            hint.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        hint_layout.addWidget(hint)
        layout.addWidget(hint_group)

        # Virtual mic info
        self._mic_info = QLabel(
            "Virtual mic: <b>Phone_Virtual_Microphone</b> — "
            "select it in EasyEffects / OBS / Meet"
        )
        self._mic_info.setWordWrap(True)
        self._mic_info.setStyleSheet("padding: 4px 0;")
        layout.addWidget(self._mic_info)

        # Log viewer
        log_group = QGroupBox("Log")
        log_layout.addWidget(self._log_view) if False else None  # placeholder check
        log_layout = QVBoxLayout(log_group)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet("font-family: monospace; font-size: 12px;")
        log_layout.addWidget(self._log_view)
        layout.addWidget(log_group, stretch=1)

        # Initial camera population
        self._on_refresh_cameras()

    def _connect_signals(self) -> None:
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        self._refresh_cams_btn.clicked.connect(self._on_refresh_cameras)

    def _create_worker(self) -> None:
        self._worker = StreamWorker()
        self._worker.log_message.connect(self._append_log)
        self._worker.status_changed.connect(self._status_lbl.setText)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)

    # ------------------------------------------------------------------
    # Slots
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
            self._camera_combo.addItem("Front Camera (Default)", "front")
            self._camera_combo.addItem("Back Camera (Default)", "back")
            self._append_log("[GUI] No cameras returned (or no device connected). Using defaults.")

    @pyqtSlot()
    def _on_start(self) -> None:
        mode = self._mode_combo.currentText()
        no_preview = self._no_preview_cb.isChecked()
        camera_id = self._camera_combo.currentData()

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
        self._log_view.clear()
        self._status_lbl.setText("Starting…")
        self._append_log(
            f"[GUI] Starting — mode: {mode}, camera_id: {camera_id}, no_preview: {no_preview}"
        )
        self._create_worker()
        self._worker.start_stream(mode, no_preview, camera_id)

    @pyqtSlot()
    def _on_stop(self) -> None:
        self._status_lbl.setText("Stopping…")
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._append_log("[GUI] Stopping stream worker …")
        if self._worker is not None:
            self._worker.stop_stream()

    @pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        self._append_log(f"[GUI] Error: {msg}")
        self._status_lbl.setText("Error")
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    @pyqtSlot()
    def _on_worker_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        lbl = self._status_lbl.text()
        if lbl in ("Starting…", "Running…", "Stopping…", "Error"):
            self._status_lbl.setText("Ready")

    @pyqtSlot(str)
    def _append_log(self, text: str) -> None:
        self._log_view.append(text)
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Pre-flight checks
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
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    os.environ.pop("QT_STYLE_OVERRIDE", None)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(220, 220, 220))
    app.setPalette(palette)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
