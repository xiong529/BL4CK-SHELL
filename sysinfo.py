"""真机信息采集：只走快速来源（platform / psutil / Windows 注册表）。

刻意不调 wmic / systeminfo —— 那两个要 1-2 秒，会拖住首屏和每秒刷新的面板。
终端首屏 banner（term.py）与左侧 SYSTEM INFO 面板（panels.py）共用本模块，
避免两份注册表读取代码各写一遍。全部函数失败时返回可读的退化值，不抛异常。
"""
import time

import platform
import psutil
import socket

try:
    import winreg
except ImportError:          # 非 Windows：注册表相关退化为空
    winreg = None

_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_BIOS_KEY = r"HARDWARE\DESCRIPTION\System\BIOS"

# Windows 版名缩写 → Kali "OS: ... Rolling" 那样的可读串
_ED_MAP = {
    "Core": "Home", "CoreCountrySpecific": "Home (CN)",
    "CoreSingleLanguage": "Home (SL)", "Professional": "Pro",
    "ProfessionalN": "Pro N", "Enterprise": "Enterprise",
    "Education": "Education", "ProfessionalEducation": "Pro Education",
}


def _rk(path, name=""):
    """读 HKLM 注册表字符串；失败返回空串。"""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except Exception:
        return ""


def cpu_name():
    """CPU 完整型号：platform.processor() 在 Windows 只给
    'Intel64 Family 6 Model 186 Stepping 2' 这类废话，注册表给真型号。"""
    return (_rk(_CPU_KEY, "ProcessorNameString").strip()
            or platform.processor() or platform.machine() or "unknown")


def cpu_mhz():
    """CPU 标称频率（MHz），失败返回 0。"""
    try:
        return int(_rk(_CPU_KEY, "~MHz")) or 0
    except (TypeError, ValueError):
        return 0


def host():
    """主板厂商 + 机型（Kali 的 Host 行）。"""
    v = _rk(_BIOS_KEY, "SystemManufacturer").strip()
    m = _rk(_BIOS_KEY, "SystemProductName").strip()
    return f"{v} {m}".strip() or platform.machine() or "unknown"


def kernel():
    """内核/构建版本串，仿 Kali 的 '6.6.8-amd64'。"""
    return f"{platform.version()}-{platform.machine().lower()}"


def os_name():
    """OS 行：Win11 用 build ≥ 22000 判定（platform.release 在 Win11 上仍返回 10）。"""
    try:
        edition = platform.win32_edition() or ""
    except Exception:
        edition = ""
    try:
        build = int(platform.version().split(".")[-1])
    except ValueError:
        build = 0
    win = "Windows 11" if build >= 22000 else f"Windows {platform.release()}"
    return " ".join(x for x in (win, _ED_MAP.get(edition, edition),
                                platform.machine()) if x)


def package_count():
    """已安装程序数（三处 Uninstall 注册表项计数，~20ms）。"""
    if winreg is None:
        return 0
    subs = ((winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
    n = 0
    for hive, sub in subs:
        try:
            with winreg.OpenKey(hive, sub) as k:
                i = 0
                while True:
                    try:
                        winreg.EnumKey(k, i)
                        n += 1
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    return n


def load_avg():
    """1/5/15 分钟平均负载串。Windows 上 psutil 给的是模拟值（就绪队列推导），
    总比没有强，失败则退化为 n/a。"""
    try:
        a, b, c = psutil.getloadavg()
        return f"{a:.2f} {b:.2f} {c:.2f}"
    except Exception:
        return "n/a"


def uptime_str():
    """开机时长：'1d 17h 38m'。"""
    try:
        s = int(time.time() - psutil.boot_time())
    except Exception:
        s = 0
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return f"{d}d {h}h {s // 60}m"


def local_ip():
    """本机出口 IP（UDP connect 不发包，只取路由选的源地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"
