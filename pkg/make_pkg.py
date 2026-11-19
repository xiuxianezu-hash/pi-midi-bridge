#!/usr/bin/env python3
"""把 src/ 里的源码组装成 Debian 包（树莓派可直接 apt install）

用法：
    python3 pkg/make_pkg.py                 # 读取 VERSION 文件
    python3 pkg/make_pkg.py 1.0.2           # 临时指定版本
    dpkg-deb --build --root-owner-group build/pi-midi-bridge_<ver>_all \
             build/pi-midi-bridge_<ver>_all.deb
"""
import os, shutil, sys, pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src"
VER = sys.argv[1] if len(sys.argv) > 1 else (REPO / "VERSION").read_text().strip()
OUT = REPO / "build" / f"pi-midi-bridge_{VER}_all"

# ——源码补丁：① 适配器 MAC 可用环境变量覆盖 ② --version 在导入 bumble 之前处理
bridge = (SRC / "pi_midi_bridge.py").read_text()
bridge = bridge.replace(
    'ADDRESS = "B8:27:EB:A9:C3:C4"',
    'ADDRESS = os.environ.get("HCI_ADDR", "B8:27:EB:A9:C3:C4")  # 可用 HCI_ADDR 覆盖',
)
bridge = bridge.replace(
    "import asyncio",
    'import asyncio\n'
    'import sys as _sys\n'
    'if "--version" in _sys.argv:\n'
    f'    print("pi-midi-bridge {VER}")\n'
    '    _sys.exit(0)',
    1,
)
bridge = bridge.replace(
    'VIRMIDI = os.environ.get("VIRMIDI", "/dev/snd/midiC2D0")',
    'VIRMIDI = os.environ.get("VIRMIDI", "/dev/snd/midiC2D0")',
)
bridge = bridge.replace(
    'if __name__ == "__main__":\n    logging.basicConfig(level=logging.INFO)',
    'if __name__ == "__main__":\n'
    f'    print(f"[start] pi-midi-bridge {VER}", flush=True)\n'
    '    logging.basicConfig(level=logging.INFO)',
)

FILES = {
    "usr/bin/pi-midi-bridge": ("755", bridge),
    "usr/lib/pi-midi-bridge/hw_init.sh": ("755", (SRC / "hw_init.sh").read_text()),
    "usr/lib/pi-midi-bridge/wire_alsa.sh": ("755", (SRC / "wire_alsa.sh").read_text()),
    "usr/lib/pi-midi-bridge/rewire_virmidi.sh": ("755", (SRC / "rewire_virmidi.sh").read_text()),
    "usr/lib/pi-midi-bridge/rtpmidid_patch.py": ("755", (SRC / "rtpmidid_patch.py").read_text()),
    "lib/systemd/system/pi-midi-bridge.service": ("644", (SRC / "pi-midi-bridge.service").read_text()),
    "etc/default/pi-midi-bridge": ("644", (SRC / "pi-midi-bridge.default").read_text()),
    "usr/share/doc/pi-midi-bridge/README.md": ("644", (REPO / "README.md").read_text()),
    "usr/share/doc/pi-midi-bridge/CHANGELOG.md": ("644", (REPO / "CHANGELOG.md").read_text()),
    "usr/bin/pi-midi-web": ("755", (SRC / "pi_midi_web.py").read_text()),
    "usr/share/pi-midi-bridge/web/index.html": ("644", (SRC / "web" / "index.html").read_text()),
    "lib/systemd/system/pi-midi-web.service": ("644", (SRC / "pi-midi-web.service").read_text()),
    "etc/default/pi-midi-web": ("644", (SRC / "pi-midi-web.default").read_text()),
    "usr/share/doc/pi-midi-bridge/copyright": ("644", "MIT License, see LICENSE\n"),
}

