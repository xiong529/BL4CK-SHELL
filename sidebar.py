"""Right sidebar: TARGET + world map + SCAN LOG + MODULE OPTIONS.

The map switches its target every ~8s, which refreshes both the TARGET panel
and the SCAN LOG. Pressing Enter in MODULE OPTIONS writes a run trace into
the SCAN LOG. The live connection table lives in the left column
(panels.ConnectionsPanel).

NOTE: this project deliberately avoids real pentest tool names and malware signature strings, because Windows antivirus treats such a .py file as malicious and quarantines it. All menu entries are neutral invented names.
"""
import random
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QListWidget, QListWidgetItem, QPlainTextEdit, QLabel, QVBoxLayout, QWidget,
)

from theme import (GREEN, GREEN_DIM, GREEN_FAINT, GREEN_DARK, AMBER, RED,
                   BLACK, mono, hub)
from panels import PanelTitle
from worldmap import WorldMapWidget


PORT_SERVICES = [
    ("22", "ssh", "OpenSSH 8.9"), ("80", "http", "Apache httpd 2.4.52"),
    ("443", "https", "nginx 1.24"), ("445", "smb", "SMB 2.1"),
    ("3306", "mysql", "MySQL 8.0.36"), ("3389", "rdp", "Microsoft RDP 10"),
    ("8080", "http-proxy", "nginx 1.24"), ("53", "dns", "BIND 9.18"),
    ("143", "imap", "Dovecot 2.3"), ("25", "smtp", "Postfix 3.7"),
    ("23", "telnet", "telnetd"), ("1433", "mssql", "MS SQL Server 2022"),
    ("5900", "vnc", "RealVNC 5.3"), ("6379", "redis", "Redis 7.0"),
]

VULNS = ["CVE-2021-44228", "CVE-2024-3400", "CVE-2023-4863",
         "CVE-2022-26890", "CVE-2023-37350", "CVE-2021-34527"]

OS_GUESS = ["Ubuntu 22.04 LTS", "Windows Server 2022", "Debian 12",
            "CentOS Stream 9"]

SCAN_TMPL = [
    "scanning {host} for open ports ...",
    "port {port}/tcp  open   {svc}  ({ver})",
    "banner grab : {svc} @ {host}:{port}",
    "os fingerprint : {osg}",
    "service scan complete - {n} ports open",
    "checking {vuln} ...",
    "[!] {vuln} : medium risk",
    "probing {svc} default credentials ...",
    "auth ok - session established",
    "enumerating shares on {host} ...",
    "2 shares found, 1 world-readable",
    "audit finished, 1 host up",
]

LINK_STEPS = [
    "initializing module {mod} ...",
    "negotiating channel to {host}:{port} ...",
    "handshake ok - link established",
    "pulling remote telemetry ({kb} KB) from {host}",
    "stream 1 ready",
]


def fake_ip(name):
    """Stable fake public IP per city (show layer, same city -> same IP)."""
    h = 0
    for ch in name:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (f"{40 + h % 180}.{(h >> 8) % 256}."
            f"{(h >> 16) % 256}.{1 + (h >> 24) % 253}")


