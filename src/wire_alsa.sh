#!/bin/sh
# 把 virmidi 口和 rtpmidid 的端口接上
PATH=/usr/sbin:/usr/bin:/sbin:/bin
VIRMIDI_PORT="${VIRMIDI_PORT:-24:0}"
sleep 1

aconnect "$VIRMIDI_PORT" 128:0 2>/dev/null || true
aconnect 128:0 "$VIRMIDI_PORT" 2>/dev/null || true

# rtpmidid 为每个 mDNS 发现的 peer 建了 128:2..n 的端口，双向连上
aconnect -l 2>/dev/null | awk '
  /^client 128:/ {inrtp=1; next}
  /^client / {inrtp=0}
  inrtp && /^[[:space:]]+[0-9]+ / {print $1}
' | while read -r port; do
  [ "$port" = "0" ] && continue
  aconnect "$VIRMIDI_PORT" 128:"$port" 2>/dev/null || true
  aconnect 128:"$port" "$VIRMIDI_PORT" 2>/dev/null || true
done
true