CONTROL = f"""Package: pi-midi-bridge
Version: {VER}
Architecture: all
Maintainer: {{maintainer}}
Section: sound
Priority: optional
Depends: python3 (>= 3.9), python3-venv, python3-pip, alsa-utils, bluez
Recommends: rtpmidid
Homepage: {{homepage}}
Description: BLE-MIDI 外设与 ALSA/RTP-MIDI 之间的树莓派 MIDI 路由器（含网页管理端）
 让树莓派同时充当 BLE-MIDI 外设（Mac/iPad/手机可直接连接）和
 RTP-MIDI 会话端点（rtpmidid），两者通过 snd-virmidi 虚拟 MIDI 口互通。
 内含解帧修复、running status 修复、SYSEX 透传、断连自动重广播、
 体检计数日志与热重载能力。
"""

POSTINST = """#!/bin/sh
set -e
VENV=/var/lib/pi-midi-bridge/venv
PKG=pi-midi-bridge

# 1) 停用并移除旧的手工部署单元（同一套硬件不能两份服务抢 HCI）
for old in ble-midi-bridge; do
  if [ -f "/etc/systemd/system/$old.service" ]; then
    systemctl stop "$old" 2>/dev/null || true
    systemctl disable "$old" 2>/dev/null || true
    rm -f "/etc/systemd/system/$old.service"
  fi
done

# 2) snd-virmidi 开机自动载入
echo snd-virmidi > /etc/modules-load.d/pi-midi-bridge.conf
modprobe snd-virmidi 2>/dev/null || true

# 3) 运行环境（bumble 只能 pip 装）
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c 'import bumble' 2>/dev/null; then
  "$VENV/bin/pip" install --no-cache-dir -q "bumble==0.0.236" \\
     || echo "警告：bumble 安装失败，请手动执行 $VENV/bin/pip install bumble==0.0.236"
fi

# 3.5) 收紧 rtpmidid 的 mDNS 广播（只保留会话端口，别把每个 ALSA 口都广播出去）
if [ -f /etc/rtpmidid/default.ini ]; then
  if python3 /usr/lib/pi-midi-bridge/rtpmidid_patch.py; then
    systemctl restart rtpmidid 2>/dev/null || true
    sleep 2
  fi
fi

# 4) 注册并启动
systemctl daemon-reload || true
systemctl enable "$PKG.service" || true
systemctl restart "$PKG.service" || true
systemctl enable pi-midi-web.service || true
systemctl restart pi-midi-web.service || true
mkdir -p /var/log

exit 0
"""

PRERM = """#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "upgrade" ] || [ "$1" = "deconfigure" ]; then
  systemctl stop pi-midi-web 2>/dev/null || true
  systemctl disable pi-midi-web 2>/dev/null || true
  systemctl stop pi-midi-bridge 2>/dev/null || true
  systemctl disable pi-midi-bridge 2>/dev/null || true
fi
exit 0
"""

POSTRM = """#!/bin/sh
set -e
if [ "$1" = "purge" ]; then
  rm -rf /var/lib/pi-midi-bridge
  rm -f /var/log/pi-midi-bridge.log /var/log/pi-midi-web.log
  rm -f /etc/modules-load.d/pi-midi-bridge.conf
  systemctl daemon-reload 2>/dev/null || true
fi
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  systemctl unmask bluetooth 2>/dev/null || true
  systemctl start bluetooth 2>/dev/null || true
fi
exit 0
"""


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    for rel, (mode, content) in FILES.items():
        p = OUT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.chmod(p, int(mode, 8))
    deb = OUT / "DEBIAN"
    deb.mkdir(parents=True)
    (deb / "control").write_text(
        CONTROL.format(maintainer=os.environ.get("MAINTAINER", "ee <ee@rpi3>"),
                       homepage=os.environ.get("HOMEPAGE", "https://github.com"))
    )
    (deb / "conffiles").write_text(
        "/etc/default/pi-midi-bridge\n/etc/default/pi-midi-web\n")
    (deb / "postinst").write_text(POSTINST)
    (deb / "prerm").write_text(PRERM)
    (deb / "postrm").write_text(POSTRM)
    for s in ("postinst", "prerm", "postrm"):
        os.chmod(deb / s, 0o755)
    print(f"打包树已生成：{OUT}")


if __name__ == "__main__":
    main()
