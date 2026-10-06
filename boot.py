"""boot 自检流启动动画：真硬件数据 + ASCII logo + 任意键跳过。

硬件探测放在后台线程（`psutil.swap_memory()` 在这台机器上单项就要 530ms），
否则窗口构造会被它阻塞半秒多。静态行立刻开始滚动，硬件行随到随补。
"""
import os
import platform
import socket
import time

import psutil
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

from theme import BLACK, GREEN, GREEN_DIM, GREEN_FAINT, AMBER, RED, mono

ASCII_LOGO = [
    "  ______  ______  ______  __    _",
    " |      ||  __  ||  ____||  |  | |",
    " | |--| || |  | ||  |___ |  |__| |",
    " | |--| || |  | ||  ___||   __   |",
    " | |--| ||  __  ||  |___ |  |  | |",
    " |______||______||______||__|  |_|",
    "",
    "         HACK THE PLANET",
]

STATIC_LINES = [
    " BL4CK://SHELL v1.0   NEON CONSOLE",
    " [ OK ]  BIOS     : AMI x64",
    " [ OK ]  POST     : passed",
    " [ OK ]  BOOT DEV : C:\\",
]

TAIL_LINES = [
    "  Starting terminal daemon ...  [ OK ]",
    "  Mounting virtual fs  ......  [ OK ]",
]


def hardware_lines() -> list:
    """收集真实硬件信息，生成自检行。每行 (label, value, ok)。"""
    lines = []

    cpu = platform.processor() or platform.machine()
    cores = os.cpu_count() or 0
    lines.append(("CPU", f"{cpu.strip()}  [{cores} cores]", True))

    mem = psutil.virtual_memory().total
    lines.append(("MEM", f"{mem / 1024 ** 3:.1f} GB", True))

    swaps = psutil.swap_memory()          # 慢（~0.5s），所以整段放后台线程
    lines.append(("SWAP", f"{swaps.total / 1024 ** 3:.1f} GB", True))

    for part in psutil.disk_partitions(all=False)[:2]:
        try:
            usage = psutil.disk_usage(part.mountpoint)
            total = usage.total / 1024 ** 3
            lines.append(("DISK", f"{part.device} {total:.0f} GB [{part.fstype}]", True))
        except OSError:
            pass

    # 本机 IP：socket 取已路由地址，避免 spawn 子进程
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except OSError:
            ip = None
        finally:
            s.close()
        if ip:
            lines.append(("NET", f"IPv4 {ip}", True))
    except Exception:
        pass

    up = time.time() - psutil.boot_time()
    h, m, s = int(up // 3600), int(up % 3600 // 60), int(up % 60)
    lines.append(("UPTIME", f"{h:02d}:{m:02d}:{s:02d}", True))
    return lines


class _HwProbe(QThread):
    """后台硬件探测：结果通过 done 信号送回 GUI 线程。"""

    done = pyqtSignal(list)

    def run(self):
        try:
            self.done.emit(hardware_lines())
        except Exception:
            self.done.emit([])


class BootScreen(QWidget):
    """主窗口内的覆盖层：自检流逐行打印 -> ASCII logo -> finished 信号。"""

    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #020a04;")
        self.setFont(mono(13))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._rows = []              # 已打印的行
        self._pending = list(STATIC_LINES)
        self._idx = 0
        self._probe_done = False
        self._shown_logo = False
        self._emitted = False

        self._probe = _HwProbe(self)
        self._probe.done.connect(self._on_hw)
        self._probe.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(28)

    # ---------------- 硬件行异步到达 ----------------
    def _on_hw(self, lines):
        for label, value, ok in lines:
            mark = "[ OK ]" if ok else "[FAIL]"
            self._pending.append(f" {mark}  {label:<8}: {value}")
        self._pending += TAIL_LINES
        self._probe_done = True

    # ---------------- 逐行打印 ----------------
    def _tick(self):
        if self._idx < len(self._pending):
            self._rows.append(self._pending[self._idx])
            self._idx += 1
            self.update()
            return
        if not self._probe_done:
            return                    # 等硬件自检结果，先不出 logo
        if not self._shown_logo:
            self._shown_logo = True
            self.timer.stop()
            self._rows += ["", ""]
            self._rows += list(ASCII_LOGO)
            self.update()
            QTimer.singleShot(400, self._finish)

    def _finish(self):
        """只发一次 finished，避免按键+定时器重复触发。"""
        if self._emitted:
            return
        self._emitted = True
        self._stop_probe()
        self.finished.emit()

    def _stop_probe(self):
        try:
            if self._probe.isRunning():
                # 必须等它结束：QThread 还在跑时销毁 QWidget 会直接崩（qFatal abort）。
                # 探测最坏 ~2s（swap 即 650ms + UDP），10s 封顶兜底极端慢机。
                self._probe.wait(10000)
        except Exception:
            pass

    def keyPressEvent(self, e):
        self._finish()

    def mousePressEvent(self, e):
        self._finish()

    def closeEvent(self, e):
        self._stop_probe()
        super().closeEvent(e)

    # ---------------- 绘制 ----------------
    def paintEvent(self, e):
        from PyQt6.QtGui import QPainter
        p = QPainter(self)
        p.fillRect(self.rect(), BLACK)
        p.setPen(GREEN)
        fh = p.fontMetrics().height()
        x0, y0 = 24, 16

        maxfit = max(4, (self.height() // 2 - y0) // fh)
        lines_show = self._rows[:maxfit]
        for i, row in enumerate(lines_show):
            p.setPen(self._row_color(row))
            p.drawText(x0, y0 + i * fh, row)

        if self._shown_logo:
            logo_rows = [r for r in self._rows if r]
            ly = y0 + (len(lines_show) + 1) * fh
            for i, row in enumerate(logo_rows):
                p.setPen(GREEN)
                p.drawText(x0, int(ly + i * fh * 1.15), row)
            p.setPen(GREEN_DIM)
            p.drawText(x0, self.height() - 40, "  [ PRESS ANY KEY ]")
        p.end()

    def _row_color(self, row):
        if "[FAIL]" in row:
            return RED
        if "[ OK ]" in row or "HACK" in row:
            return GREEN
        if row.startswith(" "):
            return GREEN_DIM
        return GREEN_FAINT
