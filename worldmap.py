"""3D 旋转球体世界地图 + 攻击弧线动画。

把 LAND_POLYS / CITIES 的真实经纬度数据投到球面：绕 Y 轴缓慢自转 + 固定
倾斜角，弱透视投影回屏幕。绘制：经纬网（graticule）、大陆点阵（按深度
着色）、城市光点、沿大圆飞行的攻击弧线、目标脉冲环、真实位置金标。
数据层（城市/大陆坐标）保持真实，只有投影与绘制是演出层。
"""
import math
import random

from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget

from theme import (BLACK, GREEN, GREEN_DIM, GREEN_DARK, GREEN_FAINT, AMBER,
                   RED, mono)

# ---------------------------------------------------------------- 经纬数据
LAT_MIN, LAT_MAX = -58.0, 84.0
GRID_COLS, GRID_ROWS = 180, 71          # 等距圆柱，裁掉极点（点阵采样用）


def _grid_cell(lon: float, lat: float):
    c = (lon + 180.0) / 360.0 * (GRID_COLS - 1)
    r = (LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * (GRID_ROWS - 1)
    return int(c), int(r)


def _to_lon(lat_row, col):
    lon = col / (GRID_COLS - 1) * 360 - 180
    lat = LAT_MAX - lat_row / (GRID_ROWS - 1) * (LAT_MAX - LAT_MIN)
    return lon, lat


# ---------------------------------------------------------------- 大陆多边形 (lon, lat)
LAND_POLYS = [
    # 北美
    [(-168, 66), (-160, 71), (-141, 70), (-128, 70), (-110, 73), (-95, 78), (-82, 76),
     (-72, 70), (-64, 62), (-58, 55), (-55, 50), (-60, 45), (-66, 44), (-70, 42),
     (-75, 38), (-76, 35), (-81, 25), (-84, 29), (-90, 29), (-97, 26), (-97, 20),
     (-105, 22), (-110, 24), (-117, 32), (-124, 34), (-125, 40), (-128, 48),
     (-133, 55), (-137, 58), (-145, 60), (-156, 57), (-164, 60)],
    # 格陵兰
    [(-46, 60), (-30, 60), (-22, 68), (-20, 76), (-30, 82), (-46, 82), (-58, 78), (-55, 70)],
    # 南美
    [(-81, 9), (-72, 12), (-60, 11), (-50, 5), (-44, 0), (-35, -6), (-35, -12),
     (-39, -18), (-44, -23), (-48, -28), (-58, -34), (-62, -40), (-68, -46),
     (-72, -53), (-74, -46), (-72, -35), (-71, -20), (-78, -8), (-80, -3), (-81, 4)],
    # 欧洲
    [(-9, 37), (-5, 36), (3, 36), (9, 39), (15, 38), (20, 40), (27, 41), (30, 46),
     (36, 45), (40, 50), (40, 58), (33, 62), (25, 66), (18, 68), (10, 63), (5, 60),
     (-1, 58), (-5, 56), (-6, 52), (-9, 48), (-9, 42)],
    # 非洲
    [(-17, 14), (-17, 21), (-10, 28), (-5, 35), (5, 37), (11, 33), (20, 32), (25, 32),
     (33, 31), (35, 24), (38, 18), (43, 12), (51, 12), (51, 4), (46, -1), (41, -10),
     (35, -18), (32, -26), (25, -34), (18, -34), (14, -28), (12, -18), (13, -6),
     (9, 0), (5, 4), (-2, 5), (-8, 4), (-14, 8), (-17, 11)],
    # 亚洲主体
    [(30, 41), (36, 45), (40, 50), (45, 40), (48, 30), (45, 15), (52, 12), (55, 20),
     (57, 25), (62, 25), (67, 25), (72, 18), (78, 8), (80, 15), (87, 21), (92, 21),
     (95, 16), (98, 8), (100, 5), (103, 2), (105, 10), (108, 11), (109, 18), (117, 21),
     (121, 30), (122, 40), (126, 40), (130, 42), (131, 46), (135, 45), (140, 48),
     (142, 55), (145, 60), (150, 65), (160, 70), (170, 72), (180, 70), (180, 78),
     (140, 78), (100, 78), (60, 78), (50, 72), (45, 65), (40, 58)],
    # 马来群岛
    [(95, 5), (105, 3), (113, 3), (118, -2), (115, -7), (110, -8), (105, -6), (100, -3)],
    [(120, 2), (126, 1), (131, -2), (132, -8), (124, -9), (120, -6)],
    # 日本
    [(130, 32), (135, 33), (140, 35), (142, 40), (145, 44), (143, 45), (139, 42), (135, 36), (131, 33)],
    # 大不列颠
    [(-6, 50), (-1, 51), (1, 53), (-2, 58), (-5, 58), (-6, 55)],
    # 新西兰
    [(166, -46), (174, -41), (178, -37), (175, -37), (170, -42), (166, -45)],
    # 澳大利亚
    [(113, -22), (118, -20), (122, -17), (130, -12), (136, -13), (141, -13), (145, -15),
     (147, -19), (152, -25), (154, -28), (151, -35), (146, -38), (140, -38), (135, -35),
     (128, -32), (120, -34), (115, -34), (113, -27)],
    # 马达加斯加
    [(43, -12), (50, -15), (50, -25), (45, -25), (43, -20)],
    # 新几内亚
    [(131, -1), (141, -2), (150, -5), (150, -10), (141, -10), (133, -4)],
    # 冰岛
    [(-24, 64), (-14, 64), (-14, 66), (-24, 66)],
]

# ---------------------------------------------------------------- 城市 (真实坐标)
CITIES = [
    ("New York", -74.00, 40.71, "United States", "AT&T"),
    ("Los Angeles", -118.24, 34.05, "United States", "Comcast"),
    ("Miami", -80.19, 25.76, "United States", "Spectrum"),
    ("Mexico City", -99.13, 19.43, "Mexico", "Telmex"),
    ("Sao Paulo", -46.63, -23.55, "Brazil", "Vivo"),
    ("Buenos Aires", -58.38, -34.60, "Argentina", "Telecom Argentina"),
    ("Lima", -77.04, -12.05, "Peru", "Entel"),
    ("London", -0.13, 51.51, "United Kingdom", "BT Group"),
    ("Paris", 2.35, 48.86, "France", "Orange"),
    ("Berlin", 13.40, 52.52, "Germany", "Deutsche Telekom"),
    ("Istanbul", 28.98, 41.01, "Turkey", "Turkcell"),
    ("Ankara", 32.86, 39.93, "Turkey", "Turk Telekom"),
    ("Moscow", 37.62, 55.75, "Russia", "Rostelecom"),
    ("St Petersburg", 30.32, 59.93, "Russia", "Vimpelcom"),
    ("Cairo", 31.24, 30.04, "Egypt", "WE"),
    ("Lagos", 3.38, 6.52, "Nigeria", "MTN Nigeria"),
    ("Johannesburg", 28.05, -26.20, "South Africa", "Vodacom"),
    ("Nairobi", 36.82, -1.29, "Kenya", "Safaricom"),
    ("Dubai", 55.30, 25.27, "UAE", "Etisalat"),
    ("Delhi", 77.21, 28.61, "India", "Airtel"),
    ("Mumbai", 72.88, 19.08, "India", "Vodafone Idea"),
    ("Bangkok", 100.50, 13.75, "Thailand", "AIS"),
    ("Singapore", 103.82, 1.35, "Singapore", "Singtel"),
    ("Hong Kong", 114.17, 22.32, "Hong Kong", "PCCW"),
    ("Beijing", 116.41, 39.90, "China", "China Unicom"),
    ("Shanghai", 121.47, 31.23, "China", "China Telecom"),
    ("Tokyo", 139.69, 35.68, "Japan", "NTT"),
    ("Seoul", 126.98, 37.57, "South Korea", "KT"),
    ("Sydney", 151.21, -33.87, "Australia", "Telstra"),
    ("Melbourne", 144.96, -37.81, "Australia", "Optus"),
]


def _in_poly(x, y, poly):
    """射线法点在多边形内测试。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def build_land_mask():
    """栅格化大陆 -> [[bool, ...], ...]，(row, col)。"""
    mask = []
    for r in range(GRID_ROWS):
        row = []
        for c in range(GRID_COLS):
            lon, lat = _to_lon(r, c)
            on = any(_in_poly(lon, lat, p) for p in LAND_POLYS)
            row.append(on)
        mask.append(row)
    return mask


_LAND_CACHE = None


def land_mask():
    """模块级缓存的大陆栅格（纯确定性计算，进程内只算一次）。"""
    global _LAND_CACHE
    if _LAND_CACHE is None:
        _LAND_CACHE = build_land_mask()
    return _LAND_CACHE


# ---------------------------------------------------------------- 球面投影
def _ll2vec(lon: float, lat: float):
    """经纬度 -> 单位球面向量 (x 右, y 上, z 朝向观察者为 +)。"""
    la, lo = math.radians(lat), math.radians(lon)
    cl = math.cos(la)
    return (cl * math.cos(lo), math.sin(la), cl * math.sin(lo))


def _rotate(v, spin, tilt):
    """绕 Y 轴自转 spin，再绕 X 轴倾斜 tilt（让北极略倾向观察者）。"""
    x, y, z = v
    c, s = math.cos(spin), math.sin(spin)
    x1 = x * c + z * s
    z1 = -x * s + z * c
    c2, s2 = math.cos(tilt), math.sin(tilt)
    y2 = y * c2 - z1 * s2
    z2 = y * s2 + z1 * c2
    return x1, y2, z2


def _mixc(a: QColor, b: QColor, t: float):
    """两个 QColor 线性混合（0..1）。"""
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _densify(poly, step=2.0):
    """把大陆多边形边按 ~step° 加密成闭合点列（球面上画平滑轮廓用）。"""
    out = []
    n = len(poly)
    for i in range(n):
        lon0, lat0 = poly[i]
        lon1, lat1 = poly[(i + 1) % n]
        k = max(1, int(math.hypot(lon1 - lon0, lat1 - lat0) / step))
        for j in range(k):
            out.append((lon0 + (lon1 - lon0) * j / k,
                        lat0 + (lat1 - lat0) * j / k))
    return out


def _gc_raw(src_ll, dst_ll, n=42):
    """两点球面大圆的插值点列 [(vx, vy, vz, u)]（u 为沿程参数 0..1）。"""
    v0, v1 = _ll2vec(*src_ll), _ll2vec(*dst_ll)
    d = max(-1.0, min(1.0, v0[0] * v1[0] + v0[1] * v1[1] + v0[2] * v1[2]))
    om = math.acos(d)
    so = math.sin(om)
    pts = []
    for i in range(n + 1):
        u = i / n
        if so > 1e-4:
            a = math.sin((1 - u) * om) / so
            b = math.sin(u * om) / so
            pts.append((a * v0[0] + b * v1[0], a * v0[1] + b * v1[1],
                        a * v0[2] + b * v1[2], u))
        else:  # 近对跖点退化：走中点插值
            pts.append(((v0[0] + v1[0]) / 2, (v0[1] + v1[1]) / 2,
                        (v0[2] + v1[2]) / 2, u))
    return pts


class WorldMapWidget(QWidget):
    """3D 旋转球体世界地图 + 大圆攻击弧线。target_changed 通知上层切目标。"""

    target_changed = pyqtSignal(tuple)   # (x, y, name, country, isp, lon, lat)

    SPIN = 0.008        # 每帧自转弧度（约 26s 转一圈）
    TILT = 0.38         # 观看倾斜角（弧度）

    def __init__(self, height=240, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self._bg_fill = QColor(BLACK)   # 背景填充；boot 覆盖层可改透明露出下层代码雨
        self._sphere_fill = None        # 球体内不透明填充（None=不画）；boot 用它挡雨
        self.mask = land_mask()
        self.city_pts = []          # [(x_norm, y_norm, name, country, isp, lon, lat)]
        for name, lon, lat, country, isp in CITIES:
            c, r = _grid_cell(lon, lat)
            self.city_pts.append((c / (GRID_COLS - 1), r / (GRID_ROWS - 1),
                                  name, country, isp, lon, lat))

        self.arcs = []              # dict(src, dst, t, dur, bulge)  src/dst 为城市索引
        self.target_idx = 14        # 初始目标 Cairo
        self.city_phase = {}
        for i in range(len(self.city_pts)):
            self.city_phase[i] = random.uniform(0, 6.28)

        self.rot = 0.0              # 自转角度
        self.tick_n = 0
        self._real = None           # (lat, lon, label)：whereami 联动的真实位置
        self._spawn_every = 26      # 约每 0.85s 发一条弧线
        self._cs_all = None         # 当前帧旋转矩阵 (cos,sin,cosTILT,sinTILT)，_frame 里算
        self._frm_land = None       # 帧缓存：大陆点阵 [(pt, z2)]；None=未算（首帧懒算）
        self._frm_grid = []
        self._frm_polys = []
        self._frm_cities = []
        # ---- 静态几何缓存（不随自转变化，只算一次）----
        # 之前每帧 paintEvent 要重算 ~4800 次 _ll2vec（trig）+ _rotate（4 次 trig）：
        # 实测 140ms/帧、30fps = 单核 100%+，整个 GUI 被拖到 13Hz。改为：
        # 球面向量（经纬度 → 单位向量）全部预计算；每帧只剩乘加投影。
        self._cs = (math.cos(self.TILT), math.sin(self.TILT))   # 倾斜角固定
        self._geo_cache = None
        self._f8 = mono(8)          # 标签字体只建一次（每帧 new QFont 慢）
        # 大陆点阵深度色表：z2 ∈ [0,1]，量化 24 级预生成，避免每帧 700 次 _mixc
        self._depth_colors = [
            QColor(int(GREEN_FAINT.red() + (GREEN_DIM.red() - GREEN_FAINT.red()) * (i / 23.0) ** 1.5),
                   int(GREEN_FAINT.green() + (GREEN_DIM.green() - GREEN_FAINT.green()) * (i / 23.0) ** 1.5),
                   int(GREEN_FAINT.blue() + (GREEN_DIM.blue() - GREEN_FAINT.blue()) * (i / 23.0) ** 1.5))
            for i in range(24)]
        self._city_vec = [(_ll2vec(lon, lat), name, country, isp, lon, lat)
                          for name, lon, lat, country, isp in CITIES]
        self._land_vec = []
        for r in range(0, GRID_ROWS, 2):
            row = self.mask[r]
            for c in range(0, GRID_COLS, 2):
                if not row[c]:
                    continue
                self._land_vec.append(_ll2vec(*_to_lon(r, c)))
        self._grid_vecs = []        # 经纬网：每条链的静态单位向量列表
        for lo in range(-180, 180, 30):
            self._grid_vecs.append([_ll2vec(float(lo), float(la))
                                    for la in range(-84, 85, 3)])
        for la in range(-60, 85, 30):
            self._grid_vecs.append([_ll2vec(float(lo), float(la))
                                    for lo in range(-180, 181, 4)])
        self._poly_vecs = []        # 大陆轮廓：加密点列的静态单位向量
        for poly in LAND_POLYS:
            self._poly_vecs.append([_ll2vec(lon, lat)
                                    for lon, lat in _densify(poly, 2.0)])
        self.t = QTimer(self)
        self.t.timeout.connect(self._tick)
        self.t.start(66)            # 15fps：预计算后每帧 ~5ms 投影 + 绘制，CPU 可控
                                    # （终端长进程刷屏时世界地图动画与终端解析争单核，
                                    # 曾拖慢终端到 ~15Hz；实测降帧收益在噪声内，
                                    # 真机上 10fps 旋转地图反而明显变顿，保持 66ms）

    def target(self):
        return self.city_pts[self.target_idx]

    # ---------------- 投影 ----------------
    def _geo(self):
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        R = max(40, min(w, h) / 2.0 * 0.86)
        return cx, cy, R

    def _xp(self, v):
        """单位/任意球面向量 -> (屏幕点 or None, 深度 z2)。

        旋转矩阵（self._cs_all）与投影几何（self._geo_cache）都在 _frame()
        里按当前 rot 每帧只算一次；这里只剩乘加，~1µs/点。
        """
        c, s, c2, s2 = self._cs_all
        x, y, z = v
        x1 = x * c + z * s
        z1 = -x * s + z * c
        y2 = y * c2 - z1 * s2
        z2 = y * s2 + z1 * c2
        if z2 <= 0.02:
            return None, z2
        cx, cy, R = self._geo_cache
        f = 1.0 / (1.0 - z2 / 3.2)    # 弱透视：越靠近观察者越大
        return (cx + x1 * R * f, cy - y2 * R * f), z2

    def _frame(self):
        """把全部静态几何投影到当前 rot，结果缓存供 paintEvent 直接绘制。

        旧实现每帧在 paintEvent 里重算 ~4800 次 _ll2vec/_rotate/_proj_ll，
        每次 ~4 个 trig，实测 140ms/帧拖死事件循环；现在静态向量只建一次，
        每帧 _xp 只做乘加，~4700 点 < 5ms。
        """
        r = self.rot
        self._cs_all = (math.cos(r), math.sin(r), self._cs[0], self._cs[1])
        cx, cy, R = self._geo()
        self._geo_cache = (cx, cy, R)
        self._frm_land = []
        for v in self._land_vec:            # 大陆点阵（只留可见点，含深度）
            pt, z2 = self._xp(v)
            if pt is not None:
                self._frm_land.append((pt, z2))
        self._frm_grid = [[self._xp(v)[0] for v in ch] for ch in self._grid_vecs]
        self._frm_polys = [[self._xp(v)[0] for v in ch] for ch in self._poly_vecs]
        self._frm_cities = [self._xp(v[0])[0] for v in self._city_vec]

    def _proj_ll(self, lon, lat):
        """兼容旧接口：经纬度 -> (屏幕点 or None, x, y, z)。内部已不用，供外部调用。"""
        v = _rotate(_ll2vec(lon, lat), self.rot, self.TILT)
        pt = self._proj_vec(v)
        return pt, v[0], v[1], v[2]

    def set_real_location(self, lat, lon, label):
        """whereami 联动：把真实本机位置标在地图上。"""
        self._real = (lat, lon, label)
        self.update()

    # ---------------- 动画 ----------------
    def _tick(self):
        self.tick_n += 1
        self.rot = (self.rot + self.SPIN) % (2 * math.pi)
        self._frame()               # 预计算整帧投影（静态几何 + 当前 rot）
        self.arcs = [a for a in self.arcs if a["t"] < 1.0]
        for a in self.arcs:
            a["t"] += 1.0 / a["dur"]
        if self.tick_n % self._spawn_every == 0:
            self._spawn_arc()
        if self.tick_n % 240 == 0 and len(self.arcs) < 14:
            # 每 8s 换一个目标城市
            self.target_idx = random.choice([i for i in range(len(self.city_pts))
                                             if i != self.target_idx])
            self.target_changed.emit(self.city_pts[self.target_idx])
        self.update()

    def _spawn_arc(self):
        if not self.city_pts or len(self.arcs) > 14:
            return
        src_idx = random.choice([i for i in range(len(self.city_pts))
                                 if i != self.target_idx])
        src = self.city_pts[src_idx]
        dst = self.city_pts[self.target_idx]
        self.arcs.append({
            "src": src_idx, "dst": self.target_idx,
            "t": 0.0,
            "dur": random.randint(70, 110),
            "bulge": random.uniform(0.12, 0.34),
            "raw": _gc_raw((src[5], src[6]), (dst[5], dst[6]), 42),   # 静态大圆点列
        })

    def clear_arcs(self):
        self.arcs = []
        self.update()

    # ---------------- 绘制 ----------------
    def _draw_chain(self, p, pts, color, width=1.0):
        """把投影点列连成折线；跨越背面（None）处断开。"""
        p.setPen(QPen(QColor(color), width))
        for i in range(1, len(pts)):
            if pts[i - 1] is None or pts[i] is None:
                continue
            p.drawLine(QPointF(*pts[i - 1]), QPointF(*pts[i]))

    def _paint_arc(self, p, a):
        n = 42
        head = min(n, int(a["t"] * n))
        bulge = a["bulge"]
        pts = []
        for vx, vy, vz, u in a["raw"]:          # 静态点列，每帧只做抬升+投影
            rf = 1.0 + 0.85 * bulge * math.sin(math.pi * u)   # 弧顶抬升
            pts.append(self._xp((vx * rf, vy * rf, vz * rf))[0])
        # 尾巴：越靠近起点越暗
        for i in range(max(1, head - 26), head + 1):
            if pts[i - 1] is None or pts[i] is None:
                continue
            k = (i - (head - 26)) / 27.0
            col = QColor(GREEN.red(), GREEN.green(), GREEN.blue(), int(40 + 200 * k))
            p.setPen(QPen(col, 1.4 if k > 0.7 else 1.0,
                          Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(*pts[i - 1]), QPointF(*pts[i]))
        # 弹头（2px 方格）
        if 0 < head <= n and pts[head] is not None:
            hx, hy = pts[head]
            p.setPen(Qt.PenStyle.NoPen)
            hxi, hyi = int(hx), int(hy)
            p.setBrush(QColor(GREEN.red(), GREEN.green(), GREEN.blue(), 90))
            p.drawRect(hxi - 4, hyi - 4, 8, 8)
            p.setBrush(QColor(0xcc, 0xff, 0xcc))
            p.drawRect(hxi - 1, hyi - 1, 3, 3)

    def paintEvent(self, e):
        w, h = self.width(), self.height()
        cx, cy, R = self._geo()
        now = self.tick_n * 0.09
        if self._frm_land is None:          # 首帧（_tick 尚未跑）懒算一次
            self._frame()

        p = QPainter(self)
        # 反锯齿在软光栅下是主要开销（272×250 上几千个 1px 圆点）；1-2px
        # 的点/线关掉 AA 视觉几乎无差，但 paint 从 ~35ms 降到个位数 ms。
        p.fillRect(0, 0, w, h, self._bg_fill)

        # 球体不透明底：只填球内，球外保持透明（boot 背景雨从球外透出）
        if self._sphere_fill is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._sphere_fill)
            p.drawEllipse(QPointF(cx, cy), R, R)

        # 球体体积感：径向渐变
        grad = QRadialGradient(cx, cy, R)
        grad.setColorAt(0.0, QColor(GREEN_FAINT.red(), GREEN_FAINT.green(),
                                    GREEN_FAINT.blue(), 26))
        grad.setColorAt(0.85, QColor(GREEN_FAINT.red(), GREEN_FAINT.green(),
                                     GREEN_FAINT.blue(), 8))
        grad.setColorAt(1.0, QColor(GREEN_DARK.red(), GREEN_DARK.green(),
                                    GREEN_DARK.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawEllipse(QPointF(cx, cy), R, R)

        # 经纬网 graticule（帧缓存链）
        grid_col = QColor(GREEN_DARK)
        for chain in self._frm_grid:
            self._draw_chain(p, chain, grid_col, 0.8)

        # 大陆点阵：按深度着色（帧缓存已含背面裁切）；2px 方格代替椭圆（快）
        p.setPen(Qt.PenStyle.NoPen)
        dc = self._depth_colors
        for pt, z2 in self._frm_land:
            p.setBrush(dc[min(23, int(z2 * 23))])
            p.drawRect(int(pt[0]) - 1, int(pt[1]) - 1, 2, 2)

        # 大陆边缘轮廓（帧缓存链）
        for chain in self._frm_polys:
            self._draw_chain(p, chain, QColor(GREEN_DIM), 1.0)

        # 城市光点 + 呼吸光晕
        for i, pt in enumerate(self._frm_cities):
            if pt is None:
                continue
            px, py = int(pt[0]), int(pt[1])
            ph = self.city_phase[i] + now
            a = 0.5 + 0.5 * math.sin(ph)
            col = QColor(AMBER) if i < 4 else QColor(GREEN)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRect(px - 1, py - 1, 3, 3)
            if i != self.target_idx and a > 0.72:
                p.setBrush(QColor(GREEN.red(), GREEN.green(), GREEN.blue(),
                                  int(90 * a)))
                p.drawRect(px - 2, py - 2, 5, 5)

        # 攻击弧线（沿大圆飞行）
        for a in self.arcs:
            self._paint_arc(p, a)

        # 目标标记：脉冲环 + 十字
        tcity = self.city_pts[self.target_idx]
        tres = self._xp(self._city_vec[self.target_idx][0])
        if tres[0]:
            tpx, tpy = tres[0]
            ring = 5 + 9 * (0.5 + 0.5 * math.sin(now * 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(RED.red(), RED.green(), RED.blue(), 150), 1.2))
            p.drawEllipse(QPointF(tpx, tpy), ring, ring)
            p.setPen(QPen(QColor(RED), 1.0))
            p.drawLine(QPointF(tpx - 8, tpy), QPointF(tpx - 3, tpy))
            p.drawLine(QPointF(tpx + 3, tpy), QPointF(tpx + 8, tpy))
            p.drawLine(QPointF(tpx, tpy - 8), QPointF(tpx, tpy - 3))
            p.drawLine(QPointF(tpx, tpy + 3), QPointF(tpx, tpy + 8))
            p.setPen(QPen(QColor(RED), 1.0))
            p.setFont(self._f8)
            p.drawText(QRectF(tpx + 9, tpy - 9, 120, 14), tcity[2])

        # 真实位置标记（whereami 联动）：金色脉冲点 + YOU 标签
        if self._real:
            rlat, rlon, rlabel = self._real
            rres = self._xp(_ll2vec(rlon, rlat))
            if rres[0]:
                rx, ry = rres[0]
                pulse = 3 + 5 * (0.5 + 0.5 * math.sin(now * 2.8))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(0xff, 0xc4, 0x2e, 180), 1.2))
                p.drawEllipse(QPointF(rx, ry), pulse, pulse)
                p.setBrush(QColor(0xff, 0xd6, 0x5c, 220))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(rx, ry), 2.2, 2.2)
                p.setPen(QPen(QColor(0xff, 0xc4, 0x2e), 1.0))
                p.setFont(self._f8)
                p.drawText(QRectF(rx + 7, ry - 14, 120, 14), f"YOU {rlabel}")

        # 球体轮廓圈
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(GREEN_DARK), 1.0))
        p.drawEllipse(QPointF(cx, cy), R, R)

        # 四角科技框
        p.setPen(QPen(QColor(GREEN_DARK), 1))
        p.drawLine(0, 0, 18, 0)
        p.drawLine(0, 0, 0, 18)
        p.drawLine(w, 0, w - 18, 0)
        p.drawLine(w, 0, w, 18)
        p.drawLine(0, h - 1, 18, h - 1)
        p.drawLine(0, h - 1, 0, h - 19)
        p.drawLine(w, h - 1, w - 18, h - 1)
        p.drawLine(w, h - 1, w, h - 19)
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)
