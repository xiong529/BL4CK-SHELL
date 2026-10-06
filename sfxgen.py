# -*- coding: utf-8 -*-
"""程序化合成开机音效（纯 stdlib，原创波形，无第三方版权音频）。

eDEX-UI 官方仓库（GitSquared/edex-ui）的全部音效就是我们 assets/audio 里
那 13 个，没有更合适的开机音可用；为给开机动画一段「够味」的声景，
这里用数学合成两个原创音效，缺失时惰性写入 assets/audio/：

  boot_power.wav   1.0s  开机上扫轰鸣（70→700Hz 加速扫频 + 谐波 + 轻电路噪声）
  boot_beep.wav    0.4s  POST 成功双音哔（660→880Hz 软方波，经典主板自检声）

MIT License —— 波形全部本模块生成，可随仓库自由分发。
"""
import math
import os
import random
import struct
import wave

SR = 44100            # 采样率
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "assets", "audio")

SYNTHS = {
    "boot_power.wav": "power",
    "boot_beep.wav": "beep",
}


def _write_wav(path: str, samples: list):
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767))))
            for s in samples))


def _power() -> list:
    """1.0s 开机轰鸣：频率 70→700Hz 加速上扫，三阶谐波 + 固定种子噪声。"""
    n = int(SR * 1.0)
    rng = random.Random(11)
    out = []
    for i in range(n):
        p = i / n                      # 0..1
        t = i / SR
        # 加速上扫（ease_in 曲线）：像电容充电/引擎点火
        f = 70.0 + 630.0 * p * p
        ph = 2.0 * math.pi * f * t
        # 音量包络：60ms 起音 + 尾部 150ms 收尾
        attack = min(1.0, p / 0.06)
        release = min(1.0, (1.0 - p) / 0.15)
        env = attack * release
        s = (math.sin(ph)
             + 0.45 * math.sin(2.01 * ph)
             + 0.22 * math.sin(3.03 * ph))
        s += (rng.random() - 0.5) * 0.05 * 4.0     # 轻电路噪声
        out.append(s * env * 0.55)
    return out


def _beep() -> list:
    """0.4s POST 成功双音哔：660Hz 140ms -> 880Hz 240ms（软方波）。"""
    out = []
    for tone, dur in ((660.0, 0.14), (880.0, 0.24)):
        n = int(SR * dur)
        for i in range(n):
            t = i / SR
            ph = 2.0 * math.pi * tone * t
            sq = 1.0 if math.sin(ph) >= 0.0 else -1.0
            s = 0.7 * sq + 0.3 * math.sin(ph)      # 软边方波，不刺耳
            out.append(s * 0.5)
    return out


def ensure(name: str) -> bool:
    """确保 assets/audio/<name> 存在；合成型缺失时惰性生成。返回是否可用。"""
    path = os.path.join(_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 44:
        return True
    kind = SYNTHS.get(name)
    if kind is None:
        return False
    try:
        os.makedirs(_DIR, exist_ok=True)
        _write_wav(path, _power() if kind == "power" else _beep())
        return os.path.exists(path)
    except Exception:
        return False
