"""BL4CK://SHELL —— 类 Linux 终端，Windows 后端。

启动流程：BootScreen 作为**主窗口内部的覆盖层**播放自检流，播完自动移除、
焦点交给终端。不再另开顶层全屏窗口——避开全屏状态切换造成的闪烁、
掉焦点（之前「命令输入不了」的元凶之一）和窗口透明度动画的卡顿。
布局：左栏(系统信息/文件树/端口/进程/流量+连接) | 五标签页 | 右栏(TARGET/地图/日志/菜单)。
"""
import os
import sys
import time

import psutil
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton,
    QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from theme import (APP_STYLE, BLACK, GREEN, GREEN_DARK, GREEN_DIM, RED,
                   mono, apply_theme, hub)
import audio
from boot import BootScreen
from term import TerminalWidget
from panels import LeftPanel
from monitor import MonitorPage
from sidebar import RightSidebar
from editor import EditorPage


def _retheme_all():
    """主题切换后：重设全局 QSS + 让所有控件用当前颜色重建 setStyleSheet。"""
    app = QApplication.instance()
    if app is None:
        return
    app.setStyleSheet(APP_STYLE)
    for w in app.allWidgets():
        rt = getattr(w, "retint", None)
        if callable(rt):
            try:
                rt()
            except Exception:
                pass


