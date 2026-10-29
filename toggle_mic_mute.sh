#!/usr/bin/env bash
REMAP_NAME="Phone_Virtual_Microphone"

# Try to toggle mute on the virtual microphone
if ! pactl set-source-mute "$REMAP_NAME" toggle 2>/dev/null; then
    notify-send -u low "Virtual Mic" "الميكروفون الافتراضي غير متصل"
    exit 0
fi

# Check new mute state
MUTE_STATUS=$(pactl get-source-mute "$REMAP_NAME" 2>/dev/null | awk '{print $2}')

if [ "$MUTE_STATUS" = "yes" ]; then
    notify-send -u low -i microphone-sensitivity-muted "Virtual Mic" "تم كتم الميكروفون 🔇"
else
    notify-send -u low -i audio-input-microphone "Virtual Mic" "تم تشغيل الميكروفون 🎙️"
fi
