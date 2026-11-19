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

VERSION = "1.2.0"
WEBROOT = os.environ.get("WEBROOT", "/usr/share/pi-midi-bridge/web")
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
TOKEN = os.environ.get("WEB_TOKEN", "")
LOG = os.environ.get("BRIDGE_LOG", "/var/log/pi-midi-bridge.log")
RTPD_INI = os.environ.get("RTPMIDID_INI", "/etc/rtpmidid/default.ini")
PRESET_FILE = os.environ.get("PRESET_FILE", "/var/lib/pi-midi-bridge/presets.json")


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
                    buf.append({"n": self.seq[port], "t": round(time.time(), 3), "text": line})
                    if len(buf) > self.MAX:
                        del buf[:len(buf) - self.MAX]
        finally:
            with self.lock:
                self.procs.pop(port, None)

    def start(self, port):
        with self.lock:
            if port in self.procs and self.procs[port].poll() is None:
                return True
            self.buffers.setdefault(port, [])
            try:
                p = subprocess.Popen(["aseqdump", "-p", port], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
            except Exception:                     # noqa: BLE001
                return False
            self.procs[port] = p
        threading.Thread(target=self._run, args=(port, p), daemon=True).start()
        return True

    def stop(self, port):
        with self.lock:
            p = self.procs.pop(port, None)
            self.buffers.pop(port, None)
            self.seq.pop(port, None)
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
    if clear_first:
        for src, dst in alsa_links():
            if src.startswith("0:") or dst.startswith("0:"):   # System 端口不动
                continue
            subprocess.run(["aconnect", "-d", src, dst], capture_output=True, text=True)
    done, failed = 0, []
    for src, dst in pairs:
        p = subprocess.run(["aconnect", src, dst], capture_output=True, text=True)
        msg = (p.stdout + p.stderr)
        if p.returncode == 0 or "already subscribed" in msg:
            done += 1
        else:
            failed.append({"pair": [src, dst], "msg": (p.stdout + p.stderr).strip()})
    return done, failed


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
            if not port or ":" not in port:
                return self._err("port 需形如 24:0")
            if data.get("op") == "stop":
                MONITOR.stop(port)
                return self._ok({"ok": True, "running": False})
            return self._ok({"ok": MONITOR.start(port), "port": port})
        if path == "/api/presets":
            return self._presets(data)
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
        op, src, dst = data.get("op"), data.get("src"), data.get("dst")
        if not (src and dst) or ":" not in src or ":" not in dst:
            return self._err("src/dst 需形如 24:0")
        cmd = {"link": ["aconnect", src, dst], "unlink": ["aconnect", "-d", src, dst]}
        if op not in cmd:
            return self._err("op 只能是 link / unlink")
        p = subprocess.run(cmd[op], capture_output=True, text=True, timeout=10)
        out = (p.stdout + p.stderr).strip()
        if op == "link" and data.get("both"):
            subprocess.run(["aconnect", dst, src], capture_output=True, text=True)
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


def main():
    os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
    print(f"[web] pi-midi-web {VERSION} 监听 {BIND}:{PORT} token={'有' if TOKEN else '无'}",
          flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"pi-midi-web {VERSION}")
        raise SystemExit(0)
    main()
