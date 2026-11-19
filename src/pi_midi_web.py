#!/usr/bin/env python3
"""pi-midi-web —— MIDI 路由的网页管理端（纯标准库，无第三方依赖）

提供：
  GET  /                单页管理界面
  GET  /api/state       蓝牙状态 / RTP 对端 / ALSA 端口与连接
  POST /api/alsa        连接或断开两个 ALSA 端口（即设置 MIDI 路由）
  POST /api/rtp         连接远端 RTP-MIDI 设备 / 移除对端
  POST /api/service     重载桥接服务（需以 root 运行）

设计要点：rtpmidid 会把每个 RTP 对端导出成 ALSA seq 端口（client 128:x），
所以"多设备互通"不需要动 rtpmidid 内部路由，只用 aconnect 把端口两两相连即可。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"
WEBROOT = os.environ.get("WEBROOT", "/usr/share/pi-midi-bridge/web")
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
TOKEN = os.environ.get("WEB_TOKEN", "")          # 空 = 不校验
LOG = os.environ.get("BRIDGE_LOG", "/var/log/pi-midi-bridge.log")
RTPD_INI = os.environ.get("RTPMIDID_INI", "/etc/rtpmidid/default.ini")


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
    """跟 rtpmidid 的控制 socket 说话：一行 JSON 请求 → 一份 JSON 应答"""
    req = json.dumps({"method": method, "params": params or []}) + "\n"
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
    try:
        return json.loads(buf.decode(errors="replace").strip().splitlines()[-1])
    except Exception as exc:                      # noqa: BLE001
        return {"error": f"应答解析失败: {exc}", "raw": buf[:200].decode(errors="replace")}


# ---------------------------------------------------------------- ALSA 检查
PORT_RE = re.compile(r"^\s+(\d+)\s+'([^']*)'")
CLIENT_RE = re.compile(r"^client\s+(\d+):\s+'([^']*)'\s+\[(.*?)\]")
LINK_RE = re.compile(r"^\s+(Connecting To|Connected From):\s*(.*)$")


def alsa_state():
    """解析 aconnect -l，得到客户端 / 端口 / 订阅关系"""
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


# ---------------------------------------------------------------- 蓝牙状态
LINK_RE_BLE = re.compile(r"\[链路\]\s*(.*)")


def ble_state():
    """从桥日志读蓝牙链路状态（桥在连接/断开时会写 [链路] 行）"""
    state = {"connected": False, "peer": None, "since": None, "advertising": False, "note": ""}
    try:
        with open(LOG, "rb") as f:
            size = os.path.getsize(LOG)
            f.seek(max(0, size - 65536))
            tail = f.read().decode(errors="replace")
    except OSError:
        return state
    for line in tail.splitlines():
        if m := LINK_RE_BLE.match(line.strip()):
            body = m.group(1)
            if "已连接" in body:
                pm = re.search(r"已连接\s+(\S+).*?当前连接数=(\d+)", body)
                state["connected"] = True
                state["peer"] = pm.group(1) if pm else body
                state["since"] = line.split("]")[0].strip(" [")[:0] or state["since"]
            elif "断开" in body or "已取消" in body:
                state["connected"] = False
                state["peer"] = None
        if "广播中" in line:
            state["advertising"] = True
    state["note"] = "BLE 外设同一时刻只支持 1 条链路"
    return state


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"pi-midi-web/{VERSION}"

    def log_message(self, fmt, *args):           # 静音访问日志
        pass

    # -- 工具
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
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode() or "{}")

    # -- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            if not self._auth():
                return self._err("需要 token", 401)
            rtp = rtp_rpc("status")
            return self._ok({
                "version": VERSION,
                "ble": ble_state(),
                "alsa": alsa_state(),
                "rtp": rtp.get("result") or {"error": rtp.get("error")},
            })
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

    # -- POST
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
        src, dst = data.get("src"), data.get("dst")
        if not (src and dst) or ":" not in src or ":" not in dst:
            return self._err("src/dst 需形如 24:0")
        cmd = {"link": ["aconnect", src, dst],
               "unlink": ["aconnect", "-d", src, dst]}
        if op not in cmd:
            return self._err("op 只能是 link / unlink")
        if op == "link" and data.get("both"):
            pass
        try:
            p = subprocess.run(cmd[op], capture_output=True, text=True, timeout=10)
        except Exception as exc:                  # noqa: BLE001
            return self._err(str(exc), 500)
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0 and "Connection" not in out and "Rejected" not in out:
            return self._ok({"ok": False, "out": out})
        if op == "link" and data.get("both"):
            subprocess.run(["aconnect", dst, src], capture_output=True, text=True)
        return self._ok({"ok": True,
                         "out": ("已连接" if op == "link" else "已断开") if not out else out})

    def _rtp(self, data):
        op = data.get("op")
        if op == "connect":
            host = (data.get("hostname") or "").strip()
            if not host:
                return self._err("需要 hostname")
            # rtpmidid 只接受 [name, hostname, port] 三个字符串
            params = [data.get("name") or host, host, str(int(data.get("port") or 5004))]
            res = rtp_rpc("connect", params)
        elif op in ("remove", "router.remove"):
            if data.get("id") is None:
                return self._err("需要 id")
            res = rtp_rpc("router.remove", [int(data["id"])])
        elif op == "disconnect":
            res = rtp_rpc("router.disconnect", [int(data.get("id", 0)), int(data.get("target", 0))])
        else:
            return self._err("op 只能是 connect / remove / disconnect")
        if res.get("error"):
            return self._ok({"ok": False, "error": res["error"]})
        return self._ok({"ok": True, "result": res.get("result")})


def main():
    print(f"[web] pi-midi-web {VERSION} 监听 {BIND}:{PORT} token={'有' if TOKEN else '无'}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"pi-midi-web {VERSION}")
        raise SystemExit(0)
    main()
