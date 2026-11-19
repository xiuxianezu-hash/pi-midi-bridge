# 更新记录

## 1.8.1 — 2026-11-19

- 顺带查清了广播的动态行为：rtpmidid 的广播列表是**跟着活的 ALSA 客户端实时增删**的。网页一启动，它自己的观察器客户端（`aseqdump` / `129:0`）就会被 `[alsa_announce]` 广播成一条 `aseqdump` 服务；网页停掉这条也跟着消失。`[alsa_announce]` 的结构里只有 `name` 一个字段，没有正则/白名单，所以在配置层过滤不掉它。
- 因此最终广播 = `rpi3`（真会话）+ `aseqdump`（本机网页观察器，仅 1 条、随网页服务存在，不影响任何功能）。原来那 6 条 `Midi Through` / `Virtual Raw MIDI 1-x-VirMIDI 1-x` 是 `[alsa_hw_auto_export]` 造成的，已彻底消失。
- 安装流程小调整：打 rtpmidid 补丁需要重启 rtpmidid 时，先停 `pi-midi-web` 再启，避免安装过程中多刷一条广播。

