# 更新记录

## 1.0.1 — 2026-11-19

- 修复：`pi-midi-bridge --version` 不再依赖 bumble（可在系统 python 下直接调用）
- 修复：安装后自动启动服务（原来只 enable 不 start）

## 1.0.0 — 2026-11-19

- 首个发行版本
- BLE-MIDI 外设：解帧按消息边界 + 长度表，时间戳任意 >=0x80 正确跳过
- 裸 MIDI 流解析保留 running status（修复约 45% 系统性丢音）
- SYSEX 整段透传（macOS 连接时的 GM Reset 不再被掐成单字节）
- 断连后自动补广播 + 15 秒看门狗
- 新增丢弃/错误计数与 30 秒 `[体检]` 日志
- 支持 `systemctl reload` 热重载（不断蓝牙链路，自动重建 ALSA 订阅）
- 适配器按 MAC 自动定位，hci 索引不再写死
