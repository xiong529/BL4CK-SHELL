"""editor.py —— 内置文本编辑器（VS Code 风的基础功能）。

EditorPage 是一个自包含的编辑页：
  - 行号 + 当前行高亮 + 语法高亮（Python 完整，JSON/HTML/CSS/Markdown/JS 简易）
  - 查找: Ctrl+F 查找条（全匹配高亮 + 计数 + Enter/F3 下一个）
  - 缩放: Ctrl+滚轮 / Ctrl+= / Ctrl+- / Ctrl+0
  - 保存: Ctrl+S 手动 + 修改后 800ms 自动保存（"实时保存"）
  - 补全: Python 联想（关键字 + 内置函数 + 文档内收集的名称，Tab/Enter 接受）
  - 快捷键: Ctrl+N 新建, Ctrl+Z/Y 撤销重做, Ctrl+G 跳行, 等
  - 状态栏: 路径 / 语言 / 光标行列 / 大小 / 未保存标记
  - 主题联动：retint() + hub.changed

与外部接线（main.py）：
    page = EditorPage(); page.open(path)
    page.dirty_changed(bool)   -> 更新标签页标题 ●
    page.close_requested()     -> 移除标签页
    page.new_requested()       -> 打开新未命名页
"""
import json
import os
import re
import time

from PyQt6.QtCore import QPoint, Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QPainter, QSyntaxHighlighter,
                         QTextCharFormat, QTextCursor, QTextDocument)
from PyQt6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QListView, QListWidget,
                             QListWidgetItem, QPlainTextEdit, QPushButton, QTextEdit,
                             QVBoxLayout, QWidget)

from theme import (AMBER, BLACK, GREEN, GREEN_DARK, GREEN_DIM, GREEN_FAINT,
                   PANEL_BG, RED, hub, mono)

MAX_BYTES = 1_500_000          # 超过 1.5MB 拒绝打开（避免拖动卡顿）
_BIN_CHUNK = b"\x00"


# --------------------------------------------------------------------------
# 语言注册表
# --------------------------------------------------------------------------
def _lang_of(name):
    ext = os.path.splitext(name)[1].lower()
    table = {
        ".py": "Python", ".pyw": "Python",
        ".js": "JavaScript", ".ts": "JavaScript", ".jsx": "JavaScript",
        ".tsx": "JavaScript",
        ".json": "JSON",
        ".html": "HTML", ".htm": "HTML",
        ".css": "CSS", ".scss": "CSS", ".less": "CSS",
        ".md": "Markdown", ".markdown": "Markdown",
        ".txt": "Plain Text", ".log": "Text Log",
        ".ini": "INI", ".cfg": "INI", ".toml": "INI",
        ".yaml": "YAML", ".yml": "YAML",
        ".xml": "XML",
        ".sh": "Shell", ".bat": "BAT", ".cmd": "BAT", ".ps1": "Shell",
        ".c": "C", ".h": "C", ".cpp": "C/C++", ".hpp": "C/C++",
        ".java": "Java", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
        ".php": "PHP", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift",
        ".csv": "CSV",
    }
    return table.get(ext, "Plain Text")


# --------------------------------------------------------------------------
# Python 语法高亮
# --------------------------------------------------------------------------
_PY_KWORDS = (
    "and as assert async await break class continue def del elif else except "
    "finally for from global if import in is lambda nonlocal not or pass "
    "raise return try while with yield False None True match case"
).split()
_PY_KEYWORDS_ = set(_PY_KWORDS)
_PY_BUILTINS = (
    "abs all any ascii bin bool bytearray bytes callable chr classmethod "
    "compile complex delattr dict dir divmod enumerate eval exec filter "
    "float format frozenset getattr globals hasattr hash help hex id input "
    "int isinstance issubclass iter len list locals map max memoryview min "
    "next object oct open ord pow print property range repr reversed round "
    "set setattr slice sorted staticmethod str sum super tuple type vars zip"
).split()
# VS Code 风格：普通单词联想时，最常用的内置函数/类型排最前
_PY_HOT = {
    "print", "len", "str", "int", "float", "list", "dict", "set", "tuple",
    "range", "input", "open", "sum", "min", "max", "abs", "sorted",
    "enumerate", "zip", "map", "filter", "type", "isinstance", "issubclass",
    "repr", "round", "format", "getattr", "setattr", "hasattr", "callable",
    "iter", "next", "all", "any", "bool", "bytes", "chr", "ord", "hex",
    "oct", "bin", "id", "hash", "dir", "vars", "globals", "locals",
    "super", "staticmethod", "classmethod", "property",
}

# 补全候选：标准库常见模块（前缀匹配到即提示 import xxx）
_PY_MODULES = (
    "argparse asyncio base64 collections configparser contextlib copy "
    "csv dataclasses datetime decimal difflib enum functools glob hashlib "
    "heapq html http importlib itertools json logging math mimetypes "
    "multiprocessing os pathlib pickle platform pprint queue random re "
    "shutil signal socket sqlite3 ssl statistics string struct subprocess "
    "sys tempfile threading time traceback types typing unittest urllib "
    "uuid venv warnings xml zipfile"
).split()
# 模块别名 → 属性列表（`os.` 后给出这些）
_MOD_ALIASES = {
    "os": ("path getcwd getmtime getpid listdir makedirs mkdir remove "
           "rename rmdir scandir sep stat walk").split(),
    "os.path": ("abspath basename commonpath dirname exists expanduser "
                "getsize isabs isdir isfile join normcase normpath "
                "realpath relpath split splitext").split(),
    "sys": ("argv exit executable exitcode maxsize path platform "
            "setrecursionlimit stdin stdout stderr version").split(),
    "json": ("dump dumps load loads").split(),
    "math": ("ceil cos degrees exp fabs floor fsum log log10 pi pow "
             "radians sin sqrt tau trunc").split(),
    "re": ("compile findall finditer fullmatch match search split sub "
           "subn split").split(),
    "time": ("sleep time time_ns time_struct strftime ctime "
             "localtime localtime_monotonic monotonic mktime perf_counter "
             "perf_counter_floating process_time sleep thread_time "
             "time sleep").split(),
}
# 内置类型 → 属性/方法列表（`x.` 后给出这些）
_TYPE_ATTRS = {
    "print": ("__call__",),
    "file": ("close read readline readlines seek tell write writelines "
             "flush truncate fileno isatty name encoding newlines "
             "mode closed").split(),
    "list": ("append clear copy count extend index insert pop remove "
             "reverse sort count index").split(),
    "dict": ("clear copy get items keys pop popitem setdefault values "
             "update get items keys values").split(),
    "set": ("add clear copy difference intersection issuperset issubset "
            "isdisjoint union symmetric_difference update remove discard "
            "pop").split(),
    "str": ("capitalize casefold center count encode expandtabs find "
            "format index isalnum isalpha isdigit islower isspace "
            "istitle isupper join ljust lower lstrip partition replace "
            "rfind rindex rjust rpartition rsplit rstrip split splitlines "
            "startswith strip swapcase title translate upper zfill "
            "encode decode").split(),
    "bytes": ("capitalize count decode find fromhex index isalnum "
              "isalpha isdigit islower isspace isupper join lstrip "
              "replace rstrip split strip translate").split(),
    "int": ("bit_length conjugate hex to_bytes from_bytes as_integer_ratio "
            "real imaginary").split(),
    "float": ("as_integer_ratio conjugate fromhex hex imag real "
              "is_finite is_infinite is_nan to_bytes to_hex").split(),
    "range": ("count index start stop step length_hint empty").split(),
    "tuple": ("count index",),
    "object": ("__class__ __delattr__ __dir__ __doc__ __eq__ __format__ "
               "__getattribute__ __gt__ __hash__ __init__ __init_subclass__ "
               "__le__ __lt__ __ne__ __new__ __reduce__ __reduce_ex__ "
               "__repr__ __setattr__ __sizeof__ __str__ "
               "__subclasshook__").split(),
}

# --------------------------------------------------------------------------
# 词表 JSON 化（PyQ 移植点①）：langdata/python.json 可覆盖上面全部内嵌词表。
# 高亮器与补全同源——外部文件缺失/损坏时回退内嵌常量，永不崩。
# --------------------------------------------------------------------------
_LANG_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "langdata", "python.json")


