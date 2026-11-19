# pi-midi-bridge

把树莓派变成一台 **MIDI 路由器**：它同时是一台 **BLE-MIDI 蓝牙设备**（Mac / iPad / 手机在蓝牙列表里直接看到 `Pi-MIDI`），也是一个 **RTP-MIDI 网络端点**（与 Mac、Windows 走 Wi-Fi/以太网），两条通道通过 ALSA 虚拟 MIDI 口互通。

```
                     ┌────────────────────────┐
 Mac / iPad  ──BLE──▶│                        │──raw MIDI──▶ /dev/snd/midiC2D0
                     │  pi-midi-bridge (Py)   │                    │
 Windows/Mac ──RTP──▶│                        │◀──raw MIDI─────────┘
   (rtpmidid)        └────────────────────────┘
                          snd-virmidi 虚拟口 → ALSA seq → rtpmidid 的 RTP 会话
```

## 为什么需要它

- 树莓派没有 USB OTG 从设备模式，想让电脑/手机"把它当键盘鼠标/手柄/MIDI 设备"只能走蓝牙或网络。
- BlueZ 5.x 自带一个 BLE-MIDI GATT 服务（UUID `03B80E5A-…`）却关不掉，会和你的实现抢同一个 UUID；本项目直接用 [bumble](https://github.com/google/bumble) 以 `HCI_CHANNEL_USER` 独占适配器，绕开 BlueZ。
- 市面上常见的 BLE-MIDI 桥普遍有这么几个坑，本项目逐一修掉了（见「已修的坑」）。

## 安装

```bash
sudo apt install ./pi-midi-bridge_1.0.1_all.deb
```

安装时自动完成：载入 `snd-virmidi`、创建虚拟环境并安装 `bumble`、注册并启动 `pi-midi-bridge.service`、让 BlueZ 让出蓝牙适配器。RTP 通道需要另外装 `rtpmidid`（`Recommends`）。

## 使用

```bash
sudo systemctl status pi-midi-bridge     # 状态
sudo systemctl reload pi-midi-bridge     # 热重载：不断蓝牙链路，重开 ALSA 口 + 重建订阅
tail -f /var/log/pi-midi-bridge.log      # 每 30 秒打一行 [体检] 计数
pi-midi-bridge --version                 # 版本
```

**Mac 端**

1. 「音频 MIDI 设置」→ 窗口菜单「蓝牙配置」→ 看到 `Pi-MIDI` 点「连接」；
2. 或「窗口 → MIDI 网络设置」→ 目录里选 `rpi3`（就是树莓派的主会话端口）→ 打开「已启用」→ 连接。

**Windows 端**：装 rtpMIDI（Tobias Erichsen），添加 `rpi3` 的地址即可。

## 配置

`/etc/default/pi-midi-bridge`（Debian conffile，升级不会覆盖）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HCI_ADDR` | `B8:27:EB:A9:C3:C4` | 按 MAC 自动定位 hci 索引；也可填索引或留空 |
| `VIRMIDI` | `/dev/snd/midiC2D0` | 桥使用的虚拟 MIDI 口 |
| `VIRMIDI_PORT` | `24:0` | 对应的 ALSA seq 端口 |
| `EATT` | `0` | BLE 增强 ATT，macOS 上必须关，否则通知全丢 |
| `DEBUG` | `0` | 1 = 打印每条消息的十六进制字节 |

## 已修的坑

| # | 问题 | 现象 |
|---|---|---|
| 1 | BLE-MIDI 时间戳被当成状态字节 | 时间戳可以是任意 ≥0x80（如 `0x96`），旧解析器解出 `ch7 0x90 226 144` 之类垃圾 |
| 2 | ALSA→BLE 方向错用 BLE 解析器 | 裸 MIDI `90 2c 7f` 的首字节被当包头吞掉，整条静默丢弃 |
| 3 | running status 续传包被丢 | virmidi 输出 `903c7f` 后跟 `3d7f`、`3e7f`…（省略状态字节），每发一条就清空 status → **系统性丢约 45% 音符** |
| 4 | SYSEX 被掐成单字节 | macOS 连接瞬间发 GM Reset 长包，旧解析器只吐一个 `0xF0` |
| 5 | 断开后不再广播 | 客户端断开后再也扫不到设备 |

修完后的实测（真实曲目《水边的阿狄丽娜》1927 条事件）：

| 方向 | 蓝牙 | RTP |
|---|---|---|
| 树莓派 → Mac | 1927/1927，σ 10.0 ms，P95 23.8 ms | 1927/1927，σ 23.6 ms，P95 36.0 ms |
| Mac → 树莓派 | 1926/1927（末尾 1 条被采集窗口截断） | 1926/1927（同上） |

## 已知限制

- **macOS 不会自动重连 BLE-MIDI**，树莓派重启后要手动点一次「连接」（用 `reload` 而不是 `restart` 可以减少重启）。
- **BLE 单连接**：Mac 连着时 iPad 连不进来（BLE 外设只允许一条链路）。
- **rtpmidid 一次写入多消息只处理第一条**，发送端需一条消息一个 `MIDIPacketList`。
- **RTP 抖动无法调参**：rtpmidid 26.01 没暴露 latency/journal 配置入口。


## 网页管理端（1.6.0）

### 连线表怎么用（三步）
1. 打开 `http://<树莓派IP>:8080` → 「连线图」标签
2. 表格一行一条连线：左列选「源端口」（MIDI 从哪出来），右列选「目的端口」（送到哪去）
3. 点「应用」下发；状态栏会显示 `未应用：新增 n / 删除 m` 或 `与当前生效一致`

下面的拓扑图只是**效果预览**：橙色实线=外部 RTP 接口，灰色虚线=内部桥接，点线可跳到对应行。

常用端口对照：

| 端口 | 是什么 |
|---|---|
| `24:0` VirMIDI 2-0 | 桥接程序用的虚拟口（蓝牙路径从这进出） |
| `25:0` VirMIDI 2-1 | 备用注入口 |
| `128:0` Network Export | RTP-MIDI 网关出口 |
| `128:2/3/…` | 每个 RTP 对端一台设备一个端口 |
（1.1.0 起，1.3.0 为当前版本）

装好后浏览器打开 `http://<树莓派IP>:8080`，不用登 SSH 也能配路由。1.2.0 起有四个页签：**连线图 / 路由表 / 预设 / 监视器**。

- **连线图**：eNSP 风格拓扑画布，每个 ALSA client 一台设备；**从端口拖到端口画线连接**，橙色实线=外部 RTP 接口、灰色虚线=内部虚拟口；可只看外部/只看内部。原描述：每个 ALSA client 一列，点一个端口选中源、再点第二个端口即连接；点已有连线（绿色曲线）确认后断开。
- **预设**：把当前所有连线存成一个命名预设，之后一键回放（先清掉现有非 System 连接再重建，不会叠加成环）。
- **监视器**：选端口后起 `aseqdump`，实时打印该端口发出的 MIDI 消息（注意：只能看到这个端口自己发出的事件，路由进来的数据不会显示；调试 RTP 入站请选对应的 `128:x`）。
- **路由表**：精确选源/目的，支持单向、双向、断开。
- **结构化监视**：每条消息拆成端口 / 方向(IN·OUT) / 通道 / 事件类型 / 数据 / 音名，可按设备、方向、类型、通道四项过滤；悬停看原始行。
- **全局监视**：监视器第一项 `★ 全部端口` 可一次订阅所有可用端口（自动剔除 Midi Through / PipeWire 等订阅不了的口）。
- **开机自动恢复**：预设页底部开关，打开后树莓派重启会把上次的连线自动重建。

原 1.1.0 的功能：

- **蓝牙**：看到广播状态、当前连的是谁（`Pi-MIDI` 同一时刻只支持 1 条链路）
- **RTP-MIDI 设备**：列出网络里发现的设备（点「连接」直接接上）和已连对端（显示延迟，可断开）；也能手填 `IP:端口` 主动连一台不在同一网段/不广播的设备
- **MIDI 路由矩阵**：任选两个 ALSA 端口连/断（双向可选），把「蓝牙 ↔ RTP 对端 A ↔ RTP 对端 B」随意串起来
- 访问控制：`/etc/default/pi-midi-web` 里 `WEB_TOKEN` 留空=局域网谁都能开，填了=页面要求输入

后端 `pi-midi-web` 是纯标准库（无第三方依赖），跟 rtpmidid 的控制 socket 通信，路由本身用 `aconnect` 完成。

## 多设备协同能做到什么程度

| 场景 | 是否可行 | 说明 |
|---|---|---|
| 多台电脑/手机同时走 RTP-MIDI | ✅ | rtpmidid 每个对端导出一个 `128:x` 端口，路由矩阵可任意两两相接；树莓派当星型 hub |
| 蓝牙设备同时多台 | ❌ | BLE 外设协议层只允许 1 条链路，只能轮流连 |
| 蓝牙设备 + 多台 RTP 设备混用 | ✅ | 蓝牙那台经 `24:0` 广播给所有 RTP 对端 |
| MIDI Clock / 传输同步 | ✅（毫秒级） | 把一台 master 的 `0xF8` 通过路由矩阵转发出去即可；实测 RTP 抖动 σ≈20ms、蓝牙 σ≈10ms |
| 音频级严格同步（sample-accurate） | ❌ | 没有 PTP/硬件时钟，RTP-MIDI 的 journal 也不能配延迟 |

## 自行构建

```bash
echo 1.0.2 > VERSION                 # 改版本号
python3 pkg/make_pkg.py              # 生成 build/pi-midi-bridge_<ver>_all/
dpkg-deb --build --root-owner-group build/pi-midi-bridge_<ver>_all \
         build/pi-midi-bridge_<ver>_all.deb
```

`dpkg-deb` 必须在 Linux 上跑（树莓派本机、容器或 GitHub Actions 都行）；CI 见 `.github/workflows/build.yml`。

## 目录结构

```
src/pi_midi_bridge.py     桥主程序（bumble 版 BLE-MIDI 外设 + ALSA 双向转发）
src/hw_init.sh            启动前让出 HCI、载入 virmidi
src/wire_alsa.sh          建立 virmidi ↔ rtpmidid 的订阅
src/rewire_virmidi.sh     热重载后重建订阅
src/pi-midi-bridge.service / src/pi-midi-bridge.default
pkg/make_pkg.py           组装 deb
```

## 许可

MIT。
