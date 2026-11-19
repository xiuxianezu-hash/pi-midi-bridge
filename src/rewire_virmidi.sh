#!/bin/sh
# 热重载后重建 ALSA seq 订阅（重开 rawmidi 会把订阅断掉）
PATH=/usr/sbin:/usr/bin:/sbin:/bin
P="${VIRMIDI_PORT:-auto}"
if [ -z "$P" ] || [ "$P" = "auto" ] || ! aconnect -l | grep -q "^client ${P%%:*}: "; then
  P=$(aconnect -l | sed -n 's/^client \([0-9]\+\).: .Virtual Raw MIDI.*/\1:0/p' | head -1)
  [ -n "$P" ] || P=24:0
fi
aconnect "$P" 128:0 2>/dev/null || true
aconnect 128:0 "$P" 2>/dev/null || true
true
