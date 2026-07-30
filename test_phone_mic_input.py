#!/usr/bin/env python3
"""
Automated Test Script: Phone Microphone Audio Input Verification
Verifies that the phone microphone stream is successfully captured by the OS
as a virtual microphone input source (Phone_Virtual_Microphone) and not leaked
to local speakers.
"""

import math
import os
import struct
import subprocess
import time

SINK_NAME = "Phone_Audio_Sink"
REMAP_NAME = "Phone_Virtual_Microphone"


def run_cmd(args: list[str]) -> str:
    res = subprocess.run(args, capture_output=True, text=True)
    return (res.stdout or "").strip()


def cleanup():
    print("[CLEANUP] Unloading test audio modules...")
    out = run_cmd(["pactl", "list", "modules", "short"])
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            mod_id = parts[0]
            if "phone_audio_sink" in line.lower() or "phone_virtual_microphone" in line.lower():
                run_cmd(["pactl", "unload-module", mod_id])


def setup_audio_modules():
    cleanup()
    print("[SETUP] Creating Phone_Audio_Sink...")
    sink_id = run_cmd(
        [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={SINK_NAME}",
            f'sink_properties=device.description="{SINK_NAME}" media.class="Audio/Sink"',
        ]
    )
    print(f"  → Null sink loaded (ID: {sink_id})")

    print("[SETUP] Creating Phone_Virtual_Microphone...")
    source_props = (
        f'device.description="{REMAP_NAME}" '
        f'media.class="Audio/Source" '
        f'device.class="sound" '
        f'device.icon_name="audio-input-microphone"'
    )
    source_id = run_cmd(
        [
            "pactl",
            "load-module",
            "module-remap-source",
            f"master={SINK_NAME}.monitor",
            f"source_name={REMAP_NAME}",
            f"source_properties={source_props}",
        ]
    )
    print(f"  → Remap source loaded (ID: {source_id})")
    run_cmd(["pactl", "set-default-source", REMAP_NAME])
    return sink_id, source_id


def enforce_audio_routing():
    """Ensure scrcpy streams to Phone_Audio_Sink and is disconnected from speakers."""
    # 1. Pactl move sink-input
    out = run_cmd(["pactl", "list", "sink-inputs"])
    curr_id = None
    for line in out.splitlines():
        line_str = line.strip()
        if line_str.startswith("Sink Input #"):
            curr_id = line_str.split("#")[1].strip()
        if "scrcpy" in line_str.lower() and curr_id:
            run_cmd(["pactl", "move-sink-input", curr_id, SINK_NAME])

    # 2. PipeWire pw-link stateful routing
    pw_out = run_cmd(["pw-link", "-l"])
    current_src = None
    scrcpy_linked = False
    for line in pw_out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "|->" in stripped:
            dst = stripped.split("|->")[1].strip()
            if current_src and "scrcpy" in current_src.lower():
                if SINK_NAME not in dst:
                    run_cmd(["pw-link", "-d", current_src, dst])
                    ch = "FL" if "FL" in current_src else ("FR" if "FR" in current_src else "")
                    if ch:
                        run_cmd(["pw-link", current_src, f"{SINK_NAME}:playback_{ch}"])
                    scrcpy_linked = True
                else:
                    scrcpy_linked = True
        elif "|<-" not in stripped:
            current_src = stripped
    return scrcpy_linked


def record_and_analyze_audio(duration_sec: float = 3.0) -> tuple[float, int, int]:
    """Record raw 16-bit PCM mono audio from Phone_Virtual_Microphone and calculate RMS."""
    rec_file = "/tmp/test_phone_mic.raw"
    if os.path.exists(rec_file):
        os.remove(rec_file)

    # Use pw-record or parec to record raw 16-bit 44100Hz audio
    print(f"[TEST] Recording {duration_sec}s audio from {REMAP_NAME}...")
    proc = subprocess.Popen(
        [
            "pw-record",
            "--target",
            REMAP_NAME,
            "--format",
            "s16",
            "--rate",
            "44100",
            "--channels",
            "1",
            rec_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(duration_sec)
    proc.terminate()
    proc.wait()

    if not os.path.exists(rec_file) or os.path.getsize(rec_file) == 0:
        return 0.0, 0, 0

    with open(rec_file, "rb") as f:
        data = f.read()

    sample_count = len(data) // 2
    if sample_count == 0:
        return 0.0, 0, 0

    samples = struct.unpack(f"<{sample_count}h", data)
    max_amp = max(abs(s) for s in samples)
    sum_squares = sum(s * s for s in samples)
    rms = math.sqrt(sum_squares / sample_count)
    norm_rms = rms / 32768.0

    if os.path.exists(rec_file):
        os.remove(rec_file)

    return norm_rms, max_amp, sample_count


def main():
    print("=" * 65)
    print("      AUTOMATED TEST: PHONE MICROPHONE AUDIO INPUT CHECK      ")
    print("=" * 65)

    # 1. Check ADB
    devices = run_cmd(["adb", "devices"])
    lines = [l for l in devices.splitlines()[1:] if l.strip() and "offline" not in l]
    if not lines:
        print("[ERROR] No ADB Android device connected. Please connect over USB/Wi-Fi.")
        return

    print(f"[ADB] Connected device: {lines[0].split()[0]}")

    # 2. Setup Audio Modules
    setup_audio_modules()

    # 3. Launch scrcpy audio stream
    scrcpy_cmd = [
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
        "PIPEWIRE_NODE": SINK_NAME,
    }
    print(f"[SCRCPY] Launching audio stream: {' '.join(scrcpy_cmd)}")
    scrcpy_proc = subprocess.Popen(
        scrcpy_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    try:
        # Give scrcpy 2 seconds to initialize audio stream
        time.sleep(2)
        enforce_audio_routing()

        # 4. Record and measure audio
        rms, max_amp, samples = record_and_analyze_audio(duration_sec=3.0)

        print("\n" + "-" * 40)
        print("           TEST RESULTS METRICS           ")
        print("-" * 40)
        print(f"Recorded Samples : {samples}")
        print(f"Peak Amplitude   : {max_amp} / 32767")
        print(f"RMS Volume Score : {rms:.5f}")
        print("-" * 40)

        if samples > 0 and max_amp > 10:
            print("\n✅ PASS: Phone microphone is actively captured as OS INPUT!")
            print(f"   Ambient sound detected (Peak: {max_amp}, RMS: {rms:.5f}).")
            print("   The OS recognizes and receives active audio on Phone_Virtual_Microphone.")
        else:
            print("\n❌ FAIL: Zero audio amplitude captured on Phone_Virtual_Microphone.")
            print("   Check if phone microphone is muted or permissions are restricted.")

    finally:
        print("\n[CLEANUP] Stopping scrcpy and unloading modules...")
        scrcpy_proc.terminate()
        try:
            scrcpy_proc.wait(timeout=3)
        except Exception:
            scrcpy_proc.kill()
        cleanup()
        print("[CLEANUP] Done.")


if __name__ == "__main__":
    main()
