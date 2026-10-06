"""boot 自检流启动动画（v3 疾速版）。

v2 的打字机逐行要 ~5.6s，v3 整体压到 ~2.3s、硬上限 3s，走 GSAP 式时间轴：
- 各阶段用缓动驱动（expo.out 冲刺 / back.out 过冲 / inOut 平滑），交错推进
- 大 logo 字母逐个「爆闪」进场：辉光层 + 从下方弹起（back.out）+ 偶发乱码干扰帧
- 自检行改为「解码式」：整行乱码快速滚回正确文本（stagger 交错），实时真数据照常
- 背景加极淡的数据流下坠列，扫描线改为一次干净的下扫脉冲
- 进度条收尾 expo 冲刺，SYSTEM READY 高频闪烁后发 finished

硬件探测仍在后台线程（psutil，实测 ~650ms），窗口构造不被阻塞；
任意键/鼠标点击可随时跳过。全屏只有绿系配色（与终端主题一致）。
"""
import os
import platform
import random
import socket
import time

import psutil
from PyQt6.QtCore import Qt, QThread, QPoint, QPointF, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QPainter, QPen, QPixmap,
                         QRadialGradient, QLinearGradient, QRegion)
from PyQt6.QtWidgets import QWidget

import audio
from theme import mono
from worldmap import WorldMapWidget

# ---------------- 文案 ----------------
LOGO_TEXT = "BL4CK://SHELL"
READY_TEXT = "SYSTEM READY"

STATIC_LINES = [
    (" [ OK ]  BIOS     : AMI x64", True),
    (" [ OK ]  POST     : passed", True),
    (" [ OK ]  BOOT DEV : C:\\", True),
]
TAIL_LINES = [
    ("  Starting terminal daemon ...  [ OK ]", True),
    ("  Mounting virtual fs  ......  [ OK ]", True),
]

# ---------------- 时间轴（秒）----------------
FRAME_MS = 28
T_TITLE = 0.08           # 顶部小标题淡入起点
T_LOGO = 0.20            # logo 字母开始进场
LOGO_STAGGER = 0.032     # 每字母间隔
LOGO_POP = 0.10          # 单字母进场时长
T_SWEEP = 0.55           # 扫描线下扫脉冲
STATIC_START = 0.62      # 静态自检行开始解码
STATIC_STAGGER = 0.12
HW_START = 1.05          # 硬件行 burst 起点（随探测到达时间浮动）
HW_STAGGER = 0.055
T_PROG = 1.55            # 进度条 expo 冲刺起点
T_READY_MIN = 1.72       # 最早进入 READY 闪烁
READY_BLINK = 0.50       # READY 闪烁时长
HARD_CAP = 3.0           # 硬上限：无论如何都 finished

GLITCH_CHARS = "01#@$%&*+-=!?<>/\\[]{}|~"
CRACK_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
               "`~!@#$%^&*()-_=+[]{};:'\",.<>/?\\|")


# ---------------- 缓动（GSAP ease 移植）----------------
def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def ease_out_expo(t: float) -> float:
    """expo.out：开头极快、末尾急停，最有「即时感」。"""
    t = clamp01(t)
    return 1.0 if t >= 1.0 else 1.0 - 2.0 ** (-10.0 * t)


def ease_out_back(t: float, s: float = 1.70158) -> float:
    """back.out：超过目标再回弹，logo 字母入场用。"""
    t = clamp01(t) - 1.0
    return t * t * ((s + 1.0) * t + s) + 1.0


def ease_in_out(t: float) -> float:
    """smoothstep：淡入淡出用。"""
    t = clamp01(t)
    return t * t * (3.0 - 2.0 * t)


