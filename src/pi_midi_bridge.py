#!/usr/bin/env python3
"""BLE-MIDI 外设 + RTP-MIDI 桥（bumble 版）

结构：
  Mac/iPhone  --BLE-MIDI-->  本脚本  --raw MIDI-->  /dev/snd/midiC2D0
                              ▲                        │
                              └────── raw MIDI ────────┘
                                    （snd-virmidi 虚拟口，再经 ALSA seq 接到 rtpmidid 的 RTP 会话）

要先：
  modprobe snd-virmidi          # 生成 /dev/snd/midiC2D0..3 和 ALSA seq 端口
  systemctl start rtpmidid      # RTP-MIDI 守护进程
"""
import asyncio
import logging
import signal
import os
import select
import sys
import threading

from bumble.core import AdvertisingData, UUID
from bumble.device import Device, DeviceConfiguration
from bumble.gatt import Attribute, Characteristic, CharacteristicValue, Service
from bumble.hci import Address, OwnAddressType
from bumble.transport import open_transport

SVC = "03b80e5a-ede8-4b33-a751-6ce34ec4c700"
CHR = "7772e5db-3868-4112-a1a9-f2669d106bf3"
ADDRESS = "B8:27:EB:A9:C3:C4"
VIRMIDI = os.environ.get("VIRMIDI", "/dev/snd/midiC2D0")
DEBUG = os.environ.get("DEBUG", "0") == "1"

# 体检计数器：任何"静默跳过/失败"都要能看见，避免再次无声丢音
STATS = {
    "ble_in": 0,        # 收到 BLE 消息数
    "alsa_out": 0,      # 写入 ALSA 的消息数
    "alsa_in_bytes": 0, # 从 ALSA 读到的字节数
    "ble_out": 0,       # 发给 BLE 的消息数
    "drop_nostatus": 0, # 裸 MIDI 流里没有状态字节、无从解析的字节
    "err_read": 0,
    "err_write": 0,
    "fail_notify": 0,
}


def find_le_adapter() -> str:
    """按 MAC 地址找板载 BLE 适配器的 hci 索引（重启后索引会变，不能写死）"""
    if os.environ.get("HCI"):
        return os.environ["HCI"]
    for i in range(8):
        try:
            with open(f"/sys/class/bluetooth/hci{i}/address") as f:
                if f.read().strip().upper() == ADDRESS:
                    return str(i)
        except OSError:
            continue
    return "0"


_value = bytearray([0x80, 0x00, 0x00])


_LEN = {0x8: 3, 0x9: 3, 0xA: 3, 0xB: 3, 0xC: 2, 0xD: 2, 0xE: 3, 0xF: 1}


def parse_midi(data: bytes):
    """按 BLE-MIDI v1 规范解帧：包头 0x81 → [时间戳字节][MIDI 消息]...

    时间戳字节可以是任意 >=0x80 的值（不只是 0x80~0x82），
    所以只能靠"消息边界"判断，不能靠具体数值判断。
    """
    out = []
    if not data:
        return out
    i = 1 if (data[0] & 0x80) else 0          # 跳过包头（0x81 / 0x8x）
    status = None
    buf = bytearray()

    def emit():
        nonlocal status, buf
        if status is not None and len(buf) == _LEN.get(status >> 4, 0) - 1:
            out.append((f"ch{(status & 0x0F) + 1} {hex(status)} "
                        + " ".join(str(x) for x in buf), bytes([status]) + bytes(buf)))
        buf = bytearray()

    while i < len(data):
        b = data[i]
        if b == 0xF0:                          # SYSEX：整段透传，别按时间戳处理
            j = data.find(b"\xf7", i)
            if j < 0:
                break
            out.append(("sysex", data[i:j + 1]))
            i = j + 1
            status = None
            buf = bytearray()
            continue
        if b & 0x80:                          # 时间戳（一定出现在消息边界）
            emit()
            i += 1
            if i < len(data) and (data[i] & 0x80):   # 紧跟状态字节
                if data[i] == 0xF0:                  # SYSEX 交给外层整段处理
                    continue
                status = data[i]
                buf = bytearray()
                i += 1
            continue
        if status is None:                    # 没有状态字节，丢弃
            i += 1
            continue
        n = _LEN.get(status >> 4, 0) - 1
        if n <= 0:                            # 单字节消息（0xF 组）直接吐
            out.append((f"ch{(status & 0x0F) + 1} {hex(status)}", bytes([status])))
            status = None
            i += 1
            continue
        buf.append(b)
        i += 1
        if len(buf) == n:
            out.append((f"ch{(status & 0x0F) + 1} {hex(status)} "
                        + " ".join(str(x) for x in buf), bytes([status]) + bytes(buf)))
            buf = bytearray()
            status = None
    return out