def _load_lang():
    """装载 python 词表：外部 JSON 优先，内嵌常量兜底（进程内单例）。"""
    default = {
        "keywords": list(_PY_KWORDS),
        "builtins": list(_PY_BUILTINS),
        "hot": sorted(_PY_HOT),
        "modules": list(_PY_MODULES),
        "module_attrs": {k: list(v) for k, v in _MOD_ALIASES.items()},
        "type_attrs": {k: list(v) for k, v in _TYPE_ATTRS.items()},
    }
    if os.path.exists(_LANG_JSON):
        try:
            with open(_LANG_JSON, "r", encoding="utf-8") as f:
                ext = json.load(f)
            for k, v in ext.items():
                if v:
                    default[k] = v
        except (OSError, ValueError):
            pass   # 文件损坏 → 兜底内嵌常量
    return default


_LANG = _load_lang()                       # 模块级装载一次（进程内单例缓存）
_PY_KWORDS = tuple(_LANG["keywords"])
_PY_KEYWORDS_ = set(_LANG["keywords"])
_PY_BUILTINS = tuple(_LANG["builtins"])
_PY_HOT = set(_LANG["hot"])
_PY_MODULES = tuple(_LANG["modules"])
_MOD_ALIASES = {k: tuple(v) for k, v in _LANG["module_attrs"].items()}
_TYPE_ATTRS = {k: tuple(v) for k, v in _LANG["type_attrs"].items()}


_TOKEN_COLORS = {
    # VS Code Dark+ 系配色，集中管理（PyQ/Syn 移植点⑤）：
    # 高亮器/括号配对/当前行全部从这里取色，retint 想换主题只需改这一张表
    "kw":        "#c586c0",   # 关键字（py/js）
    "builtin":   "#4fc1ff",   # 内置函数 / 类型（py）
    "string":    "#ce9178",   # 字符串（py/json/md/html/css/js）
    "comment":   "#6a9955",   # 注释
    "number":    "#b5cea8",   # 数字（py/json）
    "decorator": "#f0d98c",   # 装饰器（py）
    "self":      "#9cdcfe",   # self / 属性名
    "defname":   "#dcdcaa",   # 函数/类名 / css 选择器
    "tag":       "#569cd6",   # html 标签
    "attr":      "#9cdcfe",   # html 属性 / css 属性 / json key
    "heading":   "#569cd6",   # markdown 标题
    "paren":     "#ffd700",   # 括号配对高亮
    "curline":   "#203020",   # 当前行背景
    "find":      "#4a4a12",   # 查找高亮背景
}
# 补全弹层候选 kind → 颜色（值与 _TOKEN_COLORS 同源）
_COMP_KIND_COLORS = {
    "kw":   _TOKEN_COLORS["kw"],
    "bi":   _TOKEN_COLORS["builtin"],
    "mod":  _TOKEN_COLORS["decorator"],   # #f0d98c
    "imp":  _TOKEN_COLORS["number"],      # #b5cea8
    "attr": _TOKEN_COLORS["self"],        # #9cdcfe
    "doc":  _TOKEN_COLORS["defname"],     # #dcdcaa
}


def _fmt(token, bold=False, italic=False, bg=None):
    """按 token 查 _TOKEN_COLORS 建 QTextCharFormat（集中取色）。"""
    f = QTextCharFormat()
    f.setForeground(QColor(_TOKEN_COLORS[token]))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    if bg is not None:
        f.setBackground(QColor(bg))
    return f


class PyHighlighter(QSyntaxHighlighter):
    """Python 语法高亮：关键字/内置/字符串(含跨行三引号)/注释/数字/装饰器/self。"""

    def __init__(self, doc):
        super().__init__(doc)
        F = {}
        F["kw"] = _fmt("kw", bold=True)
        F["builtin"] = _fmt("builtin")
        F["string"] = _fmt("string")
        F["comment"] = _fmt("comment", italic=True)
        F["number"] = _fmt("number")
        F["decorator"] = _fmt("decorator")
        F["self"] = _fmt("self", italic=True)
        F["defname"] = _fmt("defname")
        self._F = F

    def highlightBlock(self, text):
        F = self._F
        n = len(text)
        if n == 0:
            return
        # ---- 1. 字符串状态机：先解析字符串，得到区间 ----
        # 1=单引号/双引号未闭合  2=""" 未闭合  3=''' 未闭合
        i = 0
        st = self.previousBlockState()
        if st < 0:
            st = 0                # 文档首块：previousBlockState() 返回 -1，按"无状态"处理
        string_ranges = []        # [(start, end), ...]
        if st == 2:
            # 上一行是 """ 开头，只找 """ 闭合
            j = text.find('"""')
            if j < 0:
                self.setFormat(0, n, F["string"])
                string_ranges.append((0, n))
                self.setCurrentBlockState(2)
                return
            string_ranges.append((0, j + 3))
            self.setFormat(0, j + 3, F["string"])
            i = j + 3
            st = 0
        elif st == 3:
            # 上一行是 ''' 开头，只找 ''' 闭合
            j = text.find("'''")
            if j < 0:
                self.setFormat(0, n, F["string"])
                string_ranges.append((0, n))
                self.setCurrentBlockState(3)
                return
            string_ranges.append((0, j + 3))
            self.setFormat(0, j + 3, F["string"])
            i = j + 3
            st = 0
        elif st == 1:
            # 单行引号未闭合，同时找 ' 和 "
            jq = text.find("'")
            jw = text.find('"')
            if jq < 0 and jw < 0:
                self.setFormat(0, n, F["string"])
                string_ranges.append((0, n))
                self.setCurrentBlockState(1)
                return
            j = min(x for x in (jq, jw) if x >= 0)
            string_ranges.append((0, j + 1))
            self.setFormat(0, j + 1, F["string"])
            i = j + 1
            st = 0
        while st == 0 and i < n:
            # 找下一个字符串起点
            m = None
            for q in ('"""', "'''", '"', "'"):
                j = text.find(q, i)
                if j >= 0 and (m is None or j < m[0]):
                    m = (j, q)
            if m is None:
                break
            j, q = m
            if q in ('"""', "'''"):
                k = text.find(q, j + 3)
                if k < 0:
                    self.setFormat(j, n - j, F["string"])
                    string_ranges.append((j, n))
                    self.setCurrentBlockState(3 if q == "'''" else 2)
                    return
                string_ranges.append((j, k + 3))
                self.setFormat(j, k - j + 3, F["string"])
                i = k + 3
            else:
                k = text.find(q, j + 1)
                if k < 0:
                    self.setFormat(j, n - j, F["string"])
                    string_ranges.append((j, n))
                    self.setCurrentBlockState(1)
                    return
                string_ranges.append((j, k + 1))
                self.setFormat(j, k - j + 1, F["string"])
                i = k + 1
        if st:
            self.setCurrentBlockState(st)
        # ---- 2. 标记字符串内位置（供注释/关键字判断跳过） ----
        in_str = [False] * n
        for s, e in string_ranges:
            for k in range(s, min(e, n)):
                in_str[k] = True
        # ---- 3. 找注释起点（跳过字符串内的 #） ----
        comment_start = -1
        for k in range(n):
            if not in_str[k] and text[k] == '#':
                comment_start = k
                break
        if comment_start >= 0:
            self.setFormat(comment_start, n - comment_start, F["comment"])
        # ---- 4. 数字/装饰器/标识符（只在"非字符串且非注释"区间匹配） ----
        # 有注释时截断搜索文本；无注释时保持全文。
        # re.finditer 返回的位置本身就是原 text 中的位置（截断从 0 开始）。
        search_text = text[:comment_start] if comment_start >= 0 else text
        for mm in re.finditer(r"\b\d[\d_]*(\.\d[\d_]*)?([eE][+-]?\d+)?\b", search_text):
            if all(not in_str[k] for k in range(mm.start(), mm.end())):
                self.setFormat(mm.start(), mm.end() - mm.start(), F["number"])
        for mm in re.finditer(r"@\w+", search_text):
            if all(not in_str[k] for k in range(mm.start(), mm.end())):
                self.setFormat(mm.start(), mm.end() - mm.start(), F["decorator"])
        for mm in re.finditer(r"\b[a-zA-Z_]\w*\b", search_text):
            if not all(not in_str[k] for k in range(mm.start(), mm.end())):
                continue
            w = mm.group()
            if w in _PY_KEYWORDS_:
                self.setFormat(mm.start(), mm.end() - mm.start(), F["kw"])
            elif w == "self":
                self.setFormat(mm.start(), mm.end() - mm.start(), F["self"])
            elif w in _PY_BUILTINS:
                self.setFormat(mm.start(), mm.end() - mm.start(), F["builtin"])
            elif mm.start() > 0:
                prev = text[:mm.start()].rstrip()
                if prev.endswith("def") or prev.endswith("class"):
                    self.setFormat(mm.start(), mm.end() - mm.start(), F["defname"])


