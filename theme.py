"""全局主题：经典绿黑配色 + 等宽字体，支持 eDEX-UI 主题 JSON 热切换。

核心技巧：GREEN/BLACK 等是模块级 QColor「单例」，所有控件 paint 时
`QColor(GREEN.name())` 都会在绘制瞬间取值 —— 因此 apply_theme() 里
`GREEN.setRgb(...)` 原地改色后，所有自绘路径自动跟随新主题，无需重写。

代价是 setStyleSheet 里的 QSS 字符串是「设置时」固化的颜色，主题切换
时必须重建。方案：所有在 __init__ 里 setStyleSheet 的控件实现
`retint()` 方法（用当前颜色重新 setStyleSheet），main.py 主题切换后
遍历 widgets 调用；本模块向 QApp 注册一个 hub 信号通知。

主题 JSON（assets/themes/*.json，来自 eDEX-UI）：
    { "colors": {"r":0,"g":143,"b":17, ...},
      "terminal": {"foreground":"#00ff41","background":"#0D0208",
                   "cursor":"#00ff41","cursorAccent":"#000000",
                   "selection":"rgba(0,255,65,0.3)", ...},
      "globe": {"base":"#00ff41","marker":"#0aaaff","pin":"#ff4400","satellite":"#00aa2f"} }
"""
import os
import json

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont

# ---------- 主色板（QColor 单例，可原地 setRgb 热切换） ----------
GREEN = QColor(0x00, 0xFF, 0x41)      # 主前景 / 主亮色
GREEN_DIM = QColor(0x00, 0xAA, 0x2F)  # 次亮（提示符箭头、小字）
GREEN_DARK = QColor(0x00, 0x55, 0x17) # 边框、分隔线
GREEN_FAINT = QColor(0x33, 0x66, 0x44)# 弱化文本
AMBER = QColor(0xFF, 0xB8, 0x00)      # 警示黄
RED = QColor(0xFF, 0x44, 0x44)        # 错误红
BLACK = QColor(0x02, 0x0A, 0x04)      # 窗口底
PANEL_BG = QColor(0x04, 0x10, 0x08)   # 面板底

# 地球（globe）主题色，供 worldmap.py 使用
GLOBE_BASE = QColor(0x00, 0xFF, 0x41)
GLOBE_MARKER = QColor(0x0A, 0xAA, 0xFF)
GLOBE_PIN = QColor(0xFF, 0x44, 0x00)
GLOBE_SILO = QColor(0x00, 0xAA, 0x2F)

FONT_FAMILY = "Cascadia Mono"
FONT_SIZE = 11

_THEME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "themes")
CURRENT = "matrix"


class _Hub(QObject):
    """主题切换通知：changed(str theme_name)。"""
    changed = pyqtSignal(str)


hub = _Hub()


def _rgb(c: QColor) -> str:
    return c.name()


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """线性混合 a→b（t=0 全 a，t=1 全 b）。"""
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _derive(fg: QColor):
    """从主题前景色推导一整套亮→暗绿色阶（偏背景 b）。"""
    GREEN.setRgb(fg.red(), fg.green(), fg.blue())
    m = _mix(fg, QColor(0, 0, 0), 0.55)          # 主暗
    GREEN_DIM.setRgb(m.red(), m.green(), m.blue())
    m = _mix(fg, QColor(0, 0, 0), 0.75)          # 边框
    GREEN_DARK.setRgb(m.red(), m.green(), m.blue())
    m = _mix(fg, QColor(0, 0, 0), 0.88)          # 弱化
    GREEN_FAINT.setRgb(m.red(), m.green(), m.blue())