def parse_raw_stream(data: bytes, state: dict):
    """解析裸 MIDI 数据流（ALSA → BLE 方向），跨包保留未完成的消息。"""
    buf = state.get("carry", b"") + bytes(data)
    out = []
    i = 0
    status = state.get("status")
    state["carry"] = b""

    def desc(st, payload):
        return f"ch{(st & 0x0F) + 1} {hex(st)} " + " ".join(str(x) for x in payload)

    while i < len(buf):
        b = buf[i]
        if b >= 0xF8:                      # 实时消息，单字节
            out.append((f"rt {hex(b)}", bytes([b])))
            i += 1
            continue
        if b == 0xF0:                      # SYSEX：等 F7
            j = buf.find(b"\xf7", i)
            if j < 0:
                state["carry"] = buf[i:]
                state["status"] = None
                return out, state
            out.append(("sysex", buf[i:j + 1]))
            i = j + 1
            status = None
            continue
        if b & 0x80:                       # 状态字节
            status = b
            i += 1
            n = _LEN.get(status >> 4, 1)
            if n <= 1:
                out.append((desc(status, b""), bytes([status])))
                # running status 继续保持（这条消息只需 1 个状态字节）
                continue
        if status is None:                 # 没有运行状态，丢弃（会计数）
            state["drop_nostatus"] = state.get("drop_nostatus", 0) + 1
            i += 1
            continue
        n = _LEN.get(status >> 4, 3) - 1
        payload = []
        while len(payload) < n and i < len(buf) and not (buf[i] & 0x80):
            payload.append(buf[i])
            i += 1
        if len(payload) < n:               # 不完整，留到下一包
            state["carry"] = bytes([status]) + bytes(payload)
            state["status"] = status
            return out, state
        out.append((desc(status, payload), bytes([status]) + bytes(payload)))
        # 保留 running status：下一包可能是 `3d 7f` 这种不带状态字节的续传
    state["status"] = status
    return out, state


class Bridge:
    def __init__(self):
        self.fd = None
        self.device = None
        self.char = None
        self.loop = None
        self.raw_state = {"status": None, "carry": b"", "drop_nostatus": 0}

    def open_virmidi(self):
        self.fd = os.open(VIRMIDI, os.O_RDWR | os.O_NONBLOCK)
        print(f"[ALSA] 已打开虚拟 MIDI 口 {VIRMIDI}", flush=True)

    async def ble_write(self, connection, value):
        global _value
        raw = bytes(value)
        _value = bytearray(raw)
        if DEBUG:
            print(f"[原始] {raw.hex()}", flush=True)
        for desc, msg in parse_midi(raw):
            print(f"[BLE→MIDI] {desc}  {msg.hex()}", flush=True)
            STATS["ble_in"] += 1
            try:
                os.write(self.fd, msg)
                STATS["alsa_out"] += 1
            except OSError as ex:
                STATS["err_write"] += 1
                print(f"[ALSA] 写入失败 {ex}", flush=True)

    def on_virmidi_data(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as ex:
            STATS["err_read"] += 1
            print(f"[ALSA] 读取失败 {ex}", flush=True)
            return
        if not data:
            return
        STATS["alsa_in_bytes"] += len(data)
        self.feed_from_alsa(data)

    def feed_from_alsa(self, data: bytes):
        """由读线程调用：解析后交回事件循环发通知。"""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._from_alsa, data)

    def _from_alsa(self, data: bytes):
        msgs, _ = parse_raw_stream(data, self.raw_state)
        for desc, msg in msgs:
            print(f"[MIDI→BLE] {desc}  {msg.hex()}", flush=True)
            STATS["ble_out"] += 1
            pkt = bytes([0x81, 0x80]) + msg      # BLE-MIDI 头 + 时间戳 + 消息
            asyncio.ensure_future(self.notify(pkt))

    async def notify(self, pkt: bytes):
        try:
            # force=True：即使订阅表因重连残留为空，也照样把通知发到每个连接，
            # 由客户端按 CCCD 自行过滤（Mac/Windows 的 GATT 缓存重连场景必需）
            await self.device.notify_subscribers(self.char, pkt, force=True)
        except Exception as ex:
            STATS["fail_notify"] += 1
            print(f"[BLE] 通知失败 {type(ex).__name__}: {ex}", flush=True)