class TargetWidget(QWidget):
    """Current target: IP / city / lat-lon / ISP."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        lay.addWidget(PanelTitle("TARGET"))
        self.ip = QLabel("--")
        self.detail = QLabel("--")
        lay.addWidget(self.ip)
        lay.addWidget(self.detail)
        self.status = QLabel("STATUS : IDLE")
        lay.addWidget(self.status)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

    def retint(self):
        self.ip.setStyleSheet(
            f"color: {RED.name()}; font-size: 15px; font-weight: bold;")
        self.detail.setStyleSheet(f"color: {GREEN_DIM.name()}; font-size: 10px;")
        self.status.setStyleSheet(f"color: {AMBER.name()}; font-size: 10px;")

    def set_target(self, tup):
        _x, _y, name, country, isp, lon, lat = tup
        self.ip.setText(fake_ip(name))
        self.detail.setText(
            f"CITY : {name}, {country}\n"
            f"LAT  : {abs(lat):.5f} {'N' if lat >= 0 else 'S'}\n"
            f"LON  : {abs(lon):.5f} {'E' if lon >= 0 else 'W'}\n"
            f"ISP  : {isp}"
        )
        self.status.setText("STATUS : LINKED    TRACE : ACTIVE")


    def set_real(self, ip, city, lat, lon, isp):
        """whereami 联动：显示真实本机定位。"""
        self.ip.setText(ip)
        self.detail.setText(
            f"CITY : {city}\n"
            f"LAT  : {abs(lat):.5f} {'N' if lat >= 0 else 'S'}\n"
            f"LON  : {abs(lon):.5f} {'E' if lon >= 0 else 'W'}\n"
            f"ISP  : {isp}"
        )
        self.status.setText("STATUS : REAL POSITION LOCKED")


class ScanLogPanel(QWidget):
    """Auto-scrolling timestamped scan log."""

    def __init__(self, interval=900):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(PanelTitle("SCAN LOG"))

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.view.setMaximumBlockCount(260)
        self.view.setFont(mono(9))
        lay.addWidget(self.view, 1)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())

        self.host = "203.0.113.9"   # RFC 5737 TEST-NET-3 占位（虚构目标，非真实地址）
        self.city = "unknown"
        self.t = QTimer(self)
        self.t.timeout.connect(self._tick)
        self.t.start(interval)
        self.log(f"target acquired : {self.host}", GREEN)

    def retint(self):
        self.view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BLACK.name()}; color: {GREEN.name()};"
            f" border: 1px solid {GREEN_DARK.name()}; padding: 2px; }}"
        )

    def set_target(self, tup):
        """tup comes from WorldMapWidget.city_pts:
        (x, y, name, country, isp, lon, lat)"""
        _x, _y, name, country, isp, lon, lat = tup
        self.city = name
        self.host = fake_ip(name)
        self.log("", None)
        self.log(f"== new target : {name}, {country} ==", AMBER)
        self.log(f"resolving {self.host} ...", GREEN_DIM)
        self.log(f"geo : {abs(lat):.2f} {'N' if lat >= 0 else 'S'} "
                 f"{abs(lon):.2f} {'E' if lon >= 0 else 'W'}  isp={isp}", GREEN_DIM)

    def log(self, text, color=GREEN):
        if color is None:
            self.view.appendPlainText("")
        else:
            ts = time.strftime("%H:%M:%S")
            self.view.appendHtml(
                f'<span style="color:{color.name()}">[{ts}] {text}</span>')
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def run_module(self, name):
        """MODULE OPTIONS selection -> print a run trace."""
        self.log(f"run {name} --target {self.host}", AMBER)
        port = random.choice(PORT_SERVICES)[0]
        for step in LINK_STEPS:
            self.log(step.format(mod=name, host=self.host, port=port,
                                 kb=random.randint(180, 420)), GREEN_DIM)
        self.log("[+] stream 1 ready", GREEN)

    def _tick(self):
        tmpl = random.choice(SCAN_TMPL)
        port, svc, ver = random.choice(PORT_SERVICES)
        line = tmpl.format(
            host=self.host, port=port, svc=svc, ver=ver,
            osg=random.choice(OS_GUESS), vuln=random.choice(VULNS),
            n=random.randint(2, 6),
        )
        color = AMBER if line.startswith("[!]") else GREEN_DIM
        self.log(line, color)


class _MenuList(QListWidget):
    chosen = pyqtSignal(str)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            it = self.currentItem()
            if it is not None:
                self.chosen.emit(it.data(Qt.ItemDataRole.UserRole))
            return
        super().keyPressEvent(e)


class ModuleMenuPanel(QWidget):
    """Numbered menu: arrow keys to move, Enter to run. Neutral item names."""

    module_run = pyqtSignal(str)

    ITEMS = [
        ("1", "net-probe", "network sweep"),
        ("2", "port-audit", "port scanning"),
        ("3", "svc-audit", "service audit"),
        ("4", "cfg-review", "config review"),
        ("5", "log-fetch", "log collection"),
        ("6", "geo-trace", "route tracing"),
        ("7", "tty-link", "remote console"),
        ("8", "pkg-build", "artifact build"),
        ("9", "cert-check", "certificate check"),
        ("0", "exit", "return to main menu"),
    ]

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(PanelTitle("MODULE OPTIONS"))

        self.list = _MenuList()
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setFont(mono(9))
        for num, name, desc in self.ITEMS:
            it = QListWidgetItem(f"{num}. {name:<11} - {desc}")
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        self.list.setFixedHeight(len(self.ITEMS) * 15 + 6)
        lay.addWidget(self.list)
        self.retint()
        hub.changed.connect(lambda _n: self.retint())
        self.list.chosen.connect(self.module_run)

    def retint(self):
        self.list.setStyleSheet(
            f"QListWidget {{ background: {BLACK.name()}; color: {GREEN.name()};"
            f" border: 1px solid {GREEN_DARK.name()}; outline: none; }}"
            f"QListWidget::item {{ padding: 1px 4px; }}"
            f"QListWidget::item:selected {{ background: {GREEN_DARK.name()};"
            f" color: #ddffe4; }}"
        )


class RightSidebar(QWidget):
    """Whole right column. Map target change refreshes TARGET and SCAN LOG."""

    def __init__(self):
        super().__init__()
        self.setMinimumWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        self.target = TargetWidget()
        self.map = WorldMapWidget(height=250)
        self.scan = ScanLogPanel()
        self.menu = ModuleMenuPanel()

        lay.addWidget(self.target)
        lay.addWidget(self.map)
        lay.addWidget(self.scan, 1)
        lay.addWidget(self.menu)

        self.map.target_changed.connect(self.target.set_target)
        self.map.target_changed.connect(self.scan.set_target)
        self.menu.module_run.connect(self.scan.run_module)

        tup = self.map.target()
        self.target.set_target(tup)
        self.scan.set_target(tup)