class RuleHighlighter(QSyntaxHighlighter):
    """通用规则表高亮引擎（移植点④：数据驱动，对齐 PyQ/Syn 设计）。

    每种语言 = 一组 (正则, token, 选项) 规则，正则只编译一次；
    规则按序执行，后者 setFormat 覆盖前者（用于 注释/前缀 特判覆盖）。
    选项：start/end = 匹配区间的首尾偏移；words = 仅集合内词着色；
    prev = 匹配前要求行首或紧前非空白字符属于该集合（JSON key 特判）；
    marker = 注释起点标记规则：命中后把整段剩余涂成注释色（JS // 特判）。
    """

    def __init__(self, doc, rules):
        super().__init__(doc)
        self._rules = [(re.compile(p), t, o) for p, t, o in rules]

    def highlightBlock(self, text):
        for rx, token, o in self._rules:
            if o.get("marker"):
                i = text.find(rx.pattern)
                if i >= 0:
                    self.setFormat(i, len(text) - i,
                                   _fmt(token, italic=o.get("italic", False)))
                continue
            for mm in rx.finditer(text):
                words = o.get("words")
                if words and mm.group() not in words:
                    continue
                prev = o.get("prev")
                if prev is not None:
                    p = text[:mm.start()].rstrip()
                    if p and not p.endswith(prev):
                        continue
                s = mm.start() + o.get("start", 0)
                e = mm.end() + o.get("end", 0)
                if s < e:
                    self.setFormat(s, e - s,
                                   _fmt(token,
                                        bold=o.get("bold", False),
                                        italic=o.get("italic", False)))


# 每语言规则表（移植点④：从手写高亮器抽成数据，行为与旧实现逐一等价）
_JS_KWS = ("var let const function return if else for while do switch case "
           "break continue new class extends import export from default async "
           "await try catch finally throw").split()