bridge = Bridge()


async def main():
    global _value
    bridge.open_virmidi()

    hci_index = find_le_adapter()
    print(f"[bumble] 使用 hci{hci_index} ({ADDRESS})", flush=True)
    async with await open_transport(f"hci-socket:{hci_index}") as (hci_source, hci_sink):
        config = DeviceConfiguration(
            name="Pi-MIDI",
            address=Address(ADDRESS),
            eatt_enabled=(os.environ.get("EATT", "0") == "1"),
            # 支持配对/绑定：macOS 一旦绑定成功就会记住这台设备，
            # 断开后能自己重连（不绑定时 macOS 会把它当"陌生设备"忘掉）
            keystore=os.environ.get("KEYSTORE", "JsonKeyStore"),
            io_capability=3,  # NO_INPUT_NO_OUTPUT (Just Works)
            advertising_interval_min=100,   # 62.5ms，重连发现更快
            advertising_interval_max=150,
        )
        device = Device.from_config_with_hci(config, hci_source, hci_sink)
        bridge.device = device

        char = Characteristic(
            CHR,
            Characteristic.Properties.READ
            | Characteristic.Properties.WRITE
            | Characteristic.Properties.WRITE_WITHOUT_RESPONSE
            | Characteristic.Properties.NOTIFY,
            Attribute.Permissions.READABLE | Attribute.Permissions.WRITEABLE,
            CharacteristicValue(read=lambda conn: bytes(_value), write=bridge.ble_write),
        )
        bridge.char = char
        device.add_service(Service(SVC, [char]))

        device.advertising_data = bytes(
            AdvertisingData(
                [
                    (AdvertisingData.COMPLETE_LOCAL_NAME, b"Pi-MIDI"),
                    (
                        AdvertisingData.INCOMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
                        UUID(SVC).to_bytes(),
                    ),
                ]
            )
        )

        await device.power_on()
        async def advertise(tag=""):
            try:
                await device.start_advertising(
                    own_address_type=OwnAddressType.PUBLIC, auto_restart=True
                )
                if tag:
                    print(f"[bumble] 重新广播（{tag}）", flush=True)
            except Exception as ex:
                print(f"[bumble] 广播失败（{tag}）{ex}", flush=True)

        await advertise()
        print("[bumble] 已启动：Pi-MIDI / 广播中", flush=True)

        def _on_conn(conn):
            print(f"[链路] 已连接 {conn.peer_address}  当前连接数={len(device.connections)}", flush=True)

        def _on_disconn(conn, reason):
            print(f"[链路] 断开 {conn.peer_address} reason={reason}", flush=True)

            async def _re():
                await asyncio.sleep(1.0)
                if not device.connections:
                    await advertise("断开后")
            asyncio.create_task(_re())

        def _on_subscription(*args):
            print("[订阅] " + " | ".join(str(a) for a in args), flush=True)

        device.on("characteristic_subscription", _on_subscription)
        device.on("connection", _on_conn)
        device.on("disconnection", _on_disconn)

        loop = asyncio.get_running_loop()
        bridge.loop = loop

        def _alsa_reader():
            """独立线程读 virmidi，避免 add_reader 在蓝牙 HCI 事件循环里失联。"""
            print("[ALSA] 读线程已启动", flush=True)
            while True:
                try:
                    r, _, _ = select.select([bridge.fd], [], [], 0.5)
                except (OSError, ValueError) as ex:
                    print(f"[ALSA] select 异常 {ex}", flush=True)
                    return
                if not r:
                    continue
                try:
                    data = os.read(bridge.fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as ex:
                    print(f"[ALSA] 读取失败 {ex}", flush=True)
                    continue
                if not data:
                    continue
                bridge.feed_from_alsa(data)

        threading.Thread(target=_alsa_reader, daemon=True).start()
        print("[桥] BLE ↔ ALSA 双向转发已开启", flush=True)

        if os.environ.get("DEMO", "0") == "1":
            async def _demo():
                while True:
                    await asyncio.sleep(1.0)
                    await bridge.notify(bytes.fromhex("8180903c64"))
                    await asyncio.sleep(0.3)
                    await bridge.notify(bytes.fromhex("8180803c00"))
                    print("[DEMO] 发了一个 C4", flush=True)
            asyncio.create_task(_demo())

        async def _advert_watchdog():
            """控制器偶尔会把广播静默停掉，断开空闲时定期补一次。"""
            last_err = None
            while True:
                await asyncio.sleep(15)
                if device.connections:
                    continue
                try:
                    await device.start_advertising(
                        own_address_type=OwnAddressType.PUBLIC, auto_restart=True
                    )
                    if last_err is not None:
                        print("[bumble] 广播保活已恢复", flush=True)
                        last_err = None
                except Exception as ex:
                    msg = f"{type(ex).__name__}: {ex}"
                    if msg != last_err:
                        print(f"[bumble] 广播保活异常 {msg}", flush=True)
                        last_err = msg

        asyncio.create_task(_advert_watchdog())

        async def _health():
            """每 30 秒打一次体检：任何丢/错都必须显式出现，不许静默。"""
            prev = dict(STATS)
            while True:
                await asyncio.sleep(30)
                cur = dict(STATS)
                d = {k: cur[k] - prev.get(k, 0) for k in cur}
                prev = cur
                bad = (d["drop_nostatus"] + d["err_read"] + d["err_write"] + d["fail_notify"])
                line = (f"[体检] 30s内 BLE收 {d['ble_in']} / BLE发 {d['ble_out']} / "
                        f"ALSA写 {d['alsa_out']} / ALSA读 {d['alsa_in_bytes']}B / "
                        f"丢无状态 {d['drop_nostatus']} / 读错 {d['err_read']} / "
                        f"写错 {d['err_write']} / 通知失败 {d['fail_notify']} "
                        f"| 累计 BLE收 {cur['ble_in']} 发 {cur['ble_out']} 丢 {cur['drop_nostatus']}")
                print(line, flush=True)
                if bad == 0:
                    continue

        asyncio.create_task(_health())

        def _reload():
            """SIGHUP：热重载。只重开 ALSA 口 + 补广播，不断 BLE 链路。"""
            try:
                old = bridge.fd
                bridge.fd = os.open(VIRMIDI, os.O_RDWR | os.O_NONBLOCK)
                if old is not None:
                    try:
                        os.close(old)
                    except OSError:
                        pass
                bridge.raw_state = {"status": None, "carry": b"", "drop_nostatus": 0}
                print("[重载] ALSA 口已重开，解析状态已复位", flush=True)
            except OSError as ex:
                print(f"[重载] 重开 ALSA 失败 {ex}", flush=True)
            if not device.connections:
                asyncio.ensure_future(
                    device.start_advertising(own_address_type=OwnAddressType.PUBLIC, auto_restart=True)
                )
            print(f"[重载] 完成，当前连接 {len(device.connections)} 条", flush=True)

        try:
            loop.add_signal_handler(signal.SIGHUP, _reload)
            print("[桥] 支持 systemctl reload（SIGHUP 热重载，不断蓝牙链路）", flush=True)
        except (NotImplementedError, RuntimeError):
            pass

        await asyncio.sleep(3600 * 24)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stop]")
        sys.exit(0)
