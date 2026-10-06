# -*- coding: utf-8 -*-
"""综合回归测试：本轮全部修复点。运行：QT_QPA_PLATFORM=offscreen python _regress.py"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import audio
audio.set_enabled(False)   # QSoundEffect 起 ffmpeg 线程，测试时静音防 hang

from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")

# ---------- editor.py ----------
from editor import EditorPage
from PyQt6.QtGui import QTextCursor

ed = EditorPage()
e = ed.edit

def settext(t):
    e.setPlainText(t)
    e.moveCursor(QTextCursor.MoveOperation.End)
    e._collect_doc_names()

# 1) 三引号字符串不污染 doc_names
settext('x = 1\n"""\ny = 2\nz = 3\n"""\ndef real(): pass')
check("triple-quote not polluting",
      "y" not in e._doc_names and "z" not in e._doc_names and "real" in e._doc_names,
      f"names={sorted(e._doc_names)}")

# 2) _insert_import：去重（后行 import 不重复插）
settext("import json\nprint(1)")
before = e.toPlainText()
e._insert_import("json")
check("import dedupe across lines", e.toPlainText() == before, e.toPlainText())

# 3) _insert_import：import os.path 不误判已含 os
settext("import os.path\nprint(1)")
e._insert_import("os")
check("import os.path not treated as os",
      "import os\n" in e.toPlainText(), e.toPlainText())

# 4) _insert_import：无 import 块时插文件头、不粘连
settext("print(1)")
e._insert_import("os")
check("import inserted at top cleanly",
      e.toPlainText() == "import os\nprint(1)", repr(e.toPlainText()))

# 5) _insert_import：有 import 块时紧跟其后
settext("import json\nprint(1)")
e._insert_import("os")
check("import follows existing block",
      e.toPlainText() == "import json\nimport os\nprint(1)", repr(e.toPlainText()))

# 6) 弹层：plain 前缀 'p' 首候选 print（回归 m03161）
ed._set_lang("Python")
e.setPlainText("import os\n\np")
e.moveCursor(QTextCursor.MoveOperation.End)
e._show_completion()
if e._comp is not None:
    items = [(e._comp.item(i).data(0x0100)[0], e._comp.item(i).data(0x0100)[2])
             for i in range(e._comp.count())]
    check("completion popup on 'p'", len(items) > 0, f"count={len(items)}")
    tops = [w for w, k in items[:5]]
    check("'p' suggests print first", items[0][0] == "print", f"tops={tops}")
    check("'p' no mod spam in top5",
          not any(w in ("pathlib", "pickle", "platform", "pprint") for w in tops),
          f"tops={tops}")
    check("'p' hot bi before mod", tops[0] == "print", f"tops={tops}")
else:
    check("completion popup on 'p'", False, "no popup")

# ---------- term.py ----------
from term import TerminalWidget, _CmdRunner

t = TerminalWidget()
# 7) _paste_text 多行翻多命令
t.cmd = ""
t._cx = 0
t._paste_text("ls\ndir\r\n")
check("multi-line paste splits commands", t.history.count("ls") == 1 and t.history.count("dir") == 1,
      f"history={t.history[-4:]}")

# 8) _hist_nav 方向：Up 取最新
t.history = ["old", "mid", "new"]
t.hist_pos = -1
t.cmd = ""
t._hist_nav(-1)
check("hist Up -> newest", t.cmd == "new", f"cmd={t.cmd!r}")
t._hist_nav(-1)
check("hist Up again -> older", t.cmd == "mid", f"cmd={t.cmd!r}")
t._hist_nav(1)
check("hist Down -> back to newer", t.cmd == "new", f"cmd={t.cmd!r}")
t._hist_nav(1)
check("hist Down to bottom -> restore original", t.cmd == "", f"cmd={t.cmd!r}")

# 9) _sug_apply：命令名候选循环替换首 token（MEDIUM 3）
t.cmd = "p"
t._cx = 1
t._sugs = ["ps", "ping", "pwd"]
t._sug_idx = 0
t._sug_tmpl = [False, False, False]
t._sug_kind = ["cmd", "cmd", "cmd"]
t._sug_apply(keep=True)
check("sug_apply command token replaced", t.cmd == "ps ", f"cmd={t.cmd!r}")

# 10) _complete 单匹配：cmd 与显示一致（MEDIUM 4）
import tempfile
_tmp = tempfile.mkdtemp(prefix="reg_tst_")
with open(os.path.join(_tmp, "unique_target.txt"), "w") as _f:
    _f.write("")
t.cwd = _tmp
t.cmd = "cd " + os.path.join(_tmp, "unique_tar")
t._cx = len(t.cmd)
t._complete()
check("complete single match syncs cmd", t.cmd == "cd " + os.path.join(_tmp, "unique_target.txt"),
      f"cmd={t.cmd!r}")

# 11) Ctrl+C 置 _feed_cancel（LOW 1）
t._feed_cancel = False
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtCore import QEvent
ke = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
t.keyPressEvent(ke)
check("Ctrl+C sets feed_cancel", t._feed_cancel is True, f"flag={t._feed_cancel}")

# 12) _run_external 重置 _feed_cancel
t._feed_cancel = True
try:
    t._run_external("echo hi")
    check("run_external resets feed_cancel", t._feed_cancel is False, f"flag={t._feed_cancel}")
except Exception as ex:
    check("run_external resets feed_cancel", False, str(ex))
    t._closed = True

# 13) _CmdRunner shutdown：无 Popen 时不炸
j = _CmdRunner("echo hi", t.cwd, t)
try:
    j.shutdown(timeout_ms=500)
    check("CmdRunner.shutdown idle ok", True, "")
except Exception as ex:
    check("CmdRunner.shutdown idle ok", False, str(ex))

# 14) 弹层 plain 分支：前缀 'p' 应联想 print（回归 m03161 + 上下文修复）
ed2 = EditorPage()
ed2._set_lang("Python")
e2 = ed2.edit
e2.setPlainText("import os\n\nos.")
e2.moveCursor(QTextCursor.MoveOperation.End)
e2._show_completion()
if e2._comp is not None:
    w_kind = [(e2._comp.item(i).data(0x0100)[0], e2._comp.item(i).data(0x0100)[2])
              for i in range(e2._comp.count())]
    check("os. attr completions have path",
          any(w == "path" and k == "attr" for w, k in w_kind),
          f"items={w_kind[:10]}")
else:
    check("os. attr completions have path", False, "no popup")

# 结束时清理仍在跑的命令线程，避免 QThread destroyed while running 噪音
for j in list(t._cmd_jobs) + list(t._net_jobs):
    try:
        if isinstance(j, _CmdRunner):
            j.shutdown(timeout_ms=500)
    except Exception:
        pass
t._closed = True

print(f"\n===== {PASS} passed, {FAIL} failed =====")
sys.exit(1 if FAIL else 0)