def _crackle(text: str, p: float, rng: random.Random):
    """p ∈ [0,1)：返回 (正确前缀, 乱码后缀)，解码式自检行。"""
    n = int(len(text) * p)
    head = text[:n]
    tail = "".join(rng.choice(CRACK_CHARS) for _ in range(len(text) - n))
    return head, tail


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
    """主窗口内的覆盖层：解码自检流 + logo 爆闪 + 数据流，finished 收尾。"""

    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: #000000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._t = 0               # 帧计数（28ms/帧）
        self._probe_done = False
        self._emitted = False
        self._ready_at = -1.0     # READY 阶段起始秒（-1=未进入）
        self._rng = random.Random(7)   # 固定种子：数据流/乱码可复现

        # 音效触发（幂等标记）
        self._played = set()          # 一次性事件（expand/scan/ready）
        self._line_ticked = set()     # 每行解码开始已滴答
        self._logo_clicked = 0        # 已咔哒的 logo 字母数
        self._seq = 0                 # 行 id 计数器

        # 故障闪帧（CRT 神秘感）
        self._glitch_frames = 0
        self._glitch_y = 0
        self._crt_brush_obj = None    # 延迟构建的扫描线纹理画笔

        # 背景 3D 地球（复用 worldmap.WorldMapWidget：城市光点 + 攻击弧线）
        self._globe = WorldMapWidget(height=240, parent=None)
        self._globe.t.stop()          # 停掉自带 66ms 定时器，改由 boot 帧驱动
        self._globe.setMinimumHeight(0)          # 解锁构造时的 setFixedHeight(240)
        self._globe.setMaximumHeight(16777215)   # 全屏背景用
        self._globe._bg_fill = QColor(0, 0, 0, 0)    # 球外透明，露出下层代码雨
        self._globe._sphere_fill = QColor(0x00, 0x00, 0x00)  # 球内不透明，雨被遮挡
        self._globe_pm = None         # 渲染缓冲（QPixmap）
        self._globe_rect = (0, 0, 0, 0)
        self._globe_n = 0             # 地球渲染节拍计数

        # 最底层 0/1 代码雨（Matrix 风格，固定种子；随窗口尺寸延迟初始化）
        self._matrix = None           # 每列 {x, speed, head}; 见 _matrix_step

        # 背景数据流列（确定性伪随机：速度/起点/尾长）
        self._stream = [
            {"speed": 0.10 + ((i * 37) % 11) / 60.0,
             "seed": ((i * 53) % 100) / 97.0,             "len": 4 + (i * 7) % 4}
            for i in range(12)
        ]

        # 自检行：文本/成功/进场秒/解码时长
        self._lines = [
            dict(text=txt, ok=ok, start=STATIC_START + i * STATIC_STAGGER,
                 dur=0.16, id=self._seq + i)
            for i, (txt, ok) in enumerate(STATIC_LINES)
        ]
        self._seq += len(STATIC_LINES)

        self._probe = _HwProbe(self)
        self._probe.done.connect(self._on_hw)
        self._probe.start()

        self._cache = {}          # (w,h) -> 字体/画笔缓存（避免每帧重建）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(FRAME_MS)

    # ---------------- 硬件行异步到达 ----------------
    def _on_hw(self, lines):
        base = HW_START
        for label, value, ok in lines:
            mark = "[ OK ]" if ok else "[FAIL]"
            self._lines.append(dict(text=f" {mark}  {label:<8}: {value}",
                                    ok=ok, start=base, dur=0.10,
                                    id=self._seq))
            self._seq += 1
            base += HW_STAGGER
        for txt, ok in TAIL_LINES:
            self._lines.append(dict(text=txt, ok=ok, start=base + 0.06,
                                    dur=0.10, id=self._seq))
            self._seq += 1
            base += 0.09
        self._probe_done = True

    # ---------------- 动画推进 ----------------
    def _tick(self):
        self._t += 1
        t = self._t * FRAME_MS / 1000.0
        # READY 判定：文本全部解码完且硬件就绪且过了最短时间 → 闪 READY_BLINK 秒后 finished
        if (self._probe_done and t >= T_READY_MIN
                and all(ln["start"] + ln["dur"] <= t for ln in self._lines)):
            if self._ready_at < 0:
                self._ready_at = t
        if self._ready_at >= 0 and t - self._ready_at >= READY_BLINK:
            self._finish()
            return
        if t >= HARD_CAP:          # 硬上限兜底（极端慢机）
            self._finish()
            return
        self._sound_tick(t)
        self._globe_step(t)
        self._matrix_step()
        self.update()

    # ---------------- 背景地球（右置，随 boot 帧驱动）----------------
    def _globe_step(self, t):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        rect = (0, 0, w, h)                  # 全屏大地球（球面透明，雨从底下透出）
        if rect != self._globe_rect:
            self._globe_rect = rect
            self._globe.resize(w, h)
        self._globe_n += 1
        if self._globe_n % 2 == 0:          # 地球 ~18fps，省一半渲染
            self._globe._tick()
            pm = QPixmap(rect[2], rect[3])
            pm.fill(Qt.GlobalColor.transparent)   # 先清透明，避免未初始化灰底
            # 只渲染子层绘制（paintEvent），跳过默认窗口背景灰底
            self._globe.render(pm, QPoint(0, 0), QRegion(),
                               QWidget.RenderFlag.DrawChildren)
            self._globe_pm = pm

    def _matrix_step(self):
        """0/1 代码雨：每列一个亮头 + 渐暗尾串，确定性随机。"""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        col_w, row_h = 8, 14                    # 更密：1280 宽 ≈ 160 列
        rows = h // row_h + 6
        if self._matrix is None or len(self._matrix) != w // col_w:
            rng = random.Random(5)                      # 固定种子
            self._matrix = []
            for x in range(0, w, col_w):
                self._matrix.append({
                    "x": x,
                    "speed": 0.12 + rng.random() * 0.30,   # 0.12-0.42 字符/帧
                    "head": rng.random() * rows,           # 开场即有雨
                })
            self._matrix_rng = rng
            return
        for col in self._matrix:
            col["head"] += col["speed"]
            if col["head"] > rows:
                col["head"] = -random.uniform(2, 9)       # 空档后从顶重来

    def _draw_matrix(self, p, c, t):
        """0/1 代码雨：全屏背景层，亮头 + 5 层渐暗尾，整体淡入（颜色偏淡）。"""
        if self._matrix is None:
            return
        fade = int(255 * min(1.0, t / 0.70))       # 淡入
        p.setFont(c["f_rain"])
        row_h = 14
        base_a = 112 * fade // 255                 # 亮头更淡
        trail_a = (50, 34, 22, 13, 7)              # head-1..5 依次更暗
        rng = self._matrix_rng
        for col in self._matrix:
            hy = col["head"] * row_h
            p.setPen(QColor(0, 205, 50, base_a))
            p.drawText(QPointF(col["x"], hy), "1" if rng.random() < 0.5 else "0")
            y = hy - row_h
            for a in trail_a:
                if y < -14:
                    break
                p.setPen(QColor(0, 130, 36, a * fade // 255))
                p.drawText(QPointF(col["x"], y), "1" if rng.random() < 0.5 else "0")
                y -= row_h

    # ---------------- 音效（按时间轴触发，幂等）----------------
    def _sound_tick(self, t):
        """各阶段触发对应音效；尊重全局 sound on/off 开关，失败静默。"""
        if not audio.is_enabled():
            return
        try:
            # 开场：开机上扫轰鸣（程序化合成，70→700Hz）
            if t >= 0.0 and "power" not in self._played:
                self._played.add("power")
                audio.play("boot_power", 0.45)
            # logo 字母：键盘咔哒（stagger 节奏）
            n_started = (0 if t < T_LOGO else
                         min(len(LOGO_TEXT),
                             int((t - T_LOGO) / LOGO_STAGGER) + 1))
            if n_started > self._logo_clicked:
                for _ in range(min(2, n_started - self._logo_clicked)):
                    audio.play("stdin", 0.20)
                self._logo_clicked = n_started
            # 扫描线：一次 whoosh
            if t >= T_SWEEP and "sweep" not in self._played:
                self._played.add("sweep")
                audio.play("scan", 0.30)
            # 每行解码开始：短促滴答
            for ln in self._lines:
                if ln["id"] not in self._line_ticked and t >= ln["start"]:
                    self._line_ticked.add(ln["id"])
                    audio.play("stdin", 0.22)
            # READY 前一刻：POST 成功哔（660→880Hz）
            if self._ready_at >= 0 and "beep" not in self._played:
                self._played.add("beep")
                audio.play("boot_beep", 0.40)
        except Exception:
            pass

    # ---------------- 收尾 ----------------
    def _finish(self):
        if self._emitted:
            return
        self._emitted = True
        self._stop_probe()
        self.finished.emit()

    def _stop_probe(self):
        try:
            if self._probe.isRunning():
                # 必须等它结束：QThread 还在跑时销毁 QWidget 会直接崩（qFatal abort）
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
        p = QPainter(self)
        w, h = self.width(), self.height()
        t = self._t * FRAME_MS / 1000.0
        key = (w, h)
        if key not in self._cache:
            self._build_cache(w, h)
        c = self._cache[key]

        p.fillRect(self.rect(), QColor(0x00, 0x00, 0x00))   # 纯黑背景
        self._draw_matrix(p, c, t)       # 最底层：0/1 代码雨
        self._draw_globe(p, c)           # 中层：居中大地球（盖住中央的雨）
        self._draw_glow(p, c, t)
        self._draw_stream(p, c, t)
        self._draw_grid(p, c)
        self._draw_corners(p, c)
        self._draw_title(p, c, t)
        self._draw_logo(p, c, t)
        self._draw_sweep(p, c, t)
        self._draw_lines(p, c, t)
        self._draw_glitch(p, c)
        self._draw_crt(p)
        self._draw_progress(p, c, t)
        self._draw_ready(p, c, t)
        p.end()

    def _build_cache(self, w, h):
        c = {"w": w, "h": h, "cx": w / 2.0}
        fs = max(16, min(int(h * 0.16), 44))
        c["f_logo"] = mono(fs)
        c["f_title"] = mono(10)
        c["f_line"] = mono(11)
        c["f_prog"] = mono(9)
        c["f_ready"] = mono(20)
        c["f_rain"] = mono(10)   # 0/1 代码雨字符
        c["grid_pen"] = QPen(QColor(0, 85, 23, 150), 1)
        c["sweep_pen"] = QPen(QColor(0, 255, 65, 200), 2)
        self._cache[(w, h)] = c

    def _draw_glow(self, p, c, t):
        w, h, cx = c["w"], c["h"], c["cx"]
        a = int(34 * ease_in_out(clamp01(t / 0.30)))   # 中央绿光晕淡入
        grad = QRadialGradient(cx, h * 0.36, max(w, h) * 0.55)
        grad.setColorAt(0.0, QColor(0, 255, 65, a))
        grad.setColorAt(1.0, QColor(0, 255, 65, 0))
        p.fillRect(QRectF(0, 0, w, h), grad)

    def _draw_stream(self, p, c, t):
        w, h = c["w"], c["h"]
        n = len(self._stream)
        for i, col in enumerate(self._stream):
            x = (i + 0.5) * w / n
            head = (col["seed"] + t * col["speed"]) % 1.0
            hy = h * 0.9 * head
            p.fillRect(QRectF(x, hy, 2, 3), QColor(0, 255, 65, 26))
            for k in range(1, col["len"] + 1):
                ty = hy - k * 5.0
                if ty >= 0:
                    p.fillRect(QRectF(x, ty, 2, 2),
                               QColor(0, 255, 65, max(0, 20 - k * 3)))

    def _draw_grid(self, p, c):
        w, h, cx = c["w"], c["h"], c["cx"]
        p.setPen(c["grid_pen"])
        for i in range(-7, 8):
            p.drawLine(QPointF(cx + i * w * 0.14, h),
                       QPointF(cx + i * w * 0.045, h * 0.84))
        for i in range(0, 7):
            y = h * 0.84 + (h - h * 0.84) * i / 6.0
            p.drawLine(QPointF(0, y), QPointF(w, y))

    def _draw_corners(self, p, c):
        """四角 HUD 括号：黑客终端的屏幕框架。"""
        w, h = c["w"], c["h"]
        m, L = 10, 30
        pen = QPen(QColor(0, 255, 65, 110), 1)
        p.setPen(pen)
        p.drawLine(QPointF(m, m + L), QPointF(m, m))
        p.drawLine(QPointF(m, m), QPointF(m + L, m))
        p.drawLine(QPointF(w - m - L, m), QPointF(w - m, m))
        p.drawLine(QPointF(w - m, m), QPointF(w - m, m + L))
        p.drawLine(QPointF(m, h - m - L), QPointF(m, h - m))
        p.drawLine(QPointF(m, h - m), QPointF(m + L, h - m))
        p.drawLine(QPointF(w - m - L, h - m), QPointF(w - m, h - m))
        p.drawLine(QPointF(w - m, h - m), QPointF(w - m, h - m - L))

    def _draw_globe(self, p, c):
        """全屏 3D 地球背景（预渲染 QPixmap，绘制时仅一次 drawPixmap）。"""
        if self._globe_pm is None:
            return
        w, h = c["w"], c["h"]
        p.setOpacity(0.90)
        p.drawPixmap(0, 0, self._globe_pm)
        p.setOpacity(1.0)
        p.setFont(c["f_line"])
        p.setPen(QColor(0, 130, 45))
        label = "GLOBAL NETWORK  //  RELAY 02"
        lw = p.fontMetrics().horizontalAdvance(label)
        p.drawText(QPointF(w - lw - 26, h - 58), label)

    def _draw_title(self, p, c, t):
        a = int(200 * ease_in_out(clamp01((t - T_TITLE) / 0.14)))
        p.setFont(c["f_title"])
        p.setPen(QColor(0, 110, 40, min(255, a)))
        p.drawText(QPointF(16, 22), "BL4CK://SHELL v1.0  //  NEON CONSOLE")

    def _draw_logo(self, p, c, t):
        w, h, cx = c["w"], c["h"], c["cx"]
        p.setFont(c["f_logo"])
        fm = p.fontMetrics()
        adv = fm.horizontalAdvance("B")
        x0 = cx - adv * len(LOGO_TEXT) / 2.0
        base = h * 0.34
        # 字母逐个「爆闪」进场：back.out 弹起 + 辉光
        for i, ch in enumerate(LOGO_TEXT):
            st = T_LOGO + i * LOGO_STAGGER
            q = clamp01((t - st) / LOGO_POP)
            x = x0 + i * adv
            if q <= 0:
                p.setPen(QColor(0, 90, 30, 140))          # 未亮：暗轮廓
                p.drawText(QPointF(x, base + fm.ascent()), ch)
                continue
            e = ease_out_back(q)                           # 轻微过冲
            alpha = min(255, int(255 * e))
            y = base + fm.ascent() + int((1 - e) * 8)      # 从下方 8px 弹起
            p.setPen(QColor(0, 255, 65, int(70 * e)))      # 辉光层
            p.drawText(QPointF(x + 1.5, y + 1.5), ch)
            p.setPen(QColor(0, 255, 65, alpha))            # 主体
            p.drawText(QPointF(x, y), ch)
        # 偶发乱码干扰帧（logo 阶段，瞬时“坏一字母”）
        if (0.15 < t < T_LOGO + len(LOGO_TEXT) * LOGO_STAGGER + 0.20
                and random.random() < 0.05):
            i = self._rng.randrange(len(LOGO_TEXT))
            if t > T_LOGO + i * LOGO_STAGGER:
                p.setFont(c["f_logo"])
                p.setPen(QColor(0, 220, 90, 190))
                p.drawText(QPointF(x0 + i * adv, base + fm.ascent()),
                           self._rng.choice(GLITCH_CHARS))

    def _draw_sweep(self, p, c, t):
        w, h = c["w"], c["h"]
        if T_SWEEP <= t <= T_SWEEP + 0.16:                 # 一次干净的下扫脉冲
            q = ease_in_out(clamp01((t - T_SWEEP) / 0.16))
            sy = h * (0.15 + 0.65 * q)
            p.setPen(c["sweep_pen"])
            p.drawLine(QPointF(0, sy), QPointF(w, sy))
            tg = QLinearGradient(0, sy - 60, 0, sy)
            tg.setColorAt(0, QColor(0, 255, 65, 0))
            tg.setColorAt(1, QColor(0, 255, 65, 60))
            p.fillRect(QRectF(0, sy - 60, w, 60), tg)

    def _draw_lines(self, p, c, t):
        w, h = c["w"], c["h"]
        p.setFont(c["f_line"])
        fm = p.fontMetrics()
        x0, y0 = 26.0, h * 0.60
        fh = fm.height() + 4
        rows = []
        for ln in self._lines:
            q = clamp01((t - ln["start"]) / ln["dur"])
            if q <= 0:
                continue
            if q >= 1:
                rows.append((ln["text"], ln["ok"], 255))
            else:                                          # 解码中：乱码滚回
                head, tail = _crackle(ln["text"], q, self._rng)
                rows.append((head + tail, ln["ok"], int(90 + 165 * q)))
        for i, (text, ok, alpha) in enumerate(rows):
            if not text:
                continue
            p.setPen(QColor(0, 255, 65, alpha) if ok else QColor(255, 68, 68, alpha))
            p.drawText(QPointF(x0, y0 + i * fh), text)

    def _draw_glitch(self, p, c):
        """随机全宽故障闪帧：一行乱码在随机位置闪现 1-2 帧（黑客神秘感）。"""
        w, h = c["w"], c["h"]
        if self._glitch_frames > 0:
            self._glitch_frames -= 1
            p.setFont(c["f_line"])
            fm = p.fontMetrics()
            adv = max(1, fm.horizontalAdvance("W"))
            n = int(w / adv) + 1
            p.setPen(QColor(0, 255, 65, 150))
            p.drawText(QPointF(0, self._glitch_y),
                       "".join(random.choice(GLITCH_CHARS) for _ in range(n)))
        elif random.random() < 0.008:
            self._glitch_frames = 2
            self._glitch_y = random.randrange(24, h - 40)

    def _draw_crt(self, p):
        """CRT 扫描线：整屏每 2px 一条极淡暗带（一次纹理平铺填充）。"""
        p.fillRect(self.rect(), self._crt_brush())

    def _crt_brush(self):
        if self._crt_brush_obj is None:
            pm = QPixmap(2, 4)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.fillRect(QRectF(0, 0, 2, 1), QColor(0, 0, 0, 40))
            q.fillRect(QRectF(0, 2, 2, 1), QColor(0, 0, 0, 40))
            q.end()
            self._crt_brush_obj = QBrush(pm)
        return self._crt_brush_obj

    def _draw_progress(self, p, c, t):
        w, h = c["w"], c["h"]
        q = ease_out_expo(clamp01((t - T_PROG) / 0.33))    # 收尾 expo 冲刺
        bx, by = 26.0, h - 26
        bw = w - 52
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 60, 20))
        p.drawRect(QRectF(bx, by, bw, 6))
        if q > 0:
            p.setBrush(QColor(0, 255, 65))
            p.drawRect(QRectF(bx, by, bw * q, 6))
        p.setFont(c["f_prog"])
        p.setPen(QColor(0, 170, 47))
        p.drawText(QPointF(bx, by - 6), f"{q * 100:3.0f}%")

    def _draw_ready(self, p, c, t):
        if self._ready_at < 0:
            return
        w, h, cx = c["w"], c["h"], c["cx"]
        if int((t - self._ready_at) * 7) % 2:              # 7Hz 高频闪烁
            return
        p.setFont(c["f_ready"])
        rw = p.fontMetrics().horizontalAdvance(READY_TEXT)
        p.setPen(QColor(0, 255, 65, 60))                   # 辉光层
        p.drawText(QPointF(cx - rw / 2.0 + 1.5, h * 0.52 + 1.5), READY_TEXT)
        p.setPen(QColor(0, 255, 65, 235))                  # 主体
        p.drawText(QPointF(cx - rw / 2.0, h * 0.52), READY_TEXT)
