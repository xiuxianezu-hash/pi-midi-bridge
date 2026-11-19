#!/usr/bin/env python3
"""收紧 rtpmidid 的 mDNS 广播：只保留会话端口（rpi3），不再广播本机每个 ALSA 端口。

背景：rtpmidid 的 [alsa_hw_auto_export]（type=hardware）会把本机所有内核 ALSA 口
（Midi Through、VirMIDI 1-0/1-1/1-2/1-3 …）各自当成一个 RTP-MIDI 服务广播出去，
于是 macOS / Windows 的「会话和目录」里出现一堆连不上的设备。它们不是会话，
也不产生任何数据，纯噪声。

[alsa_announce] 不能删——它同时提供 Network Export（128:0）这个出网口，
删了 Pi→RTP 方向就断。所以只关 [alsa_hw_auto_export]。

用法：
    sudo python3 rtpmidid_patch.py [ini 路径]
退出码：0 = 本次改动了配置；1 = 已经是目标状态，无需改；2 = 找不到配置文件。
"""
import sys
import os
import re
import shutil
import time

MARK = "# [alsa_hw_auto_export] disabled by pi-midi-bridge：只广播会话端口（rpi3），不广播本机每个 ALSA 口"
INI = sys.argv[1] if len(sys.argv) > 1 else "/etc/rtpmidid/default.ini"


def sections(text):
    """返回 [(标题, 起始行号, 结束行号)]"""
    heads = [(i, l.strip()) for i, l in enumerate(text.splitlines())
             if re.match(r"^\[[^\]]+\]", l.strip())]
    out = []
    for idx, (i, name) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(text.splitlines())
        out.append((name, i, end))
    return out


def main() -> int:
    if not os.path.exists(INI):
        print(f"skip: {INI} 不存在（未安装 rtpmidid）")
        return 2
    text = open(INI, encoding="utf-8").read()
    if MARK in text:
        print(f"ok: {INI} 已是目标状态")
        return 1

    tgt = [s for s in sections(text) if s[0].startswith("[alsa_hw_auto_export]")]
    if not tgt:
        print(f"ok: {INI} 没有 [alsa_hw_auto_export]，无需处理")
        return 1

    name, start, end = tgt[0]
    lines = text.splitlines()
    body = lines[start + 1:end]
    # 已经在段内写过 enabled=false 之类也不再重复处理
    if any(l.strip().startswith("#") is False and "disabled" in l for l in body):
        print("ok: 该段已禁用过")
        return 1

    bak = f"{INI}.bak-announce-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(INI, bak)
    new_body = [MARK] + [
        ("# " + l if l.strip() and not l.strip().startswith("#") else l) for l in body
    ]
    lines[start + 1:end] = new_body
    open(INI, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"changed: 已禁用 [alsa_hw_auto_export]，备份 {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
