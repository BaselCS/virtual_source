#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
STREAM_SCRIPT="$SCRIPT_DIR/start_audio_stream.py"

# Check if stream is running
STREAM_RUNNING=0
if pgrep -f "start_audio_stream.py" >/dev/null 2>&1; then
    STREAM_RUNNING=1
fi

# Check if easyeffects is running
EASYEFFECTS_RUNNING=0
if pgrep -x "easyeffects" >/dev/null 2>&1; then
    EASYEFFECTS_RUNNING=1
fi

start_stream() {
    nohup uv run --project "$SCRIPT_DIR" python "$STREAM_SCRIPT" >/dev/null 2>&1 &
    notify-send -u low "Virtual Mic" "تم تشغيل ميكروفون الهاتف (المعاينة متوقفة)"
}

start_easyeffects() {
    if command -v uwsm-app >/dev/null 2>&1; then
        uwsm-app -- easyeffects >/dev/null 2>&1 &
    else
        easyeffects >/dev/null 2>&1 &
    fi
    notify-send -u low "EasyEffects" "تم تشغيل EasyEffects"
}

if [ "$STREAM_RUNNING" -eq 0 ] && [ "$EASYEFFECTS_RUNNING" -eq 0 ]; then
    start_stream
    start_easyeffects
elif [ "$STREAM_RUNNING" -eq 1 ] && [ "$EASYEFFECTS_RUNNING" -eq 0 ]; then
    start_easyeffects
elif [ "$STREAM_RUNNING" -eq 0 ] && [ "$EASYEFFECTS_RUNNING" -eq 1 ]; then
    start_stream
else
    notify-send -u low "Virtual Mic & EasyEffects" "كلا البرنامجين يعملان بالفعل"
fi
