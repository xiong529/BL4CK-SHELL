"""左侧栏：真实数据面板（系统信息 / 文件树 / 端口 / 进程 / 流量）。

全部数据来自 psutil 真读取，QTimer 每秒刷新；文件树跟随终端 cwd。
"""
import os
import time

import psutil
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QSizePolicy, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QSplitter, QFrame,
)

from theme import (GREEN, GREEN_DIM, GREEN_DARK, AMBER, RED, BLACK, mono,
                   GREEN_FAINT, hub)

# SYSTEM INFO 条形图用的实心/空心方块（模块级，避免在 f-string 表达式里写反斜杠转义）
_BLOCK = "█"
_SHADE = "░"


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
    """SYSTEM INFO：Kali neofetch 风格真机信息。

    静态项（OS/Kernel/Host）只读一次；动态项（Uptime/Load Avg/Procs/IP 与
    CPU/MEM/SWAP/DISK 百分比 + 条形图）每秒刷新。真机数据统一来自 sysinfo.py
    （platform / psutil / 注册表，不调 wmic —— wmic 要 1-2 秒）。
    """

    TEXT_ROWS = ("OS", "Kernel", "Host", "Uptime", "Load Avg", "Procs", "IP")
    BAR_ROWS = ("CPU", "MEM", "SWAP", "DISK")
    KEY_W = 56          # 左侧键名列宽（mono(8) 下 8 字符 ≈ 48px）
    BAR_CELLS = 18      # 条形格数（18×6px = 108px，正好塞进 230px 侧栏）

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(186)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(1)
        lay.addWidget(PanelTitle("{ SYSTEM INFO }"))

        self._root = os.path.splitdrive(os.path.expanduser("~"))[0] + os.sep
        self._static = {"OS": "?", "Kernel": "?", "Host": "?"}
        try:
            import sysinfo
            self._static = {"OS": sysinfo.os_name(), "Kernel": sysinfo.kernel(),
                            "Host": sysinfo.host()}
        except Exception:
            pass

        self.keys = {}
        self.vals = {}
        self.bars = {}
        self._raw = {}
        g = QGridLayout()
        g.setContentsMargins(0, 2, 0, 0)
        g.setHorizontalSpacing(4)
        g.setVerticalSpacing(1)
        g.setColumnStretch(2, 1)

        r = 0
        for k in self.TEXT_ROWS:
            kl = QLabel(k)
            kl.setFont(mono(8))
            kl.setFixedWidth(self.KEY_W)
            v = QLabel("...")
            v.setFont(mono(8))
            # Ignored：值列随布局伸缩，绝不用文本宽度去撑宽侧栏
            v.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            g.addWidget(kl, r, 0)
            g.addWidget(v, r, 1, 1, 2)
            self.keys[k], self.vals[k] = kl, v
            r += 1
        for k in self.BAR_ROWS:
            kl = QLabel(k)
            kl.setFont(mono(8))
            kl.setFixedWidth(self.KEY_W)
            pct = QLabel("...")
            pct.setFont(mono(8))
            pct.setFixedWidth(34)
            bar = QLabel()
            bar.setFont(mono(8))
            g.addWidget(kl, r, 0)
            g.addWidget(pct, r, 1)
            g.addWidget(bar, r, 2)
            self.keys[k], self.vals[k], self.bars[k] = kl, pct, bar
            r += 1
        lay.addLayout(g)
        lay.addStretch(1)

        self.retint()
        hub.changed.connect(lambda _n: self.retint())

        self.t = QTimer(self)
        self.t.timeout.connect(self._refresh)
        self.t.start(1000)
        self._refresh()

    def retint(self):
        for lab in self.keys.values():
            lab.setStyleSheet(f"color: {GREEN_DIM.name()};")
        for lab in self.vals.values():
            lab.setStyleSheet(f"color: {GREEN.name()};")

    def set_cwd(self, path):
        """跟随终端 cwd：DISK 行显示终端所在盘。"""
        drv = os.path.splitdrive(path or "")[0]
        if drv:
            self._root = drv + os.sep

    def _refresh(self):
        try:
            cpu = psutil.cpu_percent(None)
            mem = psutil.virtual_memory()
            sw = psutil.swap_memory()
            procs = len(psutil.pids())
        except Exception:
            return
        try:
            disk = psutil.disk_usage(self._root).percent
        except OSError:
            disk = 0.0
        try:
            import sysinfo
            up = sysinfo.uptime_str()
            load = sysinfo.load_avg()
            ip = sysinfo.local_ip()
        except Exception:
            up = load = ip = "n/a"
        self._raw.update(OS=self._static["OS"], Kernel=self._static["Kernel"],
                         Host=self._static["Host"], Uptime=up,
                         **{"Load Avg": load}, Procs=str(procs), IP=ip)
        self._elide_all()
        for k, pct in (("CPU", cpu), ("MEM", mem.percent),
                       ("SWAP", sw.percent), ("DISK", disk)):
            self.vals[k].setText(f"{pct:.0f}%")
            self.bars[k].setText(self._bar(pct))

    def resizeEvent(self, e):
        """侧栏被拖动改宽时必须重算省略号，否则值会停在旧宽度上。"""
        self._elide_all()
        super().resizeEvent(e)

    def _elide_all(self):
        for k in self.TEXT_ROWS:
            self._text(k, self._raw.get(k, "..."))

    def _text(self, key, text):
        """长文本按可用宽度省略号截断（侧栏窄，OS/Host 一定会超），全量进 tooltip。"""
        lab = self.vals[key]
        text = str(text)
        self._raw[key] = text
        avail = max(60, self.width() - self.KEY_W - 24)
        lab.setText(lab.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, avail))
        lab.setToolTip(text)

    def _bar(self, pct):
        pct = max(0.0, min(100.0, float(pct)))
        n = int(pct / 100 * self.BAR_CELLS)
        color = (GREEN if pct < 70 else AMBER if pct < 90 else RED).name()
        return (f'<font color="{color}">{_BLOCK * n}</font>'
                f'<font color="{GREEN_FAINT.name()}">{_SHADE * (self.BAR_CELLS - n)}</font>')


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
        self.files.cwd_changed.connect(self.sys.set_cwd)
        self.files.file_open.connect(self.file_open.emit)
        self.sys.set_cwd(self.files.cwd)

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