_LANG_RULES = {
    "JSON": [
        # 字符串：默认 str 色；前非空白为 { [ , 或行首 → key(attr) 色（覆盖）
        (r'"(?:\\.|[^"\\])*"', "string", {}),
        (r'"(?:\\.|[^"\\])*"', "attr", {"prev": ("{", "[", ",")}),
        (r"\b\d+(\.\d+)?([eE][+-]?\d+)?\b", "number", {}),
    ],
    "HTML": [
        (r"<!--.*?-->", "comment", {"italic": True}),
        (r"</?[a-zA-Z][a-zA-Z0-9-]*", "tag", {}),
        (r"\s[a-zA-Z-]+=", "attr", {"start": 1, "end": -1}),
        (r'"[^"\n]*"|\'[^\'\n]*\'', "string", {}),
    ],
    "Markdown": [
        (r"^#{1,6}\s.*$", "heading", {"bold": True}),
        (r"`[^`]*`", "string", {}),
        (r"(^|(?<=\n))\s{4,}.*", "comment", {"italic": True}),
    ],
    "CSS": [
        (r"[.#]?[a-zA-Z-]+(?=\s*\{)", "defname", {}),
        (r"([a-zA-Z-]+)\s*:", "attr", {"end": -1}),
        (r":\s*([^;{]+);", "string", {"start": 1, "end": -1}),
    ],
    "JavaScript": [
        (r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`', "string", {}),
        (r"\b[a-zA-Z_$]\w*\b", "kw", {"words": set(_JS_KWS), "bold": True}),
        ("//", "comment", {"marker": True, "italic": True}),
    ],
}


def _make_rule_hl(lang):
    """按语言规则表生成高亮器工厂。"""
    rules = _LANG_RULES[lang]

    def factory(doc):
        return RuleHighlighter(doc, rules)

    return factory


JsonHighlighter = _make_rule_hl("JSON")
HtmlHighlighter = _make_rule_hl("HTML")
MdHighlighter = _make_rule_hl("Markdown")
CssHighlighter = _make_rule_hl("CSS")
JsHighlighter = _make_rule_hl("JavaScript")


_HL_MAP = {
    "Python": PyHighlighter,
    "JSON": JsonHighlighter,
    "HTML": HtmlHighlighter,
    "Markdown": MdHighlighter,
    "CSS": CssHighlighter,
    "JavaScript": JsHighlighter,
}


# --------------------------------------------------------------------------
# 行号区域（EditorEdit 的子控件，通过 viewport 边距留出左侧空间）
# --------------------------------------------------------------------------
class LineNumberArea(QWidget):
    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(e.rect(), QColor(BLACK.name()))
        p.setFont(self._edit.font())
        fm = self._edit.fontMetrics()
        # 当前行号用亮色（PyQ 移植点②：当前行行号变色）
        cur_block = self._edit.textCursor().blockNumber()
        p.setPen(QColor(GREEN_DARK.name()))
        block = self._edit.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self._edit.blockBoundingGeometry(block)
                    .translated(self._edit.contentOffset()).top())
        bottom = top + round(self._edit.blockBoundingRect(block).height())
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                if block.blockNumber() == cur_block:
                    p.setPen(QColor(GREEN.name()))
                else:
                    p.setPen(QColor(GREEN_DARK.name()))
                p.drawText(0, top, self.width() - 6, fm.height(),
                           int(Qt.AlignmentFlag.AlignRight), str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self._edit.blockBoundingRect(block).height())
            num += 1


# --------------------------------------------------------------------------
# 编辑器主体
# --------------------------------------------------------------------------
class EditorEdit(QPlainTextEdit):
    """QPlainTextEdit + 补全弹层 + 查找/保存/缩放快捷键。"""

    find_requested = pyqtSignal()
    save_requested = pyqtSignal()
    new_requested = pyqtSignal()
    goto_requested = pyqtSignal()
    find_next = pyqtSignal()
    find_prev = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._lang = "Plain Text"
        self._comp = None
        self._doc_names = set()
        self._var_types = {}
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(self._collect_doc_names)
        self.textChanged.connect(self._schedule_scan)
        self.setFont(mono(11))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        # 行号区：EditorEdit 的子控件，用 viewport 左边距留位（Qt 官方 CodeEditor 示例做法）
        self.linearea = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_area_width)
        self.updateRequest.connect(self._update_line_area)
        self._update_line_area_width(0)
        # 当前行高亮 + 括号配对（PyQ 移植点②）：统一在 _update_extra_selections
        # 里合并查找高亮，避免 setExtraSelections 互相覆盖
        self._find_sels = []
        self.cursorPositionChanged.connect(self._update_extra_selections)

    def set_find_sels(self, sels):
        """查找高亮选区由 EditorPage 传入，与当前行/括号选区合并显示。"""
        self._find_sels = sels
        self._update_extra_selections()

    def _update_extra_selections(self):
        """合并 当前行高亮 + 括号配对 + 查找高亮 三类选区（PyQ 移植点②）。"""
        sels = list(self._find_sels)
        cur = self.textCursor()
        # ---- 当前行高亮：光标所在行浅底色 ----
        line_sel = QTextEdit.ExtraSelection()
        line_fmt = QTextCharFormat()
        line_fmt.setBackground(QColor(_TOKEN_COLORS["curline"]))
        line_sel.format = line_fmt
        line_sel.cursor = QTextCursor(cur)
        line_sel.cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        sels.append(line_sel)
        # ---- 括号配对：光标前一字符是 )]}([{ 时高亮左右括号 ----
        blk = cur.block().text()
        pos_in = cur.positionInBlock()
        open_pairs = {"(": ")", "[": "]", "{": "}"}
        close_pairs = {")": "(", "]": "[", "}": "{"}
        ch = None
        side = None
        if pos_in > 0 and blk[pos_in - 1] in close_pairs:
            ch, side = blk[pos_in - 1], "open"      # 光标在闭括号右侧 → 找左配对
        elif pos_in < len(blk) and blk[pos_in] in open_pairs:
            ch, side = blk[pos_in], "close"         # 光标在开括号左侧 → 找右配对
        if ch is not None:
            pair = close_pairs.get(ch, open_pairs.get(ch))
            # 只在本行范围内扫描（PyQ 全文档逐字符开销大，限本行足够常见）
            depth = 0
            if side == "open":      # 往前找
                for k in range(pos_in - 1, -1, -1):
                    if blk[k] == ch:
                        depth += 1
                    elif blk[k] == pair:
                        if depth == 0:
                            self._add_paren_sel(sels, cur, k, pos_in - 1)
                            break
                        depth -= 1
            else:                   # 往后找
                for k in range(pos_in + 1, len(blk)):
                    if blk[k] == ch:
                        depth += 1
                    elif blk[k] == pair:
                        if depth == 0:
                            self._add_paren_sel(sels, cur, pos_in, k)
                            break
                        depth -= 1
        self.setExtraSelections(sels)

    def _add_paren_sel(self, sels, cur, a, b):
        fmt = _fmt("paren", bold=True)
        for p in (a, b):
            s = QTextEdit.ExtraSelection()
            s.format = fmt
            c = QTextCursor(cur)
            c.setPosition(cur.block().position() + p)
            c.setPosition(cur.block().position() + p + 1,
                          QTextCursor.MoveMode.KeepAnchor)
            s.cursor = c
            sels.append(s)

    # ---------- 行号区 ----------
    def _line_area_width(self):
        digits = max(1, len(str(self.blockCount())))
        return 14 + digits * self.fontMetrics().horizontalAdvance("0")

    def _update_line_area_width(self, _n):
        self.setViewportMargins(self._line_area_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy):
        if dy:
            self.linearea.scroll(0, dy)
        else:
            self.linearea.update(0, rect.y(), self.linearea.width(),
                                 rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_area_width(0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.linearea.setGeometry(cr.left(), cr.top(),
                                  self._line_area_width(), cr.height())

    # ---------- 补全 ----------
    def _schedule_scan(self):
        if self._lang == "Python":
            self._scan_timer.start(300)

    def _collect_doc_names(self):
        """扫描文档，收集可调用的名字。返回三类集合。"""
        txt = self.toPlainText()
        if len(txt) > 200_000:
            return
        # 先把多行字符串整体挖掉，防止三引号字符串里的 `x = 1` / `def foo` 污染
        # 名字表（单行字符串/注释里的行首赋值同样会被行锚定正则误扫，
        # 但影响小、且正则成本高，这里只处理最常见的三引号块）。
        txt = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', " ", txt)
        names = set()       # 普通名（def/class/import/赋值/参数/属性赋值）
        for m in re.finditer(r"^\s*(?:def|class)\s+(\w+)", txt, re.M):
            names.add(m.group(1))
        for m in re.finditer(r"^\s*(\w+)\s*=", txt, re.M):
            names.add(m.group(1))
        for m in re.finditer(r"^\s*import\s+(\w+)", txt, re.M):
            names.add(m.group(1))
        # from X import Y (可多别名)
        for m in re.finditer(
                r"^\s*from\s+\w+(?:\.\w+)*\s+import\s+"
                r"([a-zA-Z_][\w.,\s]*?)(?:\s*#.*)?$", txt, re.M):
            for token in m.group(1).split(","):
                t = token.strip()
                # 处理 `foo as bar` —— 收集别名 bar
                if " as " in t:
                    t = t.split(" as ", 1)[1].strip()
                if t and not t.startswith("("):
                    names.add(t)
        # 函数参数：def foo(a, b, *c, d=1, **e):
        for m in re.finditer(r"def\s+\w+\s*\(([^)]*)\)", txt):
            for tok in m.group(1).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                tok = tok.lstrip("*")           # *a / **a → a
                tok = tok.split("=", 1)[0].strip()  # a=1 → a
                if tok and re.match(r"^[a-zA-Z_]\w*$", tok):
                    names.add(tok)
        # 类方法：class Foo: 下 def bar(self, ...) —— 已含在 def 里
        self._doc_names = names
        # 变量名 → 类型的粗略推断（用于 `x.` 属性补全）
        types = {}
        # 1) x = <内置类型/构造器>(...)  —— x = [] / {} / () / set() / tuple() ...
        for m in re.finditer(r"^\s*([a-zA-Z_]\w*)\s*=\s*([\w\[\]{}()]+)\s*(?:#.*)?$",
                             txt, re.M):
            var, rhs = m.group(1), m.group(2).strip()
            t = None
            if rhs == "[]":
                t = "list"
            elif rhs in ("{}", "dict()"):
                t = "dict"
            elif rhs in ("()", "tuple()"):
                t = "tuple"
            elif rhs == "set()":
                t = "set"
            elif rhs in _TYPE_ATTRS:
                t = rhs
            if t:
                types[var] = t
        # 2) x = <已知 import 的模块>.<函数>(...)  —— x = open(...)
        for m in re.finditer(r"^\s*([a-zA-Z_]\w*)\s*=\s*([\w.]+)\s*\(", txt, re.M):
            var, rhs = m.group(1), m.group(2).strip()
            if rhs == "open":
                types[var] = "file"
            elif rhs == "print":
                types[var] = "print"
        self._var_types = types

    def _word_before_cursor(self):
        cur = self.textCursor()
        pos = cur.positionInBlock()
        text = cur.block().text()
        i = pos
        while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
            i -= 1
        return text[i:pos], cur.position() - (pos - i)

    def _context_before_cursor(self):
        """探测光标前的上下文，返回 (kind, base) 之一：
        - ("attr", "obj_name")  光标前是 `obj.` 或 `obj.prefix`（属性补全）
        - ("kwfrom", "mod")     光标前是 `from <mod> import ` → 模块成员补全
        - ("kwimport", "")      光标前是 `import ` → 模块名补全
        - ("kwfrommod", "mod")  光标前是 `from <mod> ` → 模块名补全
        - ("plain", "")         默认：普通标识符前缀补全
        """
        cur = self.textCursor()
        pos = cur.positionInBlock()
        text = cur.block().text()[:pos]
        trimmed = text.rstrip()
        # 光标位于 `#` 注释内 → 抑制补全（VS Code 也不在注释里弹）。
        # 粗略判定：最后一个 # 之后没有字符串引号（三引号内的 # 会误判为注释，
        # 但注释抑制本身就是保守行为，可接受）。
        if "#" in text:
            h = text.rfind("#")
            q = max(text.rfind('"'), text.rfind("'"))
            if q < h:
                return ("comment", "")
        # `obj.` 上下文（属性补全，刚打完点）
        if trimmed.endswith("."):
            body = trimmed[:-1].rstrip()
            if re.match(r"^[a-zA-Z_][\w.]*$", body):
                return ("attr", body)
        # `obj.前缀` 上下文（属性补全，已打了几个字符）
        m = re.search(r"([a-zA-Z_][\w.]*)\.([a-zA-Z_]\w*)$", trimmed)
        if m:
            return ("attr", m.group(1))
        # `import <prefix>` 上下文
        m = re.search(r"\bimport\s+([a-zA-Z_][\w.]*)$", text)
        if m:
            return ("kwimport", "")
        # `from <mod> import <prefix>`
        m = re.search(r"\bfrom\s+([a-zA-Z_][\w.]*)\s+import\s+([a-zA-Z_][\w.]*)$", text)
        if m:
            return ("kwfrom", m.group(1))
        # `from <mod> `（还没打到 import）
        m = re.search(r"\bfrom\s+([a-zA-Z_][\w.]*)$", text)
        if m:
            return ("kwfrommod", m.group(1))
        return ("plain", "")

    def _show_completion(self):
        if self._lang != "Python":
            return
        prefix, start = self._word_before_cursor()
        pl = prefix.lower()          # VS Code 风格：大小写不敏感前缀匹配
        ctx, base = self._context_before_cursor()
        if ctx == "comment":
            self._hide_completion()  # 注释内不弹补全
            return
        cands = []
        if ctx == "attr":
            # 属性补全：`os.` / `my_list.` / `my_dict.` 等
            if len(prefix) >= 1:
                # 先试变量类型推断
                simple = base.split(".")[-1]
                t = self._var_types.get(simple)
                if t and t in _TYPE_ATTRS:
                    for a in _TYPE_ATTRS[t]:
                        if a.lower().startswith(pl):
                            cands.append((a, "attr"))
                # 再试模块别名（如 `os.` → os 的顶层名）
                if base in _MOD_ALIASES:
                    for a in _MOD_ALIASES[base]:
                        if a.lower().startswith(pl):
                            cands.append((a, "attr"))
                elif base == "os.path" and "path" in _MOD_ALIASES["os.path"]:
                    for a in _MOD_ALIASES["os.path"]:
                        if a.lower().startswith(pl):
                            cands.append((a, "attr"))
                # 兜底：所有已 import 的模块名作为属性候选
                if not cands:
                    for n in sorted(self._doc_names):
                        if n.lower().startswith(pl):
                            cands.append((n, "attr"))
            else:
                # 刚打完点，先给常用属性列表（list/dict/str 等）
                simple = base.split(".")[-1]
                t = self._var_types.get(simple)
                pool = _TYPE_ATTRS.get(t, ()) if t else ()
                if not pool and base in _MOD_ALIASES:
                    pool = _MOD_ALIASES[base]
                for a in pool[:12]:
                    cands.append((a, "attr"))
        elif ctx == "kwfrom":
            # `from <mod> import <prefix>` —— 给模块成员候选
            mod = base
            if mod in _MOD_ALIASES:
                pool = _MOD_ALIASES[mod]
            else:
                pool = ()
            for a in pool:
                if a.lower().startswith(pl):
                    cands.append((a, "imp"))
            # 兜底：文档已定义的名字
            for n in sorted(self._doc_names):
                if n.lower().startswith(pl):
                    cands.append((n, "imp"))
        elif ctx == "kwimport" or ctx == "kwfrommod":
            # `import <prefix>` 或 `from <prefix>` —— 给模块名候选
            for m in _PY_MODULES:
                if m.lower().startswith(pl):
                    cands.append((m, "mod"))
            for n in sorted(self._doc_names):
                if n.lower().startswith(pl):
                    cands.append((n, "mod"))
        else:
            # 普通标识符前缀补全（VS Code 风格：内置/关键字/文档名优先，模块名靠后）
            for w in _PY_BUILTINS:
                if w.lower().startswith(pl) and w.lower() != pl:
                    cands.append((w, "bi"))
            for w in sorted(_PY_KEYWORDS_):
                if w.lower().startswith(pl) and w.lower() != pl:
                    cands.append((w, "kw"))
            for w in sorted(self._doc_names):
                if w.lower().startswith(pl) and w.lower() != pl:
                    cands.append((w, "doc"))
            # 模块名只在前缀 >= 2 个字符时提示 import（避免单个字母刷屏）
            if len(pl) >= 2:
                for m in _PY_MODULES:
                    if m.lower().startswith(pl) and m.lower() != pl:
                        cands.append((m, "mod"))
        # 去重（保留首次出现的 kind）
        seen = set()
        dedup = []
        for w, k in cands:
            if w in seen:
                continue
            seen.add(w)
            dedup.append((w, k))
        # 排序：kind 优先级 bi > kw > doc > mod；内置里常用(_PY_HOT)再提前；
        # attr/imp（模块属性/成员）保持 _MOD_ALIASES 定义顺序（按常用度排，
        # 如 os. 的 path 应排第一）——按字母会把它挤到第 7 位，窄窗口截断时丢失；
        # 其余 kind 同级按字母。
        kind_rank = {"bi": 0, "kw": 1, "doc": 2, "mod": 3}
        def _sk(wk):
            w, k = wk
            r = kind_rank.get(k, 9)
            h = 0 if (k == "bi" and w in _PY_HOT) else 1
            if k in ("attr", "imp"):
                return (r, h, 0, dedup.index(wk))
            return (r, h, 1, w)
        cands = sorted(dedup[:30], key=_sk)[:8]
        if not cands:
            self._hide_completion()
            return
        if self._comp is None:
            self._comp = QListWidget(self)
            # 关键：不能用 Qt.Popup —— Popup 类型窗口 show() 时会 grabbing 键盘焦点，
            # 导致后续按键进弹层而非编辑器（输入卡住、Tab 无效）。
            # Tool + Frameless + WindowDoesNotAcceptFocus：不抢焦点、不进任务栏，
            # 编辑器始终保持键盘焦点，Tab/Enter/上下键由 keyPressEvent 统一处理。
            self._comp.setWindowFlags(Qt.WindowType.Tool
                                      | Qt.WindowType.FramelessWindowHint
                                      | Qt.WindowType.WindowDoesNotAcceptFocus)
            self._comp.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self._comp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            # 终端补全风格：竖排紧凑列表（对齐 term.py _sug_paint——提示符
            # 下方一行一个候选、整行选中高亮），半透明深色面板 + 细边框，
            # 宽度贴合最长候选、高度贴合候选数，绝不横向铺开。
            self._comp.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._comp.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._comp.setFont(mono(9))
            self._comp.setStyleSheet(
                f"QListWidget {{ background: rgba({PANEL_BG.red()},"
                f"{PANEL_BG.green()},{PANEL_BG.blue()},215);"
                f" color: {GREEN.name()};"
                f" border: 1px solid rgba({GREEN_DARK.red()},"
                f"{GREEN_DARK.green()},{GREEN_DARK.blue()},180);"
                f" outline: 0; }}"
                f"QListWidget::item {{ padding: 0px 4px; height: 18px; }}"
                f"QListWidget::item:selected {{ background: rgba({GREEN_DARK.red()},"
                f"{GREEN_DARK.green()},{GREEN_DARK.blue()},235);"
                f" color: #ffffff; }}")
            self._comp.itemClicked.connect(lambda it: self._apply_completion(it))
        self._comp.clear()
        fm = self._comp.fontMetrics()
        for w, kind in cands:
            label = w
            if kind == "mod":
                label = f"⚙ {w}"
            elif kind == "imp":
                label = f"◦ {w}"
            elif kind == "attr":
                label = f"· {w}"
            it = QListWidgetItem(label)
            color = _COMP_KIND_COLORS.get(kind)
            if color is not None:
                it.setForeground(QColor(color))
            it.setData(Qt.ItemDataRole.UserRole, (w, start, kind))
            self._comp.addItem(it)
        self._comp.setCurrentRow(0)
        cr = self.cursorRect()
        # 竖排紧凑：先量最长候选定列宽，再给每个 item 统一宽高（整行高亮）
        self._comp.doItemsLayout()
        fm = self._comp.fontMetrics()
        w_max = 0
        for i in range(self._comp.count()):
            adv = fm.horizontalAdvance(self._comp.item(i).text())
            if adv > w_max:
                w_max = adv
        w_max = int(w_max) + 24          # 文本 + 图标间隙 + 左右 padding/边框
        for i in range(self._comp.count()):
            self._comp.item(i).setSizeHint(QSize(w_max, 18))
        self._comp.doItemsLayout()
        h_ = self._comp.count() * 18 + 6
        self._comp.setFixedWidth(int(w_max + 6))
        self._comp.setFixedHeight(int(h_))
        gp = self.viewport().mapToGlobal(cr.bottomLeft() + QPoint(0, 4))
        # 光标在末行时弹层会超出屏幕底部 → 上移到光标上方（对齐 VS Code 行为）
        scr = self.screen().availableGeometry()
        if gp.y() + self._comp.height() > scr.bottom():
            gp = self.viewport().mapToGlobal(cr.topLeft() + QPoint(0, -4))
            gp.setY(max(scr.top(), gp.y() - self._comp.height()))
        self._comp.move(gp.x(), gp.y())
        self._comp.show()
        self._comp.raise_()
        # 弹层为 Tool + NoFocus + WindowDoesNotAcceptFocus，show() 不会抢焦点，
        # 编辑器自然保持键盘焦点，输入/快捷键继续进编辑器。
        # 不调用 self.setFocus() —— 弹层出现时若强制 setFocus 会破坏输入法状态。

    def _hide_completion(self):
        if self._comp is not None and self._comp.isVisible():
            self._comp.hide()

    def _apply_completion(self, item):
        _w, _start, kind = item.data(Qt.ItemDataRole.UserRole)
        prefix, start2 = self._word_before_cursor()
        ctx, base = self._context_before_cursor()
        cur = self.textCursor()
        cur.beginEditBlock()
        # ---------- 模块名补全（"⚙ xxx"）：在文件顶部插入 `import xxx` ----------
        if kind == "mod" and ctx == "plain":
            self._insert_import(_w)
            cur.endEditBlock()
            self._hide_completion()
            return
        # ---------- `from <mod> import <name>` 补全 ----------
        if ctx == "kwfrom":
            # 直接把当前前缀替换成完整属性名
            cur.setPosition(start2)
            cur.setPosition(start2 + len(prefix), QTextCursor.MoveMode.KeepAnchor)
            cur.insertText(_w)
            cur.endEditBlock()
            self._hide_completion()
            return
        # ---------- `import <name>` 或 `from <name>` 补全 ----------
        if ctx in ("kwimport", "kwfrommod"):
            cur.setPosition(start2)
            cur.setPosition(start2 + len(prefix), QTextCursor.MoveMode.KeepAnchor)
            cur.insertText(_w)
            if ctx == "kwimport":
                cur.insertText("\n")
            cur.endEditBlock()
            self._hide_completion()
            return
        # ---------- 属性补全 / 关键字 / 普通名 ----------
        cur.setPosition(start2)
        cur.setPosition(start2 + len(prefix), QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(_w)
        if kind == "kw":
            # VS Code 风格：关键字补全后自动补一个空格（re/import→"re "）
            # 行尾也补；仅当光标后已是空格时不补。
            c2 = self.textCursor()
            p2 = c2.position()
            blk = c2.block().text()
            off = p2 - c2.block().position()
            if off >= len(blk) or blk[off] != " ":
                c2.setPosition(p2)
                c2.insertText(" ")
        cur.endEditBlock()
        self._hide_completion()

    def _insert_import(self, mod_name):
        """在文件顶部已 import 块之后插入 `import <mod_name>`（去重）。"""
        cur = self.textCursor()
        cur.beginEditBlock()
        # 去重扫描全文档（不只 firstBlock：import 常出现在第 2+ 行）
        doc = self.document()
        b = doc.firstBlock()
        target_pos = 0
        blank_seen = 0
        # 第一遍：确认未 import 过 mod_name
        while b.isValid():
            t = b.text()
            # 只匹配"独立 import 语句"：import os 命中；import os.path（os 后是 .）、
            # import osx（os 后是字母）都不误判为正已 import os
            if re.search(rf"^\s*import\s+(?:([\w.]+)\s*,\s*)*{re.escape(mod_name)}(?=[\s,]|$)", t):
                cur.endEditBlock()
                return
            b = b.next()
        # 第二遍：找顶部第一个非 import/blank/comment 的语句行
        b = doc.firstBlock()
        while b.isValid():
            t = b.text().strip()
            if not t or t.startswith("#") or t.startswith(("import ", "from ", "encoding:")):
                blank_seen += 1
                b = b.next()
                target_pos = b.position() if b.isValid() else target_pos
                continue
            break
        # 在扫描到的位置插入新行（若前面已有 import 块，直接紧跟其后）
        if blank_seen == 0:
            # 完全没 import 块 —— 在文件最前面插入
            target_pos = 0
        # 在目标行首插入 "import <mod>\n"：
        # 前不带 \n（避免与上一行/代码行之间多出空行），尾部带 \n 把代码行顶下去，
        # 否则 "\nimport os" 会粘在首行代码后面（"import osprint(1)"）。
        c = QTextCursor(doc)
        c.setPosition(target_pos)
        c.insertText("import " + mod_name + "\n")
        cur.endEditBlock()

    # ---------- 键盘 ----------
    def keyPressEvent(self, e):
        # 补全弹层打开时：上下/回车/Tab/Esc 交给弹层
        if self._comp is not None and self._comp.isVisible():
            k = e.key()
            if k in (Qt.Key.Key_Up, Qt.Key.Key_Down,
                     Qt.Key.Key_Return, Qt.Key.Key_Enter,
                     Qt.Key.Key_Tab, Qt.Key.Key_Escape):
                if k == Qt.Key.Key_Up:
                    self._comp.setCurrentRow(
                        (self._comp.currentRow() - 1) % self._comp.count())
                    e.accept(); return
                if k == Qt.Key.Key_Down:
                    self._comp.setCurrentRow(
                        (self._comp.currentRow() + 1) % self._comp.count())
                    e.accept(); return
                if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                    it = self._comp.currentItem()
                    if it is not None:
                        self._apply_completion(it)
                    else:
                        self._hide_completion()
                    e.accept(); return
                if k == Qt.Key.Key_Escape:
                    self._hide_completion()
                    e.accept(); return
        mod = e.modifiers()
        has_ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)
        k = e.key()
        if has_ctrl and k == Qt.Key.Key_Space:
            self._show_completion()
            e.accept(); return
        # 自动配对括号/引号（PyQ 移植点③）：仅无修饰键的普通字符输入
        if (not has_ctrl
                and not (mod & Qt.KeyboardModifier.AltModifier)
                and len(e.text()) == 1 and e.text().isprintable()
                and self._lang == "Python"):
            ch = e.text()
            if ch in ("(", "[", "{", ")", "]", "}", "'", '"'):
                cur = self.textCursor()
                pos_in = cur.positionInBlock()
                blk = cur.block().text()
                after = blk[pos_in:pos_in + 1] if pos_in < len(blk) else ""
                self._hide_completion()
                if ch in ("(", "[", "{"):
                    # 开括号：插一对，光标留在中间
                    cur.beginEditBlock()
                    cur.insertText(ch + {"(": ")", "[": "]", "{": "}"}[ch])
                    cur.movePosition(QTextCursor.MoveOperation.Left)
                    cur.endEditBlock()
                    self.setTextCursor(cur)
                    e.accept(); return
                if ch in (")", "]", "}"):
                    # 闭括号：右侧已有配对则智能跳过（光标右移一位）
                    if after == ch:
                        cur.movePosition(QTextCursor.MoveOperation.Right)
                        self.setTextCursor(cur)
                        e.accept(); return
                elif ch in ("'", '"'):
                    # 引号：右侧已有相同引号则跳过；否则插一对（光标在中间）
                    if after == ch:
                        cur.movePosition(QTextCursor.MoveOperation.Right)
                        self.setTextCursor(cur)
                        e.accept(); return
                    # 左侧是标识符时不配对（`name'` 场景多为拼接/闭包，保持原样）
                    if pos_in > 0 and blk[pos_in - 1].isalnum():
                        pass
                    else:
                        cur.beginEditBlock()
                        cur.insertText(ch + ch)
                        cur.movePosition(QTextCursor.MoveOperation.Left)
                        cur.endEditBlock()
                        self.setTextCursor(cur)
                        e.accept(); return
        # 自动缩进：Enter 后继承上一行前导空白（VS Code 行为）
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not has_ctrl:
            cur = self.textCursor()
            cur.beginEditBlock()
            prev = cur.block().previous()
            prev_text = prev.text() if prev.isValid() else ""
            # { 结尾 → 扩行补 }（PyQ 移植点③：`{⏎}` 自动缩进 + 补右花括号）
            if prev_text.rstrip().endswith("{"):
                lead = prev_text[:len(prev_text) - len(prev_text.lstrip())]
                cur.insertBlock()                 # 中间行
                cur.insertText(lead + "    ")     # 进一层缩进
                mid_end = cur.position()          # 记录中间行末尾
                cur.insertBlock()                 # 补 } 行
                cur.insertText(lead + "}")
                cur.setPosition(mid_end)          # 光标真正回到中间行末尾
                cur.endEditBlock()
                e.accept(); return
            cur.insertBlock()
            # 找当前行行首的前导空白
            pos_in_blk = cur.positionInBlock()
            off = 0
            # 用 block 内容取前导空白
            block_text = cur.block().text()
            # 光标现在应在 block 开头（positionInBlock()==0）
            lead = prev_text
            i = 0
            while i < len(lead) and lead[i] in (" ", "\t"):
                i += 1
            indent = lead[:i]
            if indent:
                cur.insertText(indent)
            cur.endEditBlock()
            e.accept(); return
        if has_ctrl and k == Qt.Key.Key_F:
            self.find_requested.emit()
            e.accept(); return
        if has_ctrl and k == Qt.Key.Key_S:
            self.save_requested.emit()
            e.accept(); return
        if has_ctrl and k == Qt.Key.Key_N:
            self.new_requested.emit()
            e.accept(); return
        if has_ctrl and k == Qt.Key.Key_G:
            self.goto_requested.emit()
            e.accept(); return
        if has_ctrl and k in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.zoomIn(1); e.accept(); return
        if has_ctrl and k == Qt.Key.Key_Minus:
            self.zoomOut(1); e.accept(); return
        if has_ctrl and k == Qt.Key.Key_0:
            self.setFont(mono(11)); e.accept(); return
        if k == Qt.Key.Key_F3:
            if mod & Qt.KeyboardModifier.ShiftModifier:
                self.find_prev.emit()
            else:
                self.find_next.emit()
            e.accept(); return
        super().keyPressEvent(e)
        txt = e.text()
        if not txt or not (txt[-1].isalnum() or txt[-1] == "_"):
            # 非标识符按键（空格/括号/运算符）或光标移动（方向键 e.text() 为空）：
            # 候选已过时，收起弹层，避免 Enter 误应用过期候选 / 弹层停在旧光标处
            self._hide_completion()
            return
        # 输入标识符后弹出联想
        self._show_completion()

    def focusOutEvent(self, e):
        # 弹层是 Tool + NoFocus + WindowDoesNotAcceptFocus，show() 不会真正
        # 拿走编辑器焦点；但窗口系统仍会派发一次 focusOut（Windows 上常见
        # ActiveWindowFocusReason / OtherFocusReason）。若在此直接收起弹层，
        # 用户会看到联想一闪就没（"什么都没有"）。
        # 正确做法：延后一帧检查焦点真实去向 —— 焦点回到编辑器（或弹层
        # show 的临时转移）则保留弹层；焦点确实去了别的控件才收起。
        comp_open = self._comp is not None and self._comp.isVisible()
        if not comp_open:
            super().focusOutEvent(e)
            return
        QTimer.singleShot(0, self._settle_completion_after_focus_out)
        super().focusOutEvent(e)

    def _settle_completion_after_focus_out(self):
        if self._comp is None or not self._comp.isVisible():
            return
        fw = QApplication.focusWidget()
        self._settle_tries = getattr(self, "_settle_tries", 0) + 1
        if fw is self or fw is self.viewport() or fw is self._comp:
            return  # 焦点仍在编辑器/弹层 → 保留
        if fw is None and self._settle_tries < 10:
            # 焦点尚未落定（弹层 show 的临时窗口系统转移）→ 再等一帧；
            # 限 10 次，窗口失活（Alt-Tab 后 focusWidget() 持续 None）
            # 也不会无限自续空转
            QTimer.singleShot(0, self._settle_completion_after_focus_out)
            return
        self._settle_tries = 0
        self._hide_completion()  # 焦点去了别的控件（点了别处/切换窗口）→ 收起

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            if delta > 0:
                self.zoomIn(1)
            elif delta < 0:
                self.zoomOut(1)
            e.accept()
            return
        super().wheelEvent(e)

    def mousePressEvent(self, e):
        # 点击编辑区（弹层外部）→ 收起补全弹层（Qt.Tool 窗口不像
        # Qt.Popup 会自动关闭，需要手动 hide；点击弹层项走弹层自己的 itemClicked）
        if self._comp is not None and self._comp.isVisible():
            self._hide_completion()
        super().mousePressEvent(e)


class _FindLine(QLineEdit):
    """查找输入框：Shift+Enter = 上一个，Esc = 关闭查找条。
    Ctrl+F/S/N/G / Ctrl+滚轮等快捷键在查找框内仍转发给编辑区。"""
    prev = pyqtSignal()
    cancel = pyqtSignal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.prev.emit()
            e.accept()
            return
        if e.key() == Qt.Key.Key_Escape:
            self.cancel.emit()
            e.accept()
            return
        mod = e.modifiers()
        if mod & Qt.KeyboardModifier.ControlModifier:
            ed = getattr(self, "_edit", None)
            if ed is not None and e.key() in (
                    Qt.Key.Key_S, Qt.Key.Key_N, Qt.Key.Key_G,
                    Qt.Key.Key_Equal, Qt.Key.Key_Plus, Qt.Key.Key_Minus,
                    Qt.Key.Key_0):
                ed.keyPressEvent(e)
                e.accept()
                return
        super().keyPressEvent(e)


class EditorPage(QWidget):
    """自包含编辑页：工具栏 + 编辑区(行号+编辑) + 查找条 + 状态栏。"""

    dirty_changed = pyqtSignal(bool)
    close_requested = pyqtSignal()
    new_requested = pyqtSignal()
    title_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.path = None
        self._title = "untitled"
        self._lang = "Plain Text"
        self._dirty = False
        self._encoding = "utf-8"
        self._low = None
        self._loading = False
        self._just_loaded = False
        self._readonly_fail = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 工具栏 ----
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        bar.setSpacing(6)
        self.btn_new = QPushButton("NEW")
        self.btn_save = QPushButton("SAVE")
        self.btn_find = QPushButton("FIND")
        self.btn_close = QPushButton("✕")
        for b in (self.btn_new, self.btn_save, self.btn_find, self.btn_close):
            b.setFixedHeight(22)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 点击不抢编辑区焦点
            b.setStyleSheet(self._btn_style(GREEN_DIM))
            bar.addWidget(b)
        self.btn_new.clicked.connect(self.new_requested.emit)
        self.btn_save.clicked.connect(self._save_clicked)
        self.btn_find.clicked.connect(self._find_toggle)
        self.btn_close.clicked.connect(self.close_requested.emit)
        bar.addStretch(1)
        self.path_lbl = QLabel(self._title)
        bar.addWidget(self.path_lbl, 1)
        self.lang_lbl = QLabel("")
        bar.addWidget(self.lang_lbl)
        root.addLayout(bar)

        # ---- 编辑区（行号区是 EditorEdit 的子控件，随 viewport 边距绘制）----
        editrow = QHBoxLayout()
        editrow.setContentsMargins(0, 0, 0, 0)
        editrow.setSpacing(0)
        self.edit = EditorEdit()
        self.edit._lang = self._lang
        self.linearea = self.edit.linearea
        editrow.addWidget(self.edit, 1)
        root.addLayout(editrow, 1)

        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.cursorPositionChanged.connect(self._update_status)
        self.edit.find_requested.connect(self._find_toggle)
        self.edit.save_requested.connect(self.save)
        self.edit.new_requested.connect(self.new_requested.emit)
        self.edit.goto_requested.connect(self.goto_line)
        self.edit.find_next.connect(self._find_next)
        self.edit.find_prev.connect(self._find_prev)

        # ---- 查找条 ----
        self.findbar = QWidget()
        fl = QHBoxLayout(self.findbar)
        fl.setContentsMargins(6, 2, 6, 2)
        fl.setSpacing(6)
        self.find_input = _FindLine()
        self.find_input._edit = self.edit
        self.find_input.setPlaceholderText(
            "find…  (Enter 下一个 / Shift+Enter 上一个 / Esc 关闭)")
        self.find_input.setFixedHeight(22)
        self.find_input.textChanged.connect(self._find_highlight)
        self.find_input.returnPressed.connect(self._find_next)
        self.find_input.prev.connect(self._find_prev)
        self.find_input.cancel.connect(self._find_close)
        fl.addWidget(self.find_input, 1)
        self.find_cnt = QLabel("")
        self.find_cnt.setStyleSheet(
            f"color: {GREEN_FAINT.name()}; font-size: 10px;")
        fl.addWidget(self.find_cnt)
        b_fprev = QPushButton("▲"); b_fnext = QPushButton("▼")
        for b in (b_fprev, b_fnext):
            b.setFixedSize(24, 22)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setStyleSheet(self._btn_style(GREEN_DIM))
        b_fprev.clicked.connect(self._find_prev)
        b_fnext.clicked.connect(self._find_next)
        fl.addWidget(b_fprev); fl.addWidget(b_fnext)
        b_fesc = QPushButton("✕"); b_fesc.setFixedSize(24, 22)
        b_fesc.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b_fesc.setStyleSheet(self._btn_style(GREEN_DIM))
        b_fesc.clicked.connect(self._find_close)
        fl.addWidget(b_fesc)
        self.findbar.hide()
        root.addWidget(self.findbar)

        # ---- 状态栏 ----
        self.status_lbl = QLabel("")
        self.status_lbl.setFixedHeight(20)
        root.addWidget(self.status_lbl)

        # 自动保存（修改后 800ms）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save)

        self.retint()
        hub.changed.connect(lambda _n: self.retint())
        self._update_status()

    # ---------- 样式 ----------
    @staticmethod
    def _btn_style(color):
        return (f"QPushButton {{ color: {color.name()}; background: transparent;"
                f" border: 1px solid {GREEN_DARK.name()}; font-size: 10px;"
                f" padding: 0 8px; }}"
                f"QPushButton:hover {{ background: {GREEN_DARK.name()};"
                f" color: #ffffff; }}")

    def retint(self):
        self.path_lbl.setStyleSheet(
            f"color: {GREEN.name()}; font-size: 11px;")
        self.lang_lbl.setStyleSheet(
            f"color: {AMBER.name()}; font-size: 10px;")
        self.edit.setStyleSheet(
            f"QPlainTextEdit {{ background: {BLACK.name()};"
            f" color: {GREEN.name()}; border: none; }}"
            f"QPlainTextEdit::selection {{ background: {GREEN_DARK.name()}; }}")
        self.find_input.setStyleSheet(
            f"QLineEdit {{ background: {BLACK.name()}; color: {GREEN.name()};"
            f" border: 1px solid {GREEN_DARK.name()}; padding: 2px 6px; }}")
        self.status_lbl.setStyleSheet(
            f"color: {GREEN_FAINT.name()}; font-size: 10px;"
            f" border-top: 1px solid {GREEN_DARK.name()}; padding: 2px 6px;")
        self.linearea.update()

    def tab_title(self):
        return f"{self._title}{' ●' if self._dirty else ''}"

    # ---------- 打开 / 保存 ----------
    def open(self, path):
        """打开文件。成功返回 True；失败在编辑区显示错误并返回 False。"""
        self.path = os.path.abspath(path)
        self._title = os.path.basename(self.path)
        self._readonly_fail = False   # 打开失败后禁止编辑/自动保存（防覆盖原文件）
        try:
            size = os.path.getsize(self.path)
        except OSError as e:
            self.edit.setPlainText(f"# 无法读取: {e}")
            self._readonly_fail = True
            self._set_dirty(False)
            return False
        if size > MAX_BYTES:
            self.edit.setPlainText(
                f"# 文件过大 ({size / 1024 / 1024:.1f} MB > 1.5 MB)，拒绝打开")
            self._readonly_fail = True
            self._set_dirty(False)
            return False
        try:
            raw = open(self.path, "rb").read()
        except OSError as e:
            self.edit.setPlainText(f"# 无法打开: {e}")
            self._readonly_fail = True
            self._set_dirty(False)
            return False
        if _BIN_CHUNK in raw[:8192]:
            self.edit.setPlainText("# 二进制文件，不支持编辑。")
            self._readonly_fail = True
            self._set_dirty(False)
            return False
        text, enc = self._decode(raw)
        self._encoding = enc
        self._loading = True
        self.edit.setPlainText(text)
        self._set_lang(_lang_of(self._title))
        self._loading = False
        # _set_dirty(False) 放在 _loading=False 之后：
        # QSyntaxHighlighter 的 rehighlight 可能在事件循环里延迟触发
        # 一次 textChanged（markContentsDirty → contentsChange），
        # 那次 textChanged 落在 _loading=False 之后 → _on_text_changed 会标 dirty=True。
        # 用 _just_loaded 标志压制这一次延迟信号。
        self._just_loaded = True
        self._set_dirty(False)
        self._update_status()
        return True

    @staticmethod
    def _decode(raw):
        for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc), (enc if enc != "utf-8-sig" else "utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8"

    def _set_lang(self, lang):
        self._lang = lang
        self.edit._lang = lang
        self.lang_lbl.setText(lang)
        if self._low is not None:
            # 旧高亮器仍被 document 父对象保活：必须先从 document 卸下再重建，
            # 否则切到无高亮语言后旧高亮依旧生效（如 .py→.txt 后 Python 高亮残留）
            try:
                self._low.setDocument(None)
            except RuntimeError:
                pass   # C++ 对象已销毁
            self._low = None
        hl = _HL_MAP.get(lang)
        if hl is not None:
            self._low = hl(self.edit.document())

    def new_document(self, name):
        """新建未命名文档。"""
        self.path = None
        self._title = name or "untitled"
        self._readonly_fail = False
        self._loading = True
        self.edit.clear()
        self._set_lang(_lang_of(self._title))
        self._set_dirty(True)
        self._loading = False
        self._update_status()

    def save(self, path=None):
        """保存（自动保存 / Ctrl+S / 保存为）。返回目标路径或 None。"""
        if path is None:
            path = self.path
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save as", self._title or "untitled.txt",
                "Text (*.txt);;Python (*.py);;All (*.*)")
            if not path:
                return None
            if "." not in os.path.basename(path):
                path += ".txt"
            self.path = os.path.abspath(path)
            self._title = os.path.basename(self.path)
            self._set_lang(_lang_of(self._title))
        if getattr(self, "_readonly_fail", False):
            # 打开失败后的错误信息区：拒绝保存，防止覆盖原文件
            self.status_lbl.setText("OPEN FAILED: read-only")
            return None
        try:
            with open(self.path, "w", encoding="utf-8", newline="") as f:
                f.write(self.edit.toPlainText())
        except OSError as e:
            self.status_lbl.setText(f"SAVE FAILED: {e}")
            return None
        self._set_dirty(False)
        self.status_lbl.setText(
            f"saved {time.strftime('%H:%M:%S')}  "
            f"{_fmt_size(os.path.getsize(self.path))}")
        return self.path

    # ---------- 事件 ----------
    def _on_text_changed(self):
        if self._loading:
            return
        if getattr(self, "_just_loaded", False):
            self._just_loaded = False
            return
        if getattr(self, "_readonly_fail", False):
            # 打开失败：编辑区是错误信息，禁止标脏/自动保存，防覆盖原文件
            return
        if not self._dirty:
            self._set_dirty(True)
        if self.path:                       # 有真实路径才自动保存
            self._save_timer.start(800)

    def _set_dirty(self, v):
        self._dirty = bool(v)
        self.dirty_changed.emit(self._dirty)
        self.title_changed.emit(self.tab_title())
        self._update_status()

    def _save_clicked(self):
        if self._dirty:
            self.save()
        else:
            self.status_lbl.setText("nothing to save")

    def _update_status(self):
        if not hasattr(self, "edit"):
            return
        c = self.edit.textCursor()
        ln = c.blockNumber() + 1
        col = c.positionInBlock() + 1
        # getsize 是磁盘 stat，光标每次移动都会走到这里 → 缓存 1 秒复用，
        # 避免快速移动光标时高频 IO（文件大小变化最终也会反映出来）
        if not hasattr(self, "_stat_cache"):
            self._stat_cache = (0.0, -1)
        now = time.time()
        if self.path and os.path.exists(self.path):
            if now - self._stat_cache[0] > 1.0 or self._stat_cache[1] < 0:
                try:
                    size = os.path.getsize(self.path)
                    self._stat_cache = (now, size)
                except OSError:
                    size = -1
            else:
                size = self._stat_cache[1]
        else:
            size = len(self.edit.toPlainText().encode("utf-8"))
        dirty = "●" if self._dirty else ""
        enc = f"  {self._encoding}" if self._encoding else ""
        self.status_lbl.setText(
            f"Ln {ln}, Col {col}   |   {self._lang}{enc}   |   "
            f"{_fmt_size(size)}   {dirty}")

    # ---------- 查找 ----------
    def _find_toggle(self):
        if self.findbar.isVisible():
            self._find_close()
        else:
            self.findbar.show()
            self.find_input.setFocus()
            self.find_input.selectAll()
            self._find_highlight()

    def _find_close(self):
        self.findbar.hide()
        self._clear_find_hl()
        self.edit.setFocus()   # 关闭查找条后焦点还给编辑区

    def _find_highlight(self):
        self._do_find_hl(self.find_input.text())

    def _do_find_hl(self, txt):
        cols = []
        if txt:
            doc = self.edit.document()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(_TOKEN_COLORS["find"]))
            cur = QTextCursor(doc)
            cur = doc.find(txt, cur)
            while not cur.isNull():
                sel = QTextEdit.ExtraSelection()
                sel.format = fmt
                sel.cursor = QTextCursor(cur)
                cols.append(sel)
                cur = doc.find(txt, cur)
        self.edit.set_find_sels(cols)
        self.find_cnt.setText(str(len(cols)))

    def _clear_find_hl(self):
        self.edit.set_find_sels([])
        self.find_cnt.setText("")

    def _find_next(self):
        self._move_to(True)

    def _find_prev(self):
        self._move_to(False)

    def _move_to(self, forward):
        txt = self.find_input.text()
        if not txt:
            return
        self._do_find_hl(txt)
        doc = self.edit.document()
        cur = self.edit.textCursor()
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        found = doc.find(txt, cur, flags)
        if found.isNull():                  # 回绕
            base = QTextCursor(doc)
            if not forward:
                base.movePosition(QTextCursor.MoveOperation.End)
            found = doc.find(txt, base, flags)
        if not found.isNull():
            self.edit.setTextCursor(found)
            self.edit.centerCursor()

    def goto_line(self):
        n, ok = QInputDialog.getInt(self, "Go to line", "Line:", 1, 1, 10 ** 7)
        if ok:
            blk = self.edit.document().findBlockByNumber(n - 1)
            if blk.isValid():
                c = QTextCursor(blk)
                self.edit.setTextCursor(c)
                self.edit.centerCursor()


def _fmt_size(b):
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.2f}G"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.1f}M"
    if b >= 1024:
        return f"{b / 1024:.0f}K"
    return f"{b}B"
