#!/bin/sh
# 启动前硬件准备：让出 HCI、载入 virmidi、准备日志目录
PATH=/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p /var/log

systemctl stop bluetooth 2>/dev/null || true
systemctl mask bluetooth >/dev/null 2>&1 || true
pkill -9 bluetoothd 2>/dev/null || true
pkill -9 -x bt-agent 2>/dev/null || true

modprobe snd-virmidi 2>/dev/null || true
sleep 1

# 把可能存在的适配器都先 down 掉（索引不再写死）
for h in /sys/class/bluetooth/hci*; do
  [ -e "$h" ] || continue
  hciconfig "$(basename "$h")" down 2>/dev/null || true
done
sleep 1
true