def _apply_globe(g: dict):
    def setc(qc, key, fallback):
        v = g.get(key)
        if isinstance(v, str) and v.startswith("#") and len(v) == 7:
            try:
                qc.setRgb(int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
                return
            except ValueError:
                pass
        qc.setRgb(fallback.red(), fallback.green(), fallback.blue())
    setc(GLOBE_BASE, "base", GREEN)
    setc(GLOBE_MARKER, "marker", QColor(0x0A, 0xAA, 0xFF))
    setc(GLOBE_PIN, "pin", QColor(0xFF, 0x44, 0x00))
    setc(GLOBE_SILO, "satellite", GREEN_DIM)


# ---------- 主题注册表 ----------
THEMES = {}          # name -> {foreground, background, cursor, selection, globe, colors}
_FALLBACK = {"foreground": "#00ff41", "background": "#0D0208",
             "cursor": "#00ff41", "cursorAccent": "#000000",
             "selection": "rgba(0,255,65,0.3)",
             "globe": {}, "colors": {}}


def _load_themes():
    if not os.path.isdir(_THEME_DIR):
        THEMES["matrix"] = dict(_FALLBACK)
        return
    for fn in sorted(os.listdir(_THEME_DIR)):
        if not fn.endswith(".json"):
            continue
        name = fn[:-5]
        try:
            with open(os.path.join(_THEME_DIR, fn), "r",
                      encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        t = d.get("terminal", {})
        THEMES[name] = {
            "foreground": t.get("foreground", "#00ff41"),
            "background": t.get("background", "#0D0208"),
            "cursor": t.get("cursor", "#00ff41"),
            "cursorAccent": t.get("cursorAccent", "#000000"),
            "selection": t.get("selection", "rgba(0,255,65,0.3)"),
            "globe": d.get("globe", {}),
            "colors": d.get("colors", {}),
        }


_load_themes()
if "matrix" not in THEMES:
    THEMES["matrix"] = dict(_FALLBACK)


def theme_names() -> list:
    return sorted(THEMES)


def apply_theme(name: str) -> bool:
    """切换主题：改单例 QColor + 重建 APP_STYLE + 广播 hub.changed。
    返回是否成功。"""
    global CURRENT, APP_STYLE
    t = THEMES.get(name)
    if t is None:
        return False
    fg = QColor(t["foreground"])
    bg = QColor(t["background"]) if t["background"].startswith("#") else QColor(0x02, 0x0A, 0x04)
    BK = QColor(0, 0, 0)
    _derive(fg)
    BLACK.setRgb(bg.red(), bg.green(), bg.blue())
    PB = _mix(bg, fg, 0.03)                      # 面板底略提亮
    PANEL_BG.setRgb(PB.red(), PB.green(), PB.blue())
    _apply_globe(t.get("globe", {}))
    # AMBER/RED 保留原值——主题 JSON 可能没有对应色，人眼需要稳定语义色
    CURRENT = name
    APP_STYLE = _build_style()
    hub.changed.emit(name)
    return True


def _build_style() -> str:
    b, c = _rgb(BLACK), _rgb(GREEN)
    dim, dark = _rgb(GREEN_DIM), _rgb(GREEN_DARK)
    faint = _rgb(GREEN_FAINT)
    sel = _rgb(GREEN_DARK)
    return f"""
QWidget {{
    background-color: {b};
    color: {c};
    font-family: "{FONT_FAMILY}";
    font-size: {FONT_SIZE}px;
}}
QTabWidget::pane {{
    border: 1px solid {dark};
    background-color: {b};
}}
QTabBar::tab {{
    background-color: {faint};
    color: {dim};
    padding: 6px 16px;
    border: 1px solid {dark};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    font-family: "{FONT_FAMILY}";
    font-size: 11px;
}}
QTabBar::tab:selected {{
    background-color: {b};
    color: {c};
    font-weight: bold;
}}
QTabBar::tab:hover {{ color: {c}; }}
QTreeWidget, QTableWidget, QListWidget {{
    background-color: {PANEL_BG.name()};
    border: 1px solid {dark};
    color: {c};
    alternate-background-color: {_mix(PANEL_BG, QColor(0,0,0), 0.35).name()};
    gridline-color: {dark};
}}
QHeaderView::section {{
    background-color: {faint};
    color: {dim};
    border: 1px solid {dark};
    padding: 3px 6px;
    font-weight: bold;
}}
QTreeWidget::item:selected, QTableWidget::item:selected {{
    background-color: {sel};
    color: #ffffff;
}}
QSplitter::handle {{ background-color: {dark}; }}
QScrollBar:vertical {{ background: {b}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {dark}; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


APP_STYLE = _build_style()


def mono(size: int = FONT_SIZE) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPointSize(size)
    return f