class TitleBar(QWidget):
    """自绘深色标题栏：替代系统白色边框，含最小化/最大化/关闭与拖拽。"""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._drag = None
        self._btns = []
        self.setFixedHeight(30)
        self.setStyleSheet(f"background-color: {BLACK.name()};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 4, 0)
        lay.setSpacing(2)

        title = QLabel("BL4CK://SHELL — NEON CONSOLE")
        title.setStyleSheet(
            f"color: {GREEN.name()}; font-weight: bold; font-size: 12px;"
            f" background: transparent;")
        lay.addWidget(title)
        lay.addStretch(1)

        specs = [
            ("─", lambda: self._win.showMinimized(), "最小化", GREEN_DIM),
            ("□", lambda: self._win._toggle_maximize(), "最大化/还原", GREEN),
            ("✕", lambda: self._win.close(), "关闭", RED),
        ]
        for glyph, slot, tip, color in specs:
            b = QPushButton(glyph)
            b.setFixedWidth(30)
            b.setToolTip(tip)
            b.setStyleSheet(self._btn_style(color))
            b.clicked.connect(slot)
            self._btns.append(b)
            lay.addWidget(b)

        hub.changed.connect(lambda _n: self.retint())

    def _btn_style(self, color):
        return (f"QPushButton {{ color: {color.name()}; background: transparent;"
                f" border: none; font-size: 13px; padding: 0; }}"
                f"QPushButton:hover {{ background: {GREEN_DARK.name()}; "
                f"color: #ffffff; }}")

    def retint(self):
        """主题切换：重建本控件 + 三个按钮的样式表。"""
        self.setStyleSheet(f"background-color: {BLACK.name()};")
        colors = [GREEN_DIM, GREEN, RED]
        for b, c in zip(self._btns, colors):
            b.setStyleSheet(self._btn_style(c))

    def _maxed(self):
        return bool(getattr(self._win, "_maxed", False))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = (e.globalPosition().toPoint()
                          - self._win.frameGeometry().topLeft())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and not self._maxed():
            self._win.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._win._toggle_maximize()


class StatusBar(QWidget):
    """底部实时状态条：CPU / 内存 / 磁盘 / 网络上下行 / 时钟（真数据）。"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(22)
        self._base_style()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        self.lbl = QLabel("...")
        self.lbl.setStyleSheet(
            f"color: {GREEN_DIM.name()}; font-size: 10px; background: transparent;")
        lay.addWidget(self.lbl)

        self._last = None
        self._t0 = time.monotonic()
        self.t = QTimer(self)
        self.t.timeout.connect(self._refresh)
        self.t.start(1000)
        self._refresh()

    def _base_style(self):
        self.setStyleSheet(f"background-color: {BLACK.name()};"
                           f" border-top: 1px solid {GREEN_DIM.name()};")

    def retint(self):
        self._base_style()
        self.lbl.setStyleSheet(
            f"color: {GREEN_DIM.name()}; font-size: 10px; background: transparent;")

    def _refresh(self):
        try:
            cpu = psutil.cpu_percent(None)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage("C:\\").percent
            net = psutil.net_io_counters()
            now = time.monotonic()
            up = down = 0.0
            if self._last is not None:
                dt = now - self._t0
                if dt > 0:
                    up = (net.bytes_sent - self._last[0]) / dt / 1024
                    down = (net.bytes_recv - self._last[1]) / dt / 1024
            self._last = (net.bytes_sent, net.bytes_recv)
            self._t0 = now
        except Exception:
            cpu = mem = disk = up = down = 0.0
        clock = time.strftime("%H:%M:%S")
        self.lbl.setText(
            f"CPU {cpu:5.1f}%   MEM {mem:5.1f}%   DISK {disk:4.1f}%   "
            f"NET ▲{up:6.1f} ▼{down:6.1f} KB/s   {clock}"
        )


class PlaceholderPage(QWidget):
    """Scan / Payload / Explorer 暂用占位页。"""

    def __init__(self, title, desc):
        super().__init__()
        self._title, self._desc = title, desc
        self._build()
        hub.changed.connect(lambda _n: self.retint())

    def _build(self):
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        t = QLabel(self._title)
        t.setStyleSheet(f"color: {GREEN.name()}; font-size: 22px; font-weight: bold;")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = QLabel(self._desc)
        d.setStyleSheet(f"color: {GREEN_DIM.name()}; font-size: 13px;")
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        lay.addWidget(d)
        lay.addStretch(2)

    def retint(self):
        # 重建布局内的标签颜色
        lay = self.layout()
        if lay is not None:
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if isinstance(w, QLabel):
                    if i == 1:
                        w.setStyleSheet(f"color: {GREEN.name()}; "
                                        f"font-size: 22px; font-weight: bold;")
                    elif i == 2:
                        w.setStyleSheet(f"color: {GREEN_DIM.name()}; "
                                        f"font-size: 13px;")


class MainWindow(QMainWindow):
    # 三栏初始比例（按窗口宽度算，不写死像素，避免小屏/大屏错位）
    SPLIT_RATIO = (0.19, 0.59, 0.22)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BL4CK://SHELL - NEON CONSOLE")
        # 去系统白边框标题栏，改自绘深色 TitleBar
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1280, 800)
        self.setMinimumSize(1024, 680)      # 低于此尺寸三栏会互相挤压

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ------ 自定义深色标题栏 ------
        self.titlebar = TitleBar(self)
        root.addWidget(self.titlebar)

        self.left = LeftPanel()
        self.left.setMinimumWidth(230)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.term = TerminalWidget()
        self.tabs.addTab(self.term, "Term")

        # 固定编辑器标签：启动即可见，不依赖文件树点击（否则用户主目录没 .py 就找不到入口）
        self.editor_tab = EditorPage()
        self.editor_tab.new_document("untitled.py")
        # 预填示例代码：启动即可见语法高亮（用户可删除重写）
        self.editor_tab.edit.setPlainText(
            "import os\n\n"
            "def greet(name):\n"
            "    print(f'hello {name}')\n\n"
            "x = [1, 2, 3]\n"
            "print(len(x))\n")
        self.editor_tab.title_changed.connect(
            lambda t, p=self.editor_tab: self._editor_set_title(p, t))
        self.editor_tab.close_requested.connect(
            lambda p=self.editor_tab: self._editor_close(p))
        self.editor_tab.new_requested.connect(lambda: self._editor_new())
        self.tabs.addTab(self.editor_tab, "Editor")
        self.term.located.connect(self._on_located)
        self.term.open_file.connect(self._open_editor)

        self.tabs.addTab(
            PlaceholderPage("EXPLORER", "coming in v0.2 - real directory browser"),
            "Explorer",
        )
        self.tabs.addTab(
            PlaceholderPage("SCAN MODULE", "show layer: world map + link arcs"),
            "Scan",
        )
        self.tabs.addTab(
            PlaceholderPage("MODULE", "show layer: module menu + progress"),
            "Payload",
        )
        self.tabs.addTab(MonitorPage(), "Monitor")

        self.right = RightSidebar()
        self.right.setMinimumWidth(280)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.left)
        self.split.addWidget(self.tabs)
        self.split.addWidget(self.right)
        self.split.setStretchFactor(0, 19)
        self.split.setStretchFactor(1, 59)
        self.split.setStretchFactor(2, 22)
        self.split.setHandleWidth(2)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split, 1)

        # ------ 底部实时状态条（填补中间下方空白）------
        self.status = StatusBar()
        root.addWidget(self.status)

        # ------ 底部跑马灯 ------
        self.marquee = QLabel(
            "   HACK THE PLANET   //   STAY ANONYMOUS   //   BL4CK://SHELL v1.0   ")
        self.marquee.setStyleSheet(
            f"background-color: {BLACK.name()}; color: {GREEN.name()};"
            f"border-top: 1px solid {GREEN_DIM.name()}; padding: 3px;"
        )
        root.addWidget(self.marquee)
        self._mt = QTimer(self)
        self._mt.timeout.connect(self._marquee_tick)
        self._mt.start(200)

        # 终端 cd -> 文件树联动
        self.term.cwd_changed.connect(self._on_cwd)
        # 文件树点击目录 -> 终端 cwd 同步（双向联动；文件树已自己 refresh，
        # 这里只更新终端 cwd 与提示符，不 emit cwd_changed 避免循环）
        self.left.cwd_changed.connect(self._on_left_cwd)
        # 文件树点击文件 -> 编辑器打开
        self.left.files.file_open.connect(self._open_editor)

        # 主题切换联动：全局 QSS + 各控件 retint + 终端提示符重画
        hub.changed.connect(self._on_theme)

        # ------ boot 覆盖层（主窗口内的子控件，不另开窗口）------
        self.boot = BootScreen(central)
        self.boot.finished.connect(self._boot_done)
        self.boot.setGeometry(central.rect())
        self.boot.raise_()
        self.boot.show()
        self.boot.setFocus()

    # ---------- 窗口事件 ----------
    def closeEvent(self, e):
        """顶层关窗统一收尾：Qt 只给顶层窗口发 closeEvent，
        子控件里的 closeEvent（term/panels）收不到，这里集中停线程。"""
        try:
            self.left.procs.worker.stop()
        except Exception:
            pass
        self.term.shutdown_threads()
        super().closeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_split()
        if getattr(self, "boot", None) is not None:
            self.boot.raise_()
            self.boot.setFocus()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "boot", None) is not None:
            self.boot.setGeometry(self.centralWidget().rect())

    def _apply_split(self):
        w = self.width()
        if w <= 0:
            return
        total = sum(self.SPLIT_RATIO)
        self.split.setSizes([int(w * r / total) for r in self.SPLIT_RATIO])

    def _toggle_maximize(self):
        """自定义最大化/还原（无边框窗口不能用系统最大化）。"""
        if getattr(self, "_maxed", False):
            self.setGeometry(self._normal_geo)
            self._maxed = False
        else:
            self._normal_geo = self.geometry()
            scr = QApplication.primaryScreen().availableGeometry()
            self.setGeometry(scr)
            self._maxed = True

    # ---------- boot 结束 ----------
    def _boot_done(self):
        if getattr(self, "boot", None) is None:
            return
        audio.play("granted", 0.5)   # eDEX: Boot Complete 完成音
        self.boot._stop_probe()      # 双保险：探测线程未结束时析构 BootScreen 会 qFatal
        self.boot.hide()
        self.boot.deleteLater()
        self.boot = None
        self._apply_split()
        self.term.update()      # 覆盖层期间终端可能一次都没画过，强制整屏重绘
        QTimer.singleShot(0, self._grab_focus)
        QTimer.singleShot(150, self._grab_focus)

    def _grab_focus(self):
        if not self.isActiveWindow():
            self.activateWindow()
            self.raise_()
        self.term.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---------- 杂项 ----------
    def _marquee_tick(self):
        t = self.marquee.text()
        self.marquee.setText(t[1:] + t[0])

    def _on_cwd(self, path):
        self.left.files.set_cwd(path)

    def _on_left_cwd(self, path):
        """文件树里点目录：终端 cwd 跟随（MEDIUM 9 双向同步）。"""
        self.term.cwd = path
        self.term.repaint_prompt()

    def _on_theme(self, name):
        """主题切换：重刷全局 QSS、各控件 setStyleSheet、终端提示符色。"""
        _retheme_all()
        self.marquee.setStyleSheet(
            f"background-color: {BLACK.name()}; color: {GREEN.name()};"
            f"border-top: 1px solid {GREEN_DIM.name()}; padding: 3px;"
        )
        self.term.repaint_prompt()   # 终端里已画出的提示符行用新色重画

    def _on_located(self, lat, lon, city, ip):
        """whereami 命令联动：真实位置标上地图 + TARGET 面板改真实数据。"""
        try:
            isp = self.right.target.detail.text().splitlines()[-1]
        except Exception:
            isp = "?"
        self.right.target.set_real(ip, city, float(lat), float(lon), isp)
        self.right.map.set_real_location(float(lat), float(lon), city)

    # ---------- 编辑器集成 ----------
    def _open_editor(self, path):
        """在编辑器标签页打开/新建文件；已打开则切换到现有标签。"""
        ap = os.path.abspath(path)
        # 已打开：切过去
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, EditorPage) and w.path == ap:
                self.tabs.setCurrentIndex(i)
                w.edit.setFocus()
                return
        page = EditorPage()
        page.title_changed.connect(
            lambda t, p=page: self._editor_set_title(p, t))
        page.close_requested.connect(lambda p=page: self._editor_close(p))
        page.new_requested.connect(lambda: self._editor_new())
        if os.path.exists(ap) and os.path.isfile(ap):
            page.open(ap)
        else:
            # 新建：先占位路径，保存时真正落盘
            page.new_document(os.path.basename(ap))
            page.path = ap
            page._title = os.path.basename(ap)
            page._set_lang(page._lang)
        self.tabs.addTab(page, page.tab_title())
        self.tabs.setCurrentWidget(page)
        page.edit.setFocus()
        audio.play("expand", 0.4)

    def _editor_set_title(self, page, title):
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is page:
                self.tabs.setTabText(i, title)
                return

    def _editor_close(self, page):
        """关闭编辑器页：有未保存修改先询问（保存 / 放弃 / 取消）。"""
        if getattr(page, "_dirty", False):
            # 先停 800ms 自动保存：否则询问期间/选 Discard 后定时器
            # 仍会落盘，把"已放弃的修改"写回文件
            try:
                page._save_timer.stop()
            except Exception:
                pass
            btn = QMessageBox.question(
                self, "Unsaved changes",
                f"Save changes to {page.tab_title()}?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save)
            if btn == QMessageBox.StandardButton.Cancel:
                return
            if btn == QMessageBox.StandardButton.Save:
                saved = page.save()
                if saved is None:
                    # 保存失败/用户取消另存为 —— 不关标签
                    return
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is page:
                self.tabs.removeTab(i)
                return

    def _editor_new(self):
        """Ctrl+N：新建未命名编辑器页。"""
        page = EditorPage()
        page.title_changed.connect(
            lambda t, p=page: self._editor_set_title(p, t))
        page.close_requested.connect(lambda p=page: self._editor_close(p))
        page.new_requested.connect(lambda: self._editor_new())
        page.new_document("untitled.py")
        self.tabs.addTab(page, page.tab_title())
        self.tabs.setCurrentWidget(page)
        page.edit.setFocus()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BL4CK://SHELL")
    app.setStyleSheet(APP_STYLE)
    app.setFont(mono())

    win = MainWindow()
    win.show()
    win._toggle_maximize()       # 铺满可用屏幕（无边框窗口的自定义最大化）
    win.activateWindow()
    win.raise_()
    # 退出前停掉音效 ffmpeg 线程（audio.shutdown），并让终端关窗清理后台线程
    app.aboutToQuit.connect(audio.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
