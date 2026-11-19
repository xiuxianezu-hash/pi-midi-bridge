#!/bin/sh
# 热重载后重建 ALSA seq 订阅（重开 rawmidi 会把订阅断掉）
PATH=/usr/sbin:/usr/bin:/sbin:/bin
P="${VIRMIDI_PORT:-24:0}"
aconnect "$P" 128:0 2>/dev/null || true
aconnect 128:0 "$P" 2>/dev/null || true
true
