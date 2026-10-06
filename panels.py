"""左侧栏：真实数据面板（系统信息 / 文件树 / 端口 / 进程 / 流量）。

全部数据来自 psutil 真读取，QTimer 每秒刷新；文件树跟随终端 cwd。
"""
import os
import time

import psutil
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QSplitter, QFrame,
)

from theme import (GREEN, GREEN_DIM, GREEN_DARK, AMBER, RED, BLACK, mono,
                   GREEN_FAINT, hub)


class PanelTitle(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

    def retint(self):
        self.setStyleSheet(
            f"color: {GREEN.name()}; font-weight: bold;"
            f" border-bottom: 1px solid {GREEN_DARK.name()}; padding: 2px;"
        )


class Val(QLabel):
    """会跳动的大数字。"""

    def __init__(self, w=64, h=26):
        super().__init__("...")
        self.setFixedSize(w, h)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

    def retint(self):
        self.setStyleSheet(f"color: {GREEN.name()}; font-size: 15px; font-weight: bold;")


class SysInfoPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.addWidget(PanelTitle("SYSTEM INFO"))

        self.cpu = Val()
        self.mem = Val(96, 26)
        self.up = Val(96, 26)

        self.cpu_bar = QLabel()
        self.mem_bar = QLabel()
        self.cpu_bar.setFixedHeight(10)
        self.mem_bar.setFixedHeight(10)

        g = QGridLayout()
        g.addWidget(QLabel("CPU"), 0, 0)
        g.addWidget(self.cpu, 0, 1)
        g.addWidget(self.cpu_bar, 1, 0, 1, 2)
        g.addWidget(QLabel("MEM"), 2, 0)
        g.addWidget(self.mem, 2, 1)
        g.addWidget(self.mem_bar, 3, 0, 1, 2)
        g.addWidget(QLabel("UP"), 4, 0)
        g.addWidget(self.up, 4, 1)
        lay.addLayout(g)

        self.t = QTimer(self)
        self.t.timeout.connect(self._refresh)
        self.t.start(1000)
        self._refresh()

    def _refresh(self):
        try:
            cpu = psutil.cpu_percent(None)
            mem = psutil.virtual_memory()
        except Exception:
            return
        self.cpu.setText(f"{cpu:5.1f}%")
        self.cpu_bar.setText(self._bar(cpu))
        self.mem.setText(f"{mem.percent:4.1f}%")
        self.mem_bar.setText(self._bar(mem.percent))
        try:
            secs = int(time.time() - psutil.boot_time())
        except Exception:
            secs = 0
        self.up.setText(self._hm(secs))

    def _hm(self, s):
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        return f"{d}d {h:02d}h {s // 60:02d}m"

    def _bar(self, pct):
        n = int(pct / 100 * 24)
        filled = "\u2588" * n
        empty = "\u2591" * (24 - n)
        color = GREEN.name()
        return f'<font color="{color}">{filled}</font><font color="#0a2a12">{empty}</font>'


class FileTreePanel(QWidget):
    """跟随终端 cwd 的目录树（目录 + 文件，点击目录进入，文件打开编辑器）。"""

    cwd_changed = pyqtSignal(str)
    file_open = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.cwd = os.path.expanduser("~")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.addWidget(PanelTitle("FILES"))
        self.path = QLabel(self.cwd)
        lay.addWidget(self.path)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.itemClicked.connect(self._on_clicked)
        lay.addWidget(self.tree)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())
        self.refresh()

    def retint(self):
        self.path.setStyleSheet(f"color: {AMBER.name()}; font-size: 9px;")

    def set_cwd(self, path: str):
        self.cwd = path
        self.refresh()

    @staticmethod
    def _fmt_size(b):
        if b >= 1024 ** 3:
            return f"{b / 1024 ** 3:.2f}G"
        if b >= 1024 ** 2:
            return f"{b / 1024 ** 2:.1f}M"
        if b >= 1024:
            return f"{b / 1024:.0f}K"
        return f"{b}B"

    def refresh(self):
        self.path.setText(self.cwd)
        self.tree.clear()
        drives = [self.cwd[:3]] if os.path.isabs(self.cwd) else []
        if not drives:
            drives = [os.path.splitdrive(p)[0] or "C:\\" for p in ("C:\\", "D:\\")]
        root = QTreeWidgetItem([self.cwd])
        root.setForeground(0, QColor(GREEN.name()))
        self.tree.addTopLevelItem(root)
        self._fill(root, self.cwd, depth=0)
        root.setExpanded(True)

    def _fill(self, item, path, depth):
        if depth > 4:
            return
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return
        for n in names[:60]:
            full = os.path.join(path, n)
            try:
                is_dir = os.path.isdir(full)
                size = 0 if is_dir else os.path.getsize(full)
            except OSError:
                continue
            label = n if is_dir else f"{n}  ({self._fmt_size(size)})"
            child = QTreeWidgetItem([label])
            child.setData(0, Qt.ItemDataRole.UserRole, full)
            if is_dir:
                child.setForeground(0, QColor(GREEN_DIM.name()))
            else:
                child.setForeground(0, QColor(GREEN_FAINT.name()))
            item.addChild(child)
            if is_dir and depth < 2:
                self._fill(child, full, depth + 1)

    def _on_clicked(self, item, col):
        full = item.data(0, Qt.ItemDataRole.UserRole)
        if not full:
            return
        if os.path.isdir(full):
            self.cwd = full
            self.cwd_changed.emit(full)
            self.refresh()
        elif os.path.isfile(full):
            self.file_open.emit(full)


