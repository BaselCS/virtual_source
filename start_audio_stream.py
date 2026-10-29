#!/usr/bin/env python3
"""
Headless Audio Stream Script: Phone Microphone to Virtual Mic
Runs scrcpy in Audio-Only mode with Microphone input routed to Phone_Virtual_Microphone,
without GUI interface and with mic speaker preview disabled.
"""

import argparse
import ctypes
import os
import select
import signal
import subprocess
import sys
import time

SINK_NAME = "Phone_Audio_Sink"
REMAP_NAME = "Phone_Virtual_Microphone"


def _set_pdeathsig():
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(1, signal.SIGKILL)
    except Exception:
        pass


def run_pactl(args: list[str]) -> str:
    res = subprocess.run(["pactl", *args], capture_output=True, text=True)
    return (res.stdout or "").strip()


def cleanup_audio_modules():
    print("[AUDIO] Cleaning up audio modules...")
    out = run_pactl(["list", "modules", "short"])
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            mod_id = parts[0]
            line_lower = line.lower()
            if "phone_audio_sink" in line_lower or "phone_virtual_microphone" in line_lower:
                run_pactl(["unload-module", mod_id])


def setup_audio(mute: bool = False, volume_percent: int = 160):
    cleanup_audio_modules()

    print(f"[AUDIO] Creating null sink: {SINK_NAME}...")
    sink_props = (
        f'device.description="{SINK_NAME}" '
        f'media.class="Audio/Sink" '
        f'device.icon_name="audio-sink"'
    )
    sink_id = run_pactl([
        "load-module",
        "module-null-sink",
        f"sink_name={SINK_NAME}",
        f"sink_properties={sink_props}",
    ])
    print(f"  → Null sink loaded (ID: {sink_id})")

    print(f"[AUDIO] Creating virtual microphone: {REMAP_NAME}...")
    source_props = (
        f'device.description="{REMAP_NAME}" '
        f'media.class="Audio/Source" '
        f'device.class="sound" '
        f'device.icon_name="audio-input-microphone"'
    )
    source_id = run_pactl([
        "load-module",
        "module-remap-source",
        f"master={SINK_NAME}.monitor",
        f"source_name={REMAP_NAME}",
        f"source_properties={source_props}",
    ])
    print(f"  → Virtual mic source loaded (ID: {source_id})")

    # Boost mic gain to fix weak audio capture
    run_pactl(["set-source-volume", REMAP_NAME, f"{volume_percent}%"])
    print(f"[AUDIO] Mic volume boosted to {volume_percent}% (gain fix).")

    # Mute microphone only if explicitly requested
    if mute:
        run_pactl(["set-source-mute", REMAP_NAME, "1"])
        print("[AUDIO] Mic is MUTED (Mute: ON).")
    else:
        run_pactl(["set-source-mute", REMAP_NAME, "0"])
        print("[AUDIO] Mic is UNMUTED (Mute: OFF / Active).")

    print(f"[AUDIO] Ready: Virtual mic '{REMAP_NAME}' is active (Speaker preview: OFF).")


def ensure_audio_routing():
    """Route scrcpy audio to Phone_Audio_Sink and disconnect from local speakers."""
    # 1. pactl sink-inputs
    out = run_pactl(["list", "sink-inputs"])
    curr_id = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sink Input #"):
            curr_id = stripped.split("#")[1].strip()
        if "scrcpy" in stripped.lower() and curr_id:
            run_pactl(["move-sink-input", curr_id, SINK_NAME])

    # 2. PipeWire pw-link
    pw_res = subprocess.run(["pw-link", "-l"], capture_output=True, text=True)
    current_src = None
    for line in (pw_res.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "|->" in stripped:
            dst = stripped.split("|->")[1].strip()
            if current_src and "scrcpy" in current_src.lower():
                if SINK_NAME not in dst:
                    # Mute speaker preview
                    subprocess.run(["pw-link", "-d", current_src, dst], capture_output=True)
                ch = "FL" if "FL" in current_src else ("FR" if "FR" in current_src else "")
                if ch:
                    subprocess.run(
                        ["pw-link", current_src, f"{SINK_NAME}:playback_{ch}"],
                        capture_output=True,
                    )
        elif "|<-" not in stripped:
            current_src = stripped


def main():
    parser = argparse.ArgumentParser(description="Virtual Mic Audio Stream (Headless)")
    parser.add_argument(
        "--mute",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Start with virtual microphone muted (default: False)",
    )
    parser.add_argument(
        "--volume",
        type=int,
        default=160,
        help="Set virtual microphone volume percent to fix weak pickup (default: 160)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("   Starting Virtual Source (Audio Only - Headless Mode)")
    print(f"   Audio Source: Microphone (Phone Mic)")
    print(f"   Sink Identity: {REMAP_NAME}")
    print(f"   Mute on Start: {args.mute}")
    print(f"   Volume Boost: {args.volume}%")
    print(f"   Speaker Preview: OFF")
    print("=" * 60)

    # Check adb device
    try:
        adb_out = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True).stdout
        dev_lines = [l for l in adb_out.splitlines()[1:] if l.strip() and "offline" not in l]
        if not dev_lines:
            print("[WARN] No ADB device detected. Waiting/proceeding anyway...")
        else:
            print(f"[ADB] Target device: {dev_lines[0].split()[0]}")
    except Exception as exc:
        print(f"[WARN] adb check failed: {exc}")

    setup_audio(mute=args.mute, volume_percent=args.volume)

    cmd = [
        "scrcpy",
        "--no-video",
        "--audio-source=mic",
        "--audio-codec=opus",
        "--audio-buffer=50",
    ]

    env = {
        **os.environ,
        "SDL_AUDIODRIVER": "pulseaudio",
        "PULSE_SINK": SINK_NAME,
        "PIPEWIRE_TARGET": SINK_NAME,
        "PIPEWIRE_NODE": SINK_NAME,
        "PULSE_PROP": "media.role=filter",
    }

    print(f"[SCRCPY] Running: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=_set_pdeathsig,
    )

    is_shutting_down = False

    def shutdown(signum=None, frame=None):
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        print("\n[STOP] Shutting down...")
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        cleanup_audio_modules()
        print("[STOP] Finished.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    poller = select.poll()
    poller.register(fd, select.POLLIN)

    loop_count = 0
    try:
        while proc.poll() is None:
            events = poller.poll(500)
            loop_count += 1
            if loop_count % 4 == 0 or loop_count <= 4:
                ensure_audio_routing()

            if events:
                line = proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[SCRCPY] {text}")
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()
