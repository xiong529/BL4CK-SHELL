"""Monitor 页：多核 CPU 曲线 + 网络折线 + 内存条，全部 psutil 真数据。"""
import psutil
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from panels import NetTrafficPanel, PanelTitle, Val, SysInfoPanel
from theme import GREEN, GREEN_DARK, AMBER, GREEN_DIM, BLACK, mono, hub


class CpuChart(QWidget):
    """每核一条 60s 折线。"""

    def __init__(self, height=160):
        super().__init__()
        self.setMinimumHeight(height)
        self.hist = {}  # core_idx -> [..]
        self._first = True
        self.t = QTimer(self)
        self.t.timeout.connect(self._sample)
        self.t.start(1000)
        self._sample()

    def _sample(self):
        try:
            vals = psutil.cpu_percent(percpu=True, interval=None)
        except Exception:
            return
        if self._first:
            self._first = False
            return  # 第一次调用必为 0，丢弃预热
        for i, v in enumerate(vals):
            if i not in self.hist:
                self.hist[i] = []
            self.hist[i].append(v)
            if len(self.hist[i]) > 60:
                self.hist[i] = self.hist[i][-60:]
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), BLACK)
        w, h = self.width(), self.height()
        p.setPen(QColor(GREEN_DARK.name()))
        p.drawLine(0, h // 2, w, h // 2)
        ncores = len(self.hist)
        if ncores == 0:
            p.setPen(QColor(GREEN.name()))
            p.drawText(10, 20, "sampling cores ...")
            p.end()
            return
        band = h / ncores
        colors = [GREEN, AMBER, QColor(0x53, 0xE0, 0xB4),
                  QColor(0x9C, 0xFF, 0x00), GREEN_DIM, QColor(0xC8, 0xFF, 0x8C)]
        for ci, (core, data) in enumerate(sorted(self.hist.items())):
            col = colors[ci % len(colors)]
            p.setPen(col)
            if len(data) < 2:
                continue
            step = w / 60
            mid = (ci + 0.5) * band
            amp = band * 0.45
            for i in range(1, len(data)):
                x0 = (i - 1) * step
                x1 = i * step
                y0 = mid - (data[i - 1] / 100) * amp
                y1 = mid - (data[i] / 100) * amp
                p.drawLine(int(x0), int(y0), int(x1), int(y1))
            p.setPen(col)
            p.drawText(4, int(mid - band / 2 + 8), f"core{ci} {data[-1]:4.1f}%")
        p.end()


class MonitorPage(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.addWidget(PanelTitle("MONITOR — LIVE"))
        root.addWidget(QLabel("CPU per-core (last 60s, psutil)"))
        self.chart = CpuChart()
        root.addWidget(self.chart)

        row = QHBoxLayout()
        self.updown = NetTrafficPanel()
        row.addWidget(self.updown)
        root.addLayout(row)

        root.addStretch(1)