class PortRow(QLabel):
    pass


class PortsPanel(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.addWidget(PanelTitle("PORTS (listen)"))
        self.content = QLabel()
        self.content.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.content)
        self.t = QTimer(self)
        self.t.timeout.connect(self._refresh)
        self.t.start(2000)
        self._refresh()

    def _refresh(self):
        try:
            conns = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, PermissionError):
            conns = []
        ports = sorted({c.laddr.port for c in conns if c.laddr and c.status == "LISTEN"})
        line = "  ".join(
            f'<font color="{GREEN.name()}">{pp}</font>' for pp in ports[:24]
        )
        self.content.setText(f"<code>{line or '— none —'}</code>")
        self.content.adjustSize()


class _ProcScan(QThread):
    """后台进程扫描线程（分片扫描）。

    实测本机 psutil 全量枚举（约 288 进程）：只取 pid+name 约 1.0s，
    带 memory_info / cpu_times 约 3.0s。两个问题：
      1) 放 GUI 线程里会周期性冻结界面——「卡卡的」的根因；
      2) 即使放进后台线程，它仍是纯 Python 循环持有 GIL，一口气跑 3s
         会把 GUI 线程饿出 150~400ms 的停顿（实测数据）。
    所以这里做「分片」：每轮只扫 SLICE 个进程，让出后间隔再扫下一片。
    突发量有上界，槽位之间留出空闲给 GUI，全表在一轮分片内逐步刷新。
    """

    scanned = pyqtSignal(list)   # [(pid, name, rss_bytes, cpu_seconds)]

    SLICE = 48

    def __init__(self, interval_ms=1200, parent=None):
        super().__init__(parent)
        self._interval = interval_ms
        self._live = True
        self._cursor = 0

    def stop(self):
        self._live = False
        self.wait(2500)

    def run(self):
        while self._live:
            try:
                pids = psutil.pids()
            except Exception:
                pids = []
            if not pids:
                self.msleep(2000)
                continue
            if self._cursor >= len(pids):
                self._cursor = 0
            chunk = pids[self._cursor:self._cursor + self.SLICE]
            self._cursor += self.SLICE

            rows = []
            for n, pid in enumerate(chunk):
                if not self._live:
                    break
                if n % 4 == 0:
                    self.msleep(1)          # 让出 GIL
                try:
                    pr = psutil.Process(pid)
                    with pr.oneshot():
                        name = pr.name()
                        rss = pr.memory_info().rss
                        ct = pr.cpu_times()
                    rows.append((pid, name or "?", rss, ct.user + ct.system))
                except Exception:
                    continue
            if rows and self._live:
                self.scanned.emit(rows)

            waited = 0
            while self._live and waited < self._interval:
                self.msleep(100)
                waited += 100


