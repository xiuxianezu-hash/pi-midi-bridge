#!/usr/bin/env python3
"""pi-midi-web —— MIDI 路由的网页管理端（纯标准库，无第三方依赖）

提供：
  GET  /                    单页管理界面（路由矩阵 / 连线图 / 预设 / 监视器）
  GET  /api/state           蓝牙状态 / RTP 对端 / ALSA 端口与连接 / 预设列表
  POST /api/alsa            连接或断开两个 ALSA 端口
  POST /api/rtp             连接远端 RTP-MIDI 设备 / 移除对端
  POST /api/service         重载桥接服务（需 root）
  GET  /api/presets         列出预设
  POST /api/presets         save / apply / delete
  GET  /api/monitor         轮询监视缓冲（port=&since=）
  POST /api/monitor         开/关某端口的 MIDI 监视

设计要点：rtpmidid 会把每个 RTP 对端导出成 ALSA seq 端口（client 128:x），
所以"多设备互通"不用动 rtpmidid 内部路由，只用 aconnect 把端口两两相连即可。
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.8.1"
WEBROOT = os.environ.get("WEBROOT", "/usr/share/pi-midi-bridge/web")
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
TOKEN = os.environ.get("WEB_TOKEN", "")
LOG = os.environ.get("BRIDGE_LOG", "/var/log/pi-midi-bridge.log")
RTPD_INI = os.environ.get("RTPMIDID_INI", "/etc/rtpmidid/default.ini")
PRESET_FILE = os.environ.get("PRESET_FILE", "/var/lib/pi-midi-bridge/presets.json")
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/pi-midi-bridge/state.json")


# ---------------------------------------------------------------- rtpmidid RPC
def rtp_control_path() -> str:
    try:
        with open(RTPD_INI) as f:
            for line in f:
                line = line.strip()
                if line.startswith("control="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "/var/run/rtpmidid/control.sock"


def rtp_rpc(method: str, params=None, timeout=4.0):
    req = json.dumps({"method": method, "params": params or []}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(rtp_control_path())
            s.sendall(req.encode())
            buf = b""
            while b"\n" not in buf and len(buf) < 4_000_000:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except Exception as exc:                      # noqa: BLE001
        return {"error": f"连不上 rtpmidid 控制口: {exc}"}
    try:
        return json.loads(buf.decode(errors="replace").strip().splitlines()[-1])
    except Exception as exc:                      # noqa: BLE001
        return {"error": f"应答解析失败: {exc}"}


# ---------------------------------------------------------------- ALSA 解析
PORT_RE = re.compile(r"^\s+(\d+)\s+'([^']*)'")
CLIENT_RE = re.compile(r"^client\s+(\d+):\s+'([^']*)'\s+\[(.*?)\]")
LINK_RE = re.compile(r"^\s+(Connecting To|Connected From):\s*(.*)$")


def alsa_state():
    out = subprocess.run(["aconnect", "-l"], capture_output=True, text=True, timeout=10).stdout
    clients, cur_client, cur_port = [], None, None
    for line in out.splitlines():
        if m := CLIENT_RE.match(line):
            cur_client = {"id": int(m.group(1)), "name": m.group(2),
                          "type": m.group(3), "ports": []}
            clients.append(cur_client)
            cur_port = None
            continue
        if cur_client is None:
            continue
        if m := PORT_RE.match(line):
            cur_port = {"id": int(m.group(1)), "name": m.group(2), "to": [], "from": []}
            cur_client["ports"].append(cur_port)
            continue
        if cur_port is not None and (m := LINK_RE.match(line)):
            key = "to" if m.group(1) == "Connecting To" else "from"
            cur_port[key] = [x.strip() for x in m.group(2).split(",") if x.strip()]
    return clients


def alsa_links():
    """去重后的 [[src, dst], ...]"""
    seen, out = set(), []
    for c in alsa_state():
        for p in c["ports"]:
            src = f"{c['id']}:{p['id']}"
            for dst in p["to"]:
                if (src, dst) not in seen:
                    seen.add((src, dst))
                    out.append([src, dst])
    return out


def all_ports_list():
    """非 System 的所有端口，形如 ['24:0','25:0','128:1']"""
    out = []
    for c in alsa_state():
        # System / MIDI Through 这类 kernel 端口无法被订阅，拉进来会让 aseqdump 直接退出
        if c["id"] == 0 or "Through" in c["name"] or "PipeWire" in c["name"]:
            continue
        for p in c["ports"]:
            out.append(f"{c['id']}:{p['id']}")
    return out


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def record_links():
    """把当前非 System 连线记进状态文件，供重启后恢复"""
    st = load_state()
    st["links"] = [[a, b] for a, b in alsa_links() if not (a.startswith("0:") or b.startswith("0:"))]
    st["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    return st


# ---------------------------------------------------------------- 蓝牙状态
LINK_RE_BLE = re.compile(r"\[链路\]\s*(.*)")


def ble_state():
    state = {"connected": False, "peer": None, "advertising": False,
             "note": "BLE 外设同一时刻只支持 1 条链路"}
    try:
        size = os.path.getsize(LOG)
        with open(LOG, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode(errors="replace")
    except OSError:
        return state
    for line in tail.splitlines():
        if m := LINK_RE_BLE.match(line.strip()):
            body = m.group(1)
            if "已连接" in body:
                pm = re.search(r"已连接\s+(\S+)", body)
                state["connected"] = True
                state["peer"] = pm.group(1) if pm else body
            elif "断开" in body or "已取消" in body:
                state["connected"] = False
                state["peer"] = None
        if "广播中" in line:
            state["advertising"] = True
    return state


# ---------------------------------------------------------------- MIDI 监视器
PORT_LINE = re.compile(r"^\s*(\d+):(\d+)\s+(.+)$")
TAIL_NUM = re.compile(r"^(\d+),\s*(.+)$")
NOTE_NUM = re.compile(r"note\s+(\d{1,3})")
CC_NUM = re.compile(r"controller\s+(\d{1,3}).*?value\s+(-?\d{1,4})")
VEL_NUM = re.compile(r"velocity\s+(-?\d{1,4})")
PROG_NUM = re.compile(r"program\s+(\d{1,3})")
PB_NUM = re.compile(r"value\s+(-?\d{1,5})")
SYSEX_RE = re.compile(r"^([0-9A-Fa-f ]{2,})$")

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(num):
    return f"{NOTE_NAMES[num % 12]}{num // 12 - 1}"


def parse_event(text):
    """把 aseqdump 一行拆成结构化字段"""
    m = PORT_LINE.match(text)
    if not m:
        return {"src": None, "event": text, "chan": None, "d1": None, "d2": None,
                "note": None, "text": text}
    src = f"{m.group(1)}:{m.group(2)}"
    body = m.group(3).strip()
    parts = re.split(r"\s{2,}", body, maxsplit=1)
    event = parts[0].strip()
    tail = parts[1].strip() if len(parts) > 1 else ""
    chan, d1, d2, note = None, None, None, None
    if tm := TAIL_NUM.match(tail):
        chan = int(tm.group(1))
        tail = tm.group(2)
    low = event.lower()
    if "note on" in low or "note off" in low:
        if nm := NOTE_NUM.search(tail):
            d1 = int(nm.group(1))
            note = note_name(d1)
        if vm := VEL_NUM.search(tail):
            d2 = int(vm.group(1))
    elif "control change" in low or "control" in low and "value" in low:
        if cm := CC_NUM.search(tail):
            d1, d2 = int(cm.group(1)), int(cm.group(2))
    elif "program" in low:
        if pm := PROG_NUM.search(tail):
            d1 = int(pm.group(1))
    elif "pitch" in low:
        if bm := PB_NUM.search(tail):
            d1 = int(bm.group(1))
            note = None
    elif "exclusive" in low:
        d1 = None
    return {"src": src, "event": event, "chan": chan, "d1": d1, "d2": d2,
            "note": note, "text": tail or event,
            "kind": event_kind(event)}


def event_kind(event):
    low = event.lower()
    if "note on" in low:
        return "note_on"
    if "note off" in low:
        return "note_off"
    if "control" in low:
        return "cc"
    if "program" in low:
        return "pc"
    if "pitch" in low:
        return "pb"
    if "exclusive" in low or "sysex" in low:
        return "sysex"
    if "clock" in low:
        return "clock"
    if "channel pressure" in low or "aftertouch" in low or "poly" in low:
        return "after"
    if "sensing" in low or "active" in low or "reset" in low or "start" in low or "stop" in low:
        return "realtime"
    return "other"
class Monitor:
    """对某端口跑 aseqdump，最近消息放环形缓冲里供网页轮询"""

    MAX = 400

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}
        self.buffers = {}
        self.seq = {}

    def _run(self, port, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if (not line or line.startswith("Waiting") or line.startswith("Port")
                        or line.startswith("Source")):
                    continue
                with self.lock:
                    buf = self.buffers.setdefault(port, [])
                    self.seq[port] = self.seq.get(port, 0) + 1
                    rec = parse_event(line)
                    rec["n"] = self.seq[port]
                    rec["t"] = round(time.time(), 3)
                    rec["raw"] = line
                    buf.append(rec)
                    if len(buf) > self.MAX:
                        del buf[:len(buf) - self.MAX]
        finally:
            with self.lock:
                self.procs.pop(port, None)

    def start(self, spec):
        with self.lock:
            if spec in self.procs and self.procs[spec].poll() is None:
                return True
            if spec == "all":
                cand = all_ports_list()
                for _ in range(8):
                    if not cand:
                        return False
                    target = ",".join(cand)
                    try:
                        r = subprocess.run(["aseqdump", "-p", target],
                                           capture_output=True, text=True, timeout=0.6)
                    except subprocess.TimeoutExpired:
                        break                      # 还活着 = 这批端口可用
                    m = re.search(r"Cannot connect from port (\d+:\d+)", (r.stdout + r.stderr))
                    if not m:
                        break                      # 其它错误，交给正式启动去暴露
                    cand.remove(m.group(1))
            else:
                target = spec
            if not target:
                return False
            self.buffers.setdefault(spec, [])
            self.seq.setdefault(spec, 0)
            try:
                p = subprocess.Popen(["aseqdump", "-p", target], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
            except Exception:                     # noqa: BLE001
                return False
            self.procs[spec] = p
        threading.Thread(target=self._run, args=(spec, p), daemon=True).start()
        return True

    def stop(self, spec):
        with self.lock:
            p = self.procs.pop(spec, None)
            self.buffers.pop(spec, None)
            self.seq.pop(spec, None)
        if p:
            p.terminate()

    def poll(self, port, since):
        with self.lock:
            buf = self.buffers.get(port, [])
            return {"running": port in self.procs, "last": self.seq.get(port, 0),
                    "msgs": [m for m in buf if m["n"] > since]}


MONITOR = Monitor()


# ---------------------------------------------------------------- 预设
def load_presets():
    try:
        with open(PRESET_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_presets(p):
    os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
    tmp = PRESET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PRESET_FILE)


def apply_links(pairs, clear_first=True):
    # 只保留两端都还存在的连线，避免把已消失的对端（RTP peer 重建后端口号会变）当成目标
    alive = {f"{c['id']}:{p['id']}" for c in alsa_state() if c["id"] != 0 for p in c["ports"]}
    usable = [[a, b] for a, b in pairs if a in alive and b in alive]
    skipped = len(pairs) - len(usable)
    if clear_first:
        for src, dst in alsa_links():
            if src.startswith("0:") or dst.startswith("0:"):   # System 端口不动
                continue
            subprocess.run(["aconnect", "-d", src, dst], capture_output=True, text=True)
    pairs = usable
    done, failed = 0, []
    for src, dst in pairs:
        p = subprocess.run(["aconnect", src, dst], capture_output=True, text=True)
        msg = (p.stdout + p.stderr)
        if p.returncode == 0 or "already subscribed" in msg:
            done += 1
        else:
            failed.append({"pair": [src, dst], "msg": (p.stdout + p.stderr).strip()})
    record_links()
    if skipped:
        print(f"[web] 跳过 {skipped} 条端口已不存在的连线", flush=True)
    return done, failed




# ---------------------------------------------------------------- MIDI 报文还原（给十六进制列用）
RT_BYTES = {"clock": 0xF8, "start": 0xFA, "continue": 0xFB, "stop": 0xFC,
            "active": 0xFE, "reset": 0xFF, "tune": 0xF9}
HEX_PAIRS = re.compile(r"\b([0-9A-Fa-f]{2})\b")


def midi_bytes(rec):
    """把 aseqdump 解析出来的字段还原成真实 MIDI 字节（MIDI-OX 那种十六进制列）"""
    kind, ch = rec.get("kind"), rec.get("chan") or 0
    d1, d2 = rec.get("d1"), rec.get("d2")
    if kind == "note_on" and d1 is not None and d2 is not None:
        return [0x90 | ch, d1 & 0x7F, d2 & 0x7F]
    if kind == "note_off" and d1 is not None and d2 is not None:
        return [0x80 | ch, d1 & 0x7F, d2 & 0x7F]
    if kind == "cc" and d1 is not None and d2 is not None:
        return [0xB0 | ch, d1 & 0x7F, d2 & 0x7F]
    if kind == "pc" and d1 is not None:
        return [0xC0 | ch, d1 & 0x7F]
    if kind == "after" and d1 is not None:
        return [0xD0 | ch, d1 & 0x7F]
    if kind == "pb" and d1 is not None:
        v = max(0, min(16383, d1 + 8192))
        return [0xE0 | ch, v & 0x7F, (v >> 7) & 0x7F]
    if kind == "sysex":
        hexes = HEX_PAIRS.findall(rec.get("text") or rec.get("event") or "")
        return [int(h, 16) for h in hexes] or None
    if kind == "realtime":
        low = (rec.get("event") or "").lower()
        for k, b in RT_BYTES.items():
            if k in low:
                return [b]
        if "song position" in low:
            return None
        return None
    return None


def midi_hex(rec):
    b = midi_bytes(rec)
    return " ".join(f"{x:02X}" for x in b) if b else ""


EVENT_LABEL = {"note_on": "音符开", "note_off": "音符关", "cc": "控制变化", "pc": "音色切换",
               "pb": "弯音", "sysex": "系统专用", "clock": "时钟", "after": "触后",
               "realtime": "实时", "other": "其它"}


def midi_desc(rec):
    """中文释义，形如「音符开 C5 力度127 · 通道1」"""
    kind = rec.get("kind")
    label = EVENT_LABEL.get(kind, "其它")
    note, d1, d2, ch = rec.get("note"), rec.get("d1"), rec.get("d2"), rec.get("chan")
    if kind in ("note_on", "note_off") and note:
        return f"{label} {note} 力度{d2} · 通道{ch}"
    if kind == "cc":
        return f"{label} 控制器{d1} = {d2} · 通道{ch}"
    if kind == "pc":
        return f"{label} 音色{d1} · 通道{ch}"
    if kind == "pb":
        return f"{label} {d1} · 通道{ch}"
    if kind == "after":
        return f"{label} {d1} · 通道{ch}"
    if kind == "sysex":
        return f"{label} {rec.get('text') or ''}".strip()
    if kind in ("clock", "realtime"):
        return rec.get("event") or label
    return (rec.get("text") or rec.get("event") or label).strip()


# ---------------------------------------------------------------- 纯观测：全局 MIDI 观察器
CAT_BLE = "蓝牙 Pi-MIDI"
CAT_VIR = "内部虚拟口"
CAT_HUB = "内部桥（RTP 引擎）"
CAT_RTP = "RTP 外部设备"
CAT_SYS = "系统"
CAT_OTHER = "其它"

HEALTH_RE = re.compile(r"\[体检\].*?累计\s+BLE收\s+(\d+)\s+发\s+(\d+)\s+丢\s+(\d+)")
PEER_RE = re.compile(r"已连接\s+(\S+)")


def bridge_health():
    """从桥日志里读最后一行的累计计数，纯展示用"""
    out = {"ble_recv": 0, "ble_sent": 0, "ble_drop": 0, "bridge_active": False}
    try:
        with open(LOG) as f:
            for line in f:
                if m := HEALTH_RE.search(line):
                    out["ble_recv"] = int(m.group(1))
                    out["ble_sent"] = int(m.group(2))
                    out["ble_drop"] = int(m.group(3))
    except OSError:
        pass
    try:
        out["bridge_active"] = subprocess.run(["systemctl", "is-active", "pi-midi-bridge"],
                                              capture_output=True, text=True).stdout.strip() == "active"
    except Exception:                                  # noqa: BLE001
        pass
    return out


def classify(client):
    """给每个 ALSA client / 端口归到人看得懂的类别"""
    name, cid = client["name"], client["id"]
    if cid == 0 or "Through" in name or "PipeWire" in name:
        return CAT_SYS, "system"
    if "Virtual Raw MIDI" in name or "VirMIDI" in name:
        return CAT_VIR, "virt"
    if cid == 128:
        return CAT_HUB, "hub"
    return CAT_OTHER, "other"


def device_index():
    """端口 -> 设备key、设备清单。RTP 对端在 rtpmidid 里是 128:x 端口，按端口拆成独立设备"""
    port_key, devices, ignore = {}, [], set()
    for c in alsa_state():
        # 观察器自己的 aseqdump 客户端会以 129:0 出现，属于测量噪声，不进设备表
        if "aseqdump" in c["name"].strip().lower():
            for p2 in c["ports"]:
                ignore.add(f"{c['id']}:{p2['id']}")
            continue
        cat, tag = classify(c)
        if tag == "system":
            continue
        for p in c["ports"]:
            pid = f"{c['id']}:{p['id']}"
            if c["id"] == 128 and p["id"] == 0:
                key, name, cat2 = "hub", "RTP 引擎导出口", CAT_HUB
            elif c["id"] == 128:
                # rtpmidid 每次重连都新注册一个 128:x 端口，同名对端合并成一个设备
                key, name, cat2 = "rtp:" + (p["name"].strip() or pid), p["name"].strip() or pid, CAT_RTP
            else:
                key, name, cat2 = pid, (c["name"] if len(c["ports"]) == 1
                                        else f"{c['name']} / {p['name'].strip()}"), cat
            port_key[pid] = key
            for d in devices:
                if d["key"] == key:
                    d["ports"].append(pid)
                    for x in p["to"]:
                        if x not in d["to"]:
                            d["to"].append(x)
                    for x in p["from"]:
                        if x not in d["from"]:
                            d["from"].append(x)
                    break
            else:
                devices.append({"key": key, "name": name, "cat": cat2, "port": pid,
                                "ports": [pid], "client": c["id"], "client_name": c["name"],
                                "to": list(p["to"]), "from": list(p["from"])})
    for d in devices:
        if len(d["ports"]) > 1:
            d["port"] = d["ports"][0] + " ~ " + d["ports"][-1] + "（" + str(len(d["ports"])) + " 口）"
    return port_key, devices, ignore


class Observer:
    """一条 aseqdump 看全部端口：OUT = 该端口自己发出，IN = 按当前 aconnect 路由推出来的下游"""

    MAX = 1500

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.n = 0
        self.proc = None
        self.spec = None
        self.port_key = {}
        self.devices = []
        self.ignore = set()
        self.links = {}
        self.counters = {}
        self.sig = ""
        self.notices = []
        self.note_n = 0
        self.names = {}          # key -> 显示名（避免每条事件都线性扫 devices）
        self.notes = []          # 轻量音符缓冲：只喂钢琴键盘，字段最小
        self.nn = 0
        self.prev_dev = None
        self.started = time.time()
        self.watchdog = threading.Thread(target=self._watch, daemon=True)

    # ---- 设备/路由快照
    def refresh(self, force=False):
        try:
            pk, dv, ig = device_index()
        except Exception:                              # noqa: BLE001
            return
        lk = {}
        for a, b in alsa_links():
            lk.setdefault(a, []).append(b)
        with self.lock:
            self.port_key, self.devices, self.links, self.ignore = pk, dv, lk, ig
            self.names = {d["key"]: d["name"] for d in dv}
            self.sig = "|".join(sorted(pk))
        self.diff_notice(dv)

    def notice(self, text, kind="info"):
        with self.lock:
            self.note_n += 1
            self.notices.append({"n": self.note_n, "t": round(time.time(), 3),
                                 "kind": kind, "text": text})
            if len(self.notices) > 300:
                del self.notices[:len(self.notices) - 300]

    def diff_notice(self, devs):
        """端口出现 / 消失 → 连接类提示"""
        cur = {(d["key"], d["name"]) for d in devs}
        if self.prev_dev is not None:
            for k, n in sorted(cur - self.prev_dev):
                self.notice(f"端口出现：{n}（{k}）", "conn")
            for k, n in sorted(self.prev_dev - cur):
                self.notice(f"端口消失：{n}（{k}）", "disc")
        self.prev_dev = cur

    def _watch(self):
        while True:
            time.sleep(5)
            self.refresh()
            cur = None
            with self.lock:
                cur = self.sig
            if cur and cur != self.spec:
                self.restart()

    # ---- aseqdump
    def restart(self):
        self.stop()
        cand = all_ports_list()
        for _ in range(8):
            if not cand:
                return
            target = ",".join(cand)
            try:
                r = subprocess.run(["aseqdump", "-p", target],
                                   capture_output=True, text=True, timeout=0.6)
            except subprocess.TimeoutExpired:
                break
            m = re.search(r"Cannot connect from port (\d+:\d+)", r.stdout + r.stderr)
            if not m:
                break
            cand.remove(m.group(1))
        if not cand:
            return
        target = ",".join(cand)
        try:
            p = subprocess.Popen(["aseqdump", "-p", target], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception:                              # noqa: BLE001
            return
        with self.lock:
            self.proc, self.spec = p, self.sig
        threading.Thread(target=self._run, args=(p,), daemon=True).start()
        print(f"[web] 观察器已订阅 {len(cand)} 个端口", flush=True)
        self.notice(f"观察器已订阅 {len(cand)} 个端口", "info")

    def stop(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
            except Exception:                          # noqa: BLE001
                pass

    def _bump(self, key, direction):
        c = self.counters.setdefault(key, {"out": 0, "in": 0})
        c[direction] = c.get(direction, 0) + 1

    def _run(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if (not line or line.startswith("Waiting") or line.startswith("Port")
                        or line.startswith("Source") or line.lower().startswith("timestamp")):
                    continue
                rec = parse_event(line)
                src = rec.get("src")
                if not src:
                    continue
                ev = (rec.get("event") or "").lower()
                if "subscrib" in ev:                       # ALSA 的端口订阅/取消订阅通告，不是 MIDI 数据
                    self.notice(("端口订阅：" if "unsub" not in ev else "端口取消订阅：")
                                + (rec.get("text") or ""), "info")
                    continue
                if src.startswith("0:"):                   # System 广播层，不进事件流
                    continue
                with self.lock:
                    key = self.port_key.get(src, src)
                    name = self.names.get(key) or src
                    self.n += 1
                    base = {"n": self.n, "t": round(time.time(), 3), "port": src,
                            "key": key, "name": name, "chan": rec["chan"],
                            "kind": rec["kind"], "event": rec["event"],
                            "d1": rec["d1"], "d2": rec["d2"], "note": rec["note"],
                            "hex": midi_hex(rec), "desc": midi_desc(rec), "raw": line}
                    self.events.append(dict(base, dir="out"))
                    self._bump(key, "out")
                    self._note(base, "out", key)
                    for dst in self.links.get(src, []):
                        if dst in self.ignore:
                            continue
                        k2 = self.port_key.get(dst, dst)
                        n2 = self.names.get(k2) or dst
                        self.n += 1
                        self.events.append(dict(base, n=self.n, dir="in", key=k2,
                                                name=n2, port=dst))
                        self._bump(k2, "in")
                        self._note(base, "in", k2)
                    if len(self.events) > self.MAX:
                        del self.events[:len(self.events) - self.MAX]
        finally:
            with self.lock:
                self.proc = None

    def _note(self, base, direction, key):
        """只有音符 / CC64 进轻量缓冲；钢琴面板靠它低延迟刷新"""
        kind = base["kind"]
        if kind not in ("note_on", "note_off", "cc"):
            return
        self.nn += 1
        self.notes.append({"nn": self.nn, "t": base["t"], "key": key,
                           "dir": direction, "kind": kind,
                           "d1": base["d1"], "d2": base["d2"]})
        if len(self.notes) > 4000:
            del self.notes[:len(self.notes) - 4000]

    # ---- 查询
    def since(self, since, since_note=0):
        with self.lock:
            return ([e for e in self.events if e["n"] > since], self.n,
                    list(self.devices), dict(self.counters), self.proc is not None,
                    [x for x in self.notices if x["n"] > since_note], self.note_n)

    def since_notes(self, since):
        with self.lock:
            return ([x for x in self.notes if x["nn"] > since], self.nn)


OBSERVER = Observer()
BLE_PREV = None
PEER_PREV = [None]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"pi-midi-web/{VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _ok(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._ok({"error": msg}, code)

    def _auth(self) -> bool:
        if not TOKEN:
            return True
        supplied = self.headers.get("X-Token") or ""
        if not supplied:
            from urllib.parse import urlparse, parse_qs
            supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return supplied == TOKEN

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads((self.rfile.read(n) if n else b"{}").decode() or "{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            if not self._auth():
                return self._err("需要 token", 401)
            return self._ok({
                "version": VERSION,
                "ble": ble_state(),
                "alsa": alsa_state(),
                "links": alsa_links(),
                "presets": load_presets(),
                "restore": {k: load_state().get(k) for k in ("auto", "links", "saved")},
                "rtp": (rtp_rpc("status").get("result") or {}),
            })
        if path == "/api/monitor":
            if not self._auth():
                return self._err("需要 token", 401)
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            port = (q.get("port") or [""])[0]
            since = int((q.get("since") or ["0"])[0] or 0)
            if not port:
                return self._err("需要 port")
            return self._ok(MONITOR.poll(port, since))
        if path == "/api/notes":
            if not self._auth():
                return self._err("需要 token", 401)
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0] or 0)
            evs, last = OBSERVER.since_notes(since)
            return self._ok({"events": evs, "last": last})
        if path == "/api/observe":
            if not self._auth():
                return self._err("需要 token", 401)
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0] or 0)
            since_note = int((q.get("note_since") or ["0"])[0] or 0)
            evs, last, devs, counters, running, notices, note_last = OBSERVER.since(since, since_note)
            bl = ble_state()
            global BLE_PREV
            if BLE_PREV is not None and bl["connected"] != BLE_PREV.get("connected"):
                OBSERVER.notice(("蓝牙链路已连接：" + str(bl["peer"])) if bl["connected"]
                                else "蓝牙链路断开（回到广播）", "conn" if bl["connected"] else "disc")
            if BLE_PREV is not None and bl["advertising"] != BLE_PREV.get("advertising"):
                OBSERVER.notice("BLE 开始广播 Pi-MIDI" if bl["advertising"] else "BLE 停止广播",
                                "info")
            BLE_PREV = bl
            rt = rtp_rpc("status").get("result") or {}
            SKIP_PEER = ("aseqdump", "network export", "midi through", "virtual raw midi",
                         "rpi3", "<->")
            seen_peer, peers = set(), []
            for r in (rt.get("router") or []):
                nm = (r.get("name") or "").strip()
                low = nm.lower()
                if not nm or any(k in low for k in SKIP_PEER) or nm in seen_peer:
                    continue
                seen_peer.add(nm)
                peers.append({"name": nm, "port": r.get("port"),
                              "latency": (r.get("peer") or {}).get("latency_ms", {}).get("average"),
                              "recv": ((r.get("peer") or {}).get("stats") or {}).get("recv", 0),
                              "sent": ((r.get("peer") or {}).get("stats") or {}).get("sent", 0),
                              "type": r.get("type")})
            names = {p["name"] for p in peers}
            if PEER_PREV[0] is not None:
                for nm in sorted(names - PEER_PREV[0]):
                    OBSERVER.notice("RTP 对端加入：" + nm, "conn")
                for nm in sorted(PEER_PREV[0] - names):
                    OBSERVER.notice("RTP 对端离开：" + nm, "disc")
            PEER_PREV[0] = names
            return self._ok({"version": VERSION, "ble": ble_state(),
                             "bridge": bridge_health(), "rtp": {"peers": peers},
                             "devices": devs, "counters": counters,
                             "events": evs, "last": last, "running": running,
                             "notices": notices, "note_last": note_last})
        if path == "/api/presets":
            if not self._auth():
                return self._err("需要 token", 401)
            return self._ok(load_presets())
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEBROOT, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                return self._err("找不到 index.html", 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/token":
            return self._ok({"required": bool(TOKEN)})
        return self._err("未知路径", 404)

    def do_POST(self):
        if not self._auth():
            return self._err("需要 token", 401)
        path = self.path.split("?")[0]
        try:
            data = self._body()
        except Exception as exc:                  # noqa: BLE001
            return self._err(f"请求体不是合法 JSON: {exc}")

        if path == "/api/alsa":
            return self._alsa(data)
        if path == "/api/rtp":
            return self._rtp(data)
        if path == "/api/monitor":
            port = (data.get("port") or "").strip()
            if not port or (port != "all" and ":" not in port):
                return self._err("port 需形如 24:0 或 all")
            if data.get("op") == "stop":
                MONITOR.stop(port)
                return self._ok({"ok": True, "running": False})
            return self._ok({"ok": MONITOR.start(port), "port": port})
        if path == "/api/presets":
            return self._presets(data)
        if path == "/api/autorestore":
            st = load_state()
            if "enabled" in data:
                st["auto"] = bool(data["enabled"])
            if data.get("snapshot"):
                record_links()
            save_state(st)
            st = load_state()
            return self._ok({"ok": True, "auto": st.get("auto", False),
                             "links": st.get("links", []), "saved": st.get("saved")})
        if path == "/api/service":
            try:
                p = subprocess.run(["systemctl", data.get("op", "reload"), "pi-midi-bridge"],
                                   capture_output=True, text=True, timeout=30)
                return self._ok({"status": p.returncode, "out": p.stdout.strip(),
                                 "err": p.stderr.strip()})
            except Exception as exc:              # noqa: BLE001
                return self._err(str(exc), 500)
        return self._err("未知路径", 404)

    # -- 业务
    def _alsa(self, data):
        op = data.get("op")
        if op == "sync":
            # 一次性对齐：表格里的连线 = 目标状态，与当前实际做差集，只动非 System 连线
            want = []
            for it in (data.get("links") or []):
                if not isinstance(it, (list, tuple)) or len(it) < 2:
                    continue
                a, b = it[0], it[1]
                if isinstance(a, str) and isinstance(b, str) and ":" in a and ":" in b:
                    if [a, b] not in want:
                        want.append([a, b])
            alive = {f"{c['id']}:{p['id']}" for c in alsa_state() if c["id"] != 0 for p in c["ports"]}
            cur = [[a, b] for a, b in alsa_links() if not a.startswith("0:") and not b.startswith("0:")]
            added, removed, skipped, kept = [], [], [], []
            for a, b in want:
                if a not in alive or b not in alive:
                    skipped.append([a, b])
                elif [a, b] in cur:
                    kept.append([a, b])
                else:
                    subprocess.run(["aconnect", a, b], capture_output=True, text=True); added.append([a, b])
            for a, b in cur:
                if [a, b] not in want:
                    subprocess.run(["aconnect", "-d", a, b], capture_output=True, text=True); removed.append([a, b])
            record_links()
            print(f"[web] 连线表应用：新增 {len(added)} / 保留 {len(kept)} / 删除 {len(removed)} / 跳过 {len(skipped)}", flush=True)
            return self._ok({"ok": True, "added": added, "removed": removed,
                             "skipped": skipped, "kept": kept})
        src, dst = data.get("src"), data.get("dst")
        if not (src and dst) or ":" not in src or ":" not in dst:
            return self._err("src/dst 需形如 24:0")
        cmd = {"link": ["aconnect", src, dst], "unlink": ["aconnect", "-d", src, dst]}
        if op not in cmd:
            return self._err("op 只能是 link / unlink")
        p = subprocess.run(cmd[op], capture_output=True, text=True, timeout=10)
        out = (p.stdout + p.stderr).strip()
        if op == "link" and data.get("both"):
            subprocess.run(["aconnect", dst, src], capture_output=True, text=True)
        record_links()
        return self._ok({"ok": True,
                         "out": out or ("已连接" if op == "link" else "已断开")})

    def _rtp(self, data):
        op = data.get("op")
        if op == "connect":
            host = (data.get("hostname") or "").strip()
            if not host:
                return self._err("需要 hostname")
            # rtpmidid 只接受 [name, hostname, port] 三个字符串
            res = rtp_rpc("connect", [data.get("name") or host, host,
                                      str(int(data.get("port") or 5004))])
        elif op in ("remove", "router.remove"):
            if data.get("id") is None:
                return self._err("需要 id")
            res = rtp_rpc("router.remove", [int(data["id"])])
        else:
            return self._err("op 只能是 connect / remove")
        if res.get("error"):
            return self._ok({"ok": False, "error": res["error"]})
        return self._ok({"ok": True, "result": res.get("result")})

    def _presets(self, data):
        op = data.get("op")
        presets = load_presets()
        if op == "save":
            name = (data.get("name") or "").strip()
            if not name:
                return self._err("需要 name")
            pairs = data.get("pairs") or alsa_links()
            pairs = [[a, b] for a, b in pairs if not (a.startswith("0:") or b.startswith("0:"))]
            presets[name] = {"pairs": pairs, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_presets(presets)
            return self._ok({"ok": True, "presets": presets})
        if op == "apply":
            name = (data.get("name") or "").strip()
            if name not in presets:
                return self._err("没有这个预设")
            done, failed = apply_links(presets[name]["pairs"], data.get("clear", True))
            return self._ok({"ok": True, "done": done, "failed": failed})
        if op == "delete":
            name = (data.get("name") or "").strip()
            presets.pop(name, None)
            save_presets(presets)
            return self._ok({"ok": True, "presets": presets})
        return self._err("op 只能是 save / apply / delete")


def restore_on_boot():
    st = load_state()
    if not st.get("auto") or not st.get("links"):
        return
    time.sleep(8)                      # 等 rtpmidid / 桥接把端口建齐
    done, failed = apply_links(st["links"], clear_first=True)
    if failed:
        time.sleep(3)
        done2, failed2 = apply_links(failed and [f["pair"] for f in failed] or [], clear_first=False)
        done += done2
        failed = failed2
    print(f"[web] 开机恢复上次路由：成功 {done} 条，失败 {len(failed)} 条", flush=True)


def main():
    os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
    OBSERVER.refresh(force=True)
    OBSERVER.watchdog.start()
    OBSERVER.restart()
    threading.Thread(target=restore_on_boot, daemon=True).start()
    print(f"[web] pi-midi-web {VERSION} 监听 {BIND}:{PORT} token={'有' if TOKEN else '无'}",
          flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"pi-midi-web {VERSION}")
        raise SystemExit(0)
    main()
