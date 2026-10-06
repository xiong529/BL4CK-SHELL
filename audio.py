"""音效系统：播放 eDEX-UI 的 wav 音效（惰性加载，QSoundEffect 异步播放）。

接入点（与 eDEX-UI 源码触发点一致，见 src/classes/*.class.js）：
    audio.play("stdin")      每次按键（keyboard.class.js:316 onkeydown）
    audio.play("granted")    回车键 + 开机完成（keyboard.class.js:350 / _renderer.js:237）
    audio.play("stdout")     终端收到输出（terminal.class.js:198，30ms 节流）
    audio.play("error")      拒绝/出错场景（modal.class.js:102 error 模态打开）
    audio.play("denied")     模态框关闭 whoosh（modal.class.js:64，本终端暂无模态框）
    audio.play("folder")     文件浏览逐项渲染 / 切标签（filesystem.class.js:494）
    audio.play("theme")      标题屏/主题色显示（_renderer.js:285）
    audio.play("keyboard")   屏幕键盘弹出（_renderer.js:400，本终端无屏幕键盘，未用）
    audio.play("info")       info 模态打开（modal.class.js:108，本终端用于提示音）
    audio.play("alarm")      warning 模态打开（modal.class.js:105，未用）

sound on/off 命令控制总开关；无音频设备 / 文件缺失时静默降级。
"""
import os

from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QSoundEffect

import sfxgen

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "audio")

_ENABLED = True       # sound on/off 总开关
_DEFAULT_VOL = 0.5
_cache = {}


def set_enabled(on: bool):
    global _ENABLED
    _ENABLED = bool(on)


def is_enabled() -> bool:
    return _ENABLED


def _fx(name: str):
    fx = _cache.get(name)
    if fx is None:
        path = os.path.join(_DIR, name + ".wav")
        if not os.path.exists(path):
            # 合成型音效（boot_power/boot_beep）缺失时惰性生成
            if not sfxgen.ensure(name + ".wav"):
                return None
            path = os.path.join(_DIR, name + ".wav")
        try:
            fx = QSoundEffect()
            fx.setSource(QUrl.fromLocalFile(path))
            fx.setVolume(_DEFAULT_VOL)
            _cache[name] = fx
        except Exception:
            return None
    return fx


def play(name: str, volume: float = _DEFAULT_VOL):
    """播放一个音效。失败静默（无声卡、文件损坏、无 QApplication 都无妨）。"""
    if not _ENABLED:
        return
    fx = _fx(name)
    if fx is None:
        return
    try:
        if fx.status() == QSoundEffect.Status.Error:
            return
        fx.setVolume(max(0.0, min(1.0, volume)))
        fx.play()
    except Exception:
        pass


def shutdown():
    """停止并释放所有音效对象（应用退出前调用）。

    QSoundEffect 底层会拉起 ffmpeg 音频线程，不显式停止的话进程退出时可能
    挂起（QWaitCondition destroyed while threads are still waiting）。"""
    for name, fx in list(_cache.items()):
        try:
            fx.stop()
        except Exception:
            pass
    _cache.clear()