class ProcPanel(QWidget):
    """ACTIVE PROCESSES 榜单。扫描在后台线程，界面永不阻塞。"""

    def __init__(self, max_rows=8):
        super().__init__()
        self.max_rows = max_rows
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.addWidget(PanelTitle("ACTIVE PROCESSES"))
        self.content = QLabel()
        self.content.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.content)
        self.content.setText("<code><font color='%s'>scanning ...</font></code>" % GREEN_FAINT.name())

        self._seen = {}          # pid -> (cpu_seconds, ts)   用于算 CPU% 增量
        self._live_rows = {}     # pid -> (pc, name, rss, ts) 分片累积的最近快照
        self.worker = _ProcScan(1200)
        self.worker.scanned.connect(self._on_scanned)
        # 延迟启动：别和窗口构造/首屏抢资源
        QTimer.singleShot(1500, self._start_worker)

    def _start_worker(self):
        if not self.worker.isRunning():
            # LowPriority：进程枚举很吃 CPU，别和 GUI 线程抢调度
            self.worker.start(QThread.Priority.LowPriority)

    def _on_scanned(self, rows):
        """每片到达就合并进累积表并重绘；全表在一轮分片内逐步刷新。"""
        now = time.monotonic()
        for pid, name, rss, cpu in rows:
            prev = self._seen.get(pid)
            pc = self._live_rows.get(pid, (0.0,))[0]
            if prev is not None:
                dt = now - prev[1]
                if dt > 0.05:
                    pc = max(0.0, (cpu - prev[0]) / dt * 100.0)
            self._seen[pid] = (cpu, now)
            self._live_rows[pid] = (pc, name, rss, now)

        # 清掉一轮以上没再露面的进程
        cutoff = now - 90.0
        for pid in [p for p, v in self._live_rows.items() if v[3] < cutoff]:
            self._live_rows.pop(pid, None)
            self._seen.pop(pid, None)

        if not self._live_rows:
            return
        ranked = sorted(self._live_rows.items(),
                        key=lambda kv: (kv[1][0], kv[1][2]), reverse=True)
        out = []
        for pid, (pc, name, rss, _ts) in ranked[: self.max_rows]:
            out.append(
                f'<font color="{GREEN.name()}">{pc:5.1f}%</font> '
                f'<font color="{GREEN_FAINT.name()}">{pid:>6}</font> '
                f'{name[:15]:<15} '
                f'<font color="{AMBER.name()}">{rss / 1048576:6.0f}M</font>'
            )
        self.content.setText("<code>" + "<br>".join(out) + "</code>")
        self.content.adjustSize()

    def closeEvent(self, e):
        try:
            self.worker.stop()
        except Exception:
            pass
        super().closeEvent(e)


class NetTrafficPanel(QWidget):
    """上/下行速率折线（真数据差值）。"""

    W, H = 260, 70

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.W, self.H)
        self.hist = []  # [(up_kbps, down_kbps)]
        self._last = None
        self._t0 = time.monotonic()
        self.t = QTimer(self)
        self.t.timeout.connect(self._sample)
        self.t.start(2000)   # 2s：psutil.net_io_counters 在 Windows 上 ~30ms/次，1s 刷新太贵
        self._sample()

    def _sample(self):
        try:
            io = psutil.net_io_counters()
        except Exception:
            return
        now = time.monotonic()
        if self._last:
            dt = now - self._t0
            if dt > 0:
                up = (io.bytes_sent - self._last[0]) / dt / 1024
                down = (io.bytes_recv - self._last[1]) / dt / 1024
                self.hist.append((up, down))
                if len(self.hist) > 60:
                    self.hist = self.hist[-60:]
        self._last = (io.bytes_sent, io.bytes_recv)
        self._t0 = now
        self.update()

    # QPainter 绘制见 paintEvent
    def paintEvent(self, e):
        from PyQt6.QtGui import QPainter
        p = QPainter(self)
        p.fillRect(self.rect(), BLACK)
        w, h = self.width(), self.height()
        maxv = max((max(a, b) for a, b in self.hist), default=1.0)
        maxv = max(maxv, 16.0)
        # 基线
        p.setPen(QColor(GREEN_DARK.name()))
        p.drawLine(0, h - 1, w, h - 1)
        if len(self.hist) < 2:
            p.setPen(QColor(GREEN.name()))
            p.drawText(4, 14, "↑/↓ KB/s  ...")
            p.end()
            return
        n = len(self.hist)
        step = w / max(1, n - 1)
        # 上行
        p.setPen(QColor(GREEN.name()))
        for i in range(1, n):
            x0 = (i - 1) * step
            x1 = i * step
            y0 = h - 2 - self.hist[i - 1][0] / maxv * (h - 4)
            y1 = h - 2 - self.hist[i][0] / maxv * (h - 4)
            p.drawLine(int(x0), int(y0), int(x1), int(y1))
        # 下行（Amber）
        p.setPen(QColor(AMBER.name()))
        for i in range(1, n):
            x0 = (i - 1) * step
            x1 = i * step
            y0 = h - 2 - self.hist[i - 1][1] / maxv * (h - 4)
            y1 = h - 2 - self.hist[i][1] / maxv * (h - 4)
            p.drawLine(int(x0), int(y0), int(x1), int(y1))
        up, down = self.hist[-1]
        p.setPen(QColor(GREEN.name()))
        p.drawText(4, 12, f"▲ {up:5.1f}  {GREEN.name()}")
        p.setPen(QColor(AMBER.name()))
        p.drawText(4, 24, f"▼ {down:5.1f}  KB/s")
        p.end()


class LeftPanel(QWidget):
    """整个左侧栏组合。"""

    cwd_changed = pyqtSignal(str)
    file_open = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.sys = SysInfoPanel()
        self.files = FileTreePanel()
        self.ports = PortsPanel()
        self.procs = ProcPanel()
        self.net = NetTrafficPanel()
        self.conns = ConnectionsPanel()

        self.box = QFrame()
        inner = QVBoxLayout(self.box)
        inner.setContentsMargins(6, 4, 6, 4)
        inner.addWidget(PanelTitle("NETWORK TRAFFIC"))
        inner.addWidget(self.net)
        inner.addWidget(PanelTitle("CONNECTIONS"))
        inner.addWidget(self.conns, 1)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.sys)
        split.addWidget(self.files)
        split.addWidget(self.ports)
        split.addWidget(self.procs)
        split.addWidget(self.box)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(3, 2)
        split.setStretchFactor(2, 1)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(2)
        lay.addWidget(split)

        self.files.cwd_changed.connect(self.cwd_changed.emit)
        self.files.file_open.connect(self.file_open.emit)

    def retint(self):
        self.box.setStyleSheet(f"background-color: {BLACK.name()};")


class ConnectionsPanel(QWidget):
    """真实 TCP 连接表（psutil.net_connections，ESTABLISHED 去重）。"""

    def __init__(self, rows=7):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["STATE", "LOCAL", "REMOTE"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFont(mono(8))
        self._max = rows
        lay.addWidget(self.table, 1)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

        self.t = QTimer(self)
        self.t.timeout.connect(self._refresh)
        self.t.start(2000)
        self._refresh()

    def retint(self):
        self.table.setStyleSheet(
            f"QTableWidget {{ background: {BLACK.name()}; color: {GREEN.name()};"
            f" border: 1px solid {GREEN_DARK.name()}; font-size: 8px; }}"
            f"QHeaderView::section {{ background: {GREEN_DARK.name()}; color: {BLACK.name()};"
            f" padding: 1px; border: none; font-weight: bold; }}"
        )

    def _refresh(self):
        try:
            conns = [c for c in psutil.net_connections(kind="tcp")
                     if c.status == "ESTABLISHED" and c.raddr]
        except Exception:
            conns = []
        seen = set()
        rows = []
        for c in sorted(conns, key=lambda c: str(c.raddr)):
            key = (str(c.laddr), str(c.raddr))
            if key in seen:
                continue
            seen.add(key)
            rows.append((c.status, f"{c.laddr[0]}:{c.laddr[1]}",
                         f"{c.raddr[0]}:{c.raddr[1]}"))
        rows = rows[: self._max]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, v in enumerate(r):
                it = self.table.item(i, j)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(i, j, it)
                it.setText(str(v))