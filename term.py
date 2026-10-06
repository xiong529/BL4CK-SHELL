"""pyte 终端控件：渲染 ANSI 流 + 自写命令循环。

设计要点（对应讨论结论）：
- 自画提示符（不跑真 shell），cd/dir/cls/help 内置处理，其余命令 subprocess 透传；
- 输出喂 pyte.Stream 用真终端语义渲染（颜色、光标、清屏都支持）；
- cd 改变目录后发 cwd_changed 信号，让文件树面板跟随刷新。
"""
import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.request

import pyte
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import QApplication, QWidget
from theme import (BLACK, GREEN, GREEN_DIM, GREEN_DARK, GREEN_FAINT,
                   AMBER, RED, PANEL_BG, mono, hub, apply_theme, theme_names)
import audio


def _esc(c: QColor) -> str:
    """QColor → ANSI 前景色序列（主题切换后提示符/输出自动跟随）。"""
    return f"\x1b[38;2;{c.red()};{c.green()};{c.blue()}m"


def _prompt_ansi() -> str:
    return (_esc(GREEN) + "root@dsh" + "\x1b[0m " + _esc(GREEN_DIM) + "➜"
            + "\x1b[0m " + _esc(GREEN))


# =====================================================================
# 极速 ANSI 解析器（替代 pyte.Stream 的逐字符协程 FSM）
# ---------------------------------------------------------------------
# pyte 0.8.2 的 Stream.feed 对每个 CSI 参数位都做一次生成器 send，
# a.py 这类"每像素一个 \x1b[38;2;R;G;Bm"的真彩刷屏输出会被拖到几十 KB/s，
# CPU 打满、动画卡在几 FPS。pyte.Screen 的方法（draw/select_graphic_rendition/
# cursor_* / erase_*）本身很快，瓶颈只在 Stream 的 FSM。这里用正则整段解析
# ANSI 流，直接调 Screen 方法，跳过一次一个字符的协程往返。
# 只支持本终端实际会遇到的序列；未知序列安全忽略（不崩、不丢屏）。
# =====================================================================
import re as _re

_FAST_PLAIN = _re.compile(r"[^\r\n\x0b\x0c\t\x08\x07]+")  # 普通文本（排除 basic 控制字符）
_CSI_FULL = _re.compile(r"\x1b\[([0-9;?]*)([A-Za-z@`])")   # 完整 CSI：ESC[ 参数 终字符
_CSI_TAIL = _re.compile(r"\x1b\[[0-9;?]*$")                # 块尾不完整的 CSI 前缀（等下一块）

# basic 控制字符 → Screen 方法名（None = 忽略，如 BEL）
_FAST_BASIC = {chr(13): "carriage_return", chr(10): "linefeed",
               chr(11): "linefeed", chr(12): "linefeed",
               chr(9): "tab", chr(8): "backspace", chr(7): None}

# CSI 终字符 → Screen 方法名（只映射本终端用到的；不安全的 DA/DSR 等忽略）
_FAST_CSI = {"m": "select_graphic_rendition", "J": "erase_in_display",
             "K": "erase_in_line", "H": "cursor_position", "f": "cursor_position",
             "A": "cursor_up", "B": "cursor_down", "C": "cursor_forward",
             "D": "cursor_back", "G": "cursor_to_column", "`": "cursor_to_column",
             "d": "cursor_to_line", "r": "set_margins", "h": "set_mode",
             "l": "reset_mode", "X": "erase_characters", "@": "insert_characters",
             "P": "delete_characters", "L": "insert_lines", "M": "delete_lines",
             "g": "clear_tab_stop", "s": "save_cursor", "u": "restore_cursor"}


def _fast_csi(screen, params_s, final):
    """解析一个完整 CSI 的参数串并调用 Screen 方法。"""
    private = params_s.startswith("?")
    if private:
        params_s = params_s[1:]
    params = []
    if params_s:
        for p in params_s.split(";"):
            p = p.strip()
            params.append(min(int(p) if p.isdigit() else 0, 9999))
    name = _FAST_CSI.get(final)
    if name is None:
        return                       # 未知序列：安全忽略
    fn = getattr(screen, name, None)
    if fn is None:
        return
    try:
        if name in ("set_mode", "reset_mode"):
            fn(*params, private=private)
        elif params:
            fn(*params)
        else:
            fn()
    except TypeError:
        pass                         # 参数组合不合法：忽略该序列


def _fast_plain(screen, text):
    """把一段纯文本交给 screen.draw，basic 控制字符单独调度（\r\n\t\b）。"""
    i = 0
    n = len(text)
    while i < n:
        m = _FAST_PLAIN.match(text, i)
        if m:
            screen.draw(m.group())
            i = m.end()
            continue
        c = text[i]
        meth = _FAST_BASIC.get(c)
        if meth:
            getattr(screen, meth)()
        i += 1


def _fast_feed(screen, data, pending=""):
    """快速喂入：返回需保留到下一块的未闭合序列前缀（块边界切断 CSI 时）。"""
    if pending:
        data = pending + data
        pending = ""
    i = 0
    n = len(data)
    while i < n:
        esc = data.find("\x1b", i)
        if esc < 0:
            _fast_plain(screen, data[i:])
            return pending
        if esc > i:
            _fast_plain(screen, data[i:esc])
        # ---- 处理 ESC 序列 ----
        if esc + 1 >= n:
            return data[esc:]            # 裸 ESC 结尾：等下一块
        c = data[esc + 1]
        if c == "[":
            m = _CSI_FULL.match(data, esc)
            if m:
                _fast_csi(screen, m.group(1), m.group(2))
                i = m.end()
                continue
            if _CSI_TAIL.match(data, esc):   # 不完整 CSI（缺终字符）→ 续块
                return data[esc:]
            i = esc + 2                      # 畸形 CSI：跳过
        elif c == "]":
            # OSC：跳到 BEL 或 ESC \ 结束，都不出现则等下一块
            j = data.find("\x07", esc + 2)
            k = data.find("\x1b\\", esc + 2)
            if j < 0 and k < 0:
                return data[esc:]
            i = (k + 2) if (k >= 0 and (j < 0 or k + 2 <= j)) else (j + 1)
        elif c in "()*+$":
            if esc + 2 >= n:
                return data[esc:]        # 字符集选择 ESC ( x：等第二字符
            i = esc + 3                  # 忽略 charset 切换（pyte 默认映射直通）
        elif c in "78":
            getattr(screen, "save_cursor" if c == "7" else "restore_cursor")()
            i = esc + 2
        elif c == "c":
            screen.reset()
            i = esc + 2
        else:
            i = esc + 2                  # 其他单字符 ESC：忽略
    return pending


PROMPT = "\x1b[38;2;0;255;65mroot@dsh\x1b[0m \x1b[38;2;0;170;47m➜\x1b[0m \x1b[38;2;0;255;65m"
PROMPT_END = "\x1b[0m \x1b[?25h"

# 首屏 ASCII 大 logo（加粗方块字，宽 90、高 5）—— BL4CK://SHELL
# 渲染时逐行 ljust 到 logo_w 再居中，避免各行尾部空格被裁导致歪斜
BL4CK_LOGO = [
    "█████  █      █   █  ██████ █    █            ██     ██ ██████ █    █ ██████ █      █",
    "█    █ █      █   █  █      █   █    ██      ██     ██  █      █    █ █      █      █",
    "█████  █      ██████ █      ████            ██     ██   █████  ██████ █████  █      █",
    "█    █ █          █  █      █   █    ██    ██     ██         █ █    █ █      █      █",
    "█████  ██████     █  ██████ █    █        ██     ██     ██████ █    █ ██████ ██████ ██████",
]

# matrix 特效字符池（纯装饰，无木马特征）
MATRIX_CHARS = "01アイウエオカキクケコサシスセソタチツテト<>*+="

# 命令表：name -> (分类, 简短说明)。补全与 help 共用。
# 分类 b=内置真实现 / L=Linux 别名映射到 Windows 真命令 / n=网络 / f=特效
COMMANDS = [
    ("cd", "b", "切换目录"),
    ("ls / dir", "b", "列目录"),
    ("pwd", "b", "当前路径"),
    ("echo", "b", "回显文本"),
    ("cat", "b", "查看文件 (type)"),
    ("clear / cls", "b", "清屏"),
    ("help", "b", "帮助"),
    ("ps", "L", "进程列表 (tasklist)"),
    ("top", "L", "进程快照 (tasklist)"),
    ("tasklist", "L", "进程列表"),
    ("taskkill", "L", "结束进程 (taskkill /pid N /f)"),
    ("ifconfig / ip", "L", "网络配置 (ipconfig)"),
    ("ipconfig", "L", "网络配置"),
    ("ping", "L", "网络连通 (ping)"),
    ("tracert", "L", "路由跟踪"),
    ("netstat", "L", "网络连接与端口"),
    ("whoami", "b", "当前用户"),
    ("hostname", "b", "主机名"),
    ("date", "b", "当前日期时间"),
    ("uname", "b", "系统信息"),
    ("df", "b", "磁盘用量 (真数据)"),
    ("free", "b", "内存用量 (真数据)"),
    ("tree", "b", "目录树"),
    ("grep", "L", "内容搜索 (findstr)"),
    ("curl", "L", "HTTP 请求"),
    ("whereami", "n", "真实本机定位 (联动地图)"),
    ("weather", "n", "天气，weather [城市]"),
    ("quote", "n", "一句名言"),
    ("crypto", "n", "币价，crypto btc|eth|..."),
    ("matrix", "f", "矩阵雨 (Ctrl+C 退出)"),
    ("ver", "L", "Windows 版本"),
    ("systeminfo", "L", "系统信息全量"),
    ("theme", "b", "主题切换，theme [name]"),
    ("sound", "b", "音效开关，sound on|off"),
    ("edit", "b", "编辑器打开/新建：edit <路径>"),
]

# Linux 风格命令 → Windows 真命令（首个 token 完全匹配才替换）
# 注意：ls/cat 走内置 _do_dir/_do_cat 干净输出（无 dir 表头/统计行），故不在别名表里
CMD_ALIAS = {
    "rm": "del", "cp": "copy", "mv": "move",
    "mkdir": "md", "rmdir": "rd", "grep": "findstr",
    "ps": "tasklist", "top": "tasklist", "ifconfig": "ipconfig",
    "ip": "ipconfig", "uname": "ver",
}

# 补全候选命令的说明文字（与命令表保持一致，缺失则留空）
_SUG_DESC = dict((name, desc) for name, _cat, desc in COMMANDS if not name.startswith(("ls /", "clear /", "ifconfig /")))
_SUG_DESC.update({
    "ls": "列目录 (dir)", "clear": "清屏", "cls": "清屏",
    "ifconfig": "网络配置 (ipconfig)", "ip": "网络配置",
    "type": "查看文件 (cat)", "findstr": "内容搜索 (grep)",
    "ipconfig": "网络配置", "md": "创建目录 (mkdir)", "rd": "删除目录 (rmdir)",
    "netstat": "网络连接与端口", "taskkill": "按 PID 结束进程",
    "dir": "列目录", "mkdir": "创建目录", "rmdir": "删除目录",
    "ren": "重命名", "set": "显示环境变量", "net": "网络管理命令",
    "nslookup": "DNS 查询", "route": "路由表", "shutdown": "关机/重启",
})
def _sug_desc(s):
    return _SUG_DESC.get(s, "") or _ARG_DESC.get(s, "")


def _subseq(p, s):
    """子序列匹配（fzf 风格）：p 的字符按顺序全部出现在 s 里。
    例：'tl' 匹配 'tasklist'（t…l）、'title'（t…l）。"""
    it = iter(s)
    return all(ch in it for ch in p)


def _disp_w(s):
    """字符串的终端显示宽度：中文/全角按 2 列（wcwidth 规则），控制字符 0。"""
    from wcwidth import wcwidth
    return sum(max(wcwidth(c), 0) for c in s)


def _spec_desc(s):
    """从命令 spec 里查选项/子命令的说明（如 ping 的 -t、net 的 use）。"""
    for _name, sp in _CMD_SPECS.items():
        for o, d in sp.get("opts", []):
            if o == s:
                return d
        for su, d in sp.get("subs", []):
            if su == s:
                return d
    return ""

# 参数补全池：命令 → 候选参数（输入"命令 + 空格"后按前缀联想，如 type → nul）
_ARG_SUGS = {
    "type": ["nul", "con"],
    "ping": ["127.0.0.1", "8.8.8.8", "1.1.1.1", "localhost", "www.baidu.com"],
    "crypto": ["btc", "eth", "sol", "doge", "ada", "xrp", "bnb", "ltc"],
    "weather": ["beijing", "shanghai", "guangzhou", "shenzhen", "singapore",
                "tokyo", "london", "newyork", "paris", "moscow"],
    "chcp": ["65001", "936", "437"],
    "color": ["0a", "0f", "0c", "0e", "07"],
    "tracert": ["127.0.0.1", "8.8.8.8", "www.baidu.com"],
    "nslookup": ["www.baidu.com", "www.google.com", "localhost"],
}
# 参数候选的说明（命令名说明查 _SUG_DESC，参数说明查这里）
_ARG_DESC = {
    "nul": "空设备文件 (type nul > file 建空文件)", "con": "控制台输入",
    "127.0.0.1": "本机回环", "8.8.8.8": "Google DNS", "1.1.1.1": "Cloudflare DNS",
    "localhost": "本机", "www.baidu.com": "百度", "www.google.com": "Google",
    "btc": "比特币", "eth": "以太坊", "sol": "Solana", "doge": "狗狗币",
    "ada": "Cardano", "xrp": "Ripple", "bnb": "BNB", "ltc": "莱特币",
    "beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
    "singapore": "新加坡", "tokyo": "东京", "london": "伦敦", "newyork": "纽约",
    "paris": "巴黎", "moscow": "莫斯科",
    "65001": "UTF-8 代码页", "936": "GBK 代码页", "437": "ASCII 代码页",
    "0a": "黑底浅绿", "0f": "黑底白字", "0c": "黑底亮红", "0e": "黑底亮黄",
}
# 文件/目录操作命令：参数用当前目录真实文件名联想
_FILE_CMDS = ("cd", "dir", "ls", "cat", "type", "del", "rm", "copy", "cp",
              "move", "mv", "ren", "xcopy", "mkdir", "md", "rmdir", "rd",
              "tree", "start", "notepad", "edit")

# 命令模板（VS Code snippet 风格）：不知道语法也能 Tab 填出整条命令。
# 键=命令名（含别名），值=(完整命令示例, 说明)。
_CMD_EXAMPLES = {
    "type": [("type nul > a.txt", "创建空文件"),
             ("type 文件名", "查看文本文件")],
    "cat": [("cat 文件名", "查看文本文件")],
    "ping": [("ping 8.8.8.8 -n 4", "Ping 4 次后停止"),
             ("ping -t 8.8.8.8", "持续 Ping 直到 Ctrl+C")],
    "mkdir": [("mkdir 新文件夹", "创建目录")],
    "md": [("md 新文件夹", "创建目录")],
    "del": [("del 文件.txt", "删除文件")],
    "rm": [("rm 文件.txt", "删除文件")],
    "copy": [("copy 源.txt 目标.txt", "复制文件")],
    "cp": [("cp 源.txt 目标.txt", "复制文件")],
    "move": [("move 源.txt 目标.txt", "移动文件")],
    "mv": [("mv 源.txt 目标.txt", "移动文件")],
    "ren": [("ren 旧名.txt 新名.txt", "重命名文件")],
    "cd": [("cd ..", "返回上级目录")],
    "dir": [("dir /b", "只列文件名"), ("dir /s", "含子目录")],
    "ls": [("ls /b", "只列文件名")],
    "tree": [("tree /f", "显示含文件目录树")],
    "findstr": [('findstr "关键字" 文件.txt', "在文件中搜索文本")],
    "grep": [('grep "关键字" 文件.txt', "在文件中搜索文本")],
    "taskkill": [("taskkill /pid 1234 /f", "按 PID 强制结束进程")],
    "netstat": [("netstat -ano", "显示端口与所属 PID")],
    "ipconfig": [("ipconfig /all", "完整网络配置")],
    "ifconfig": [("ipconfig /all", "完整网络配置")],
    "ip": [("ipconfig /all", "完整网络配置")],
    "tracert": [("tracert 8.8.8.8", "路由跟踪到目标")],
    "nslookup": [("nslookup www.baidu.com", "DNS 解析查询")],
    "route": [("route print", "显示路由表")],
    "chcp": [("chcp 65001", "切换 UTF-8 代码页")],
    "shutdown": [("shutdown /s /t 0", "立即关机"),
                 ("shutdown /r /t 0", "立即重启")],
    "curl": [("curl -I https://www.baidu.com", "只取响应头")],
    "echo": [("echo hello > a.txt", "把文本写入文件")],
    "weather": [("weather singapore", "查新加坡天气")],
    "crypto": [("crypto btc", "比特币实时价格")],
    "systeminfo": [("systeminfo", "系统全量信息")],
    "tasklist": [("tasklist", "进程列表")],
    "ps": [("tasklist", "进程列表")],
    "start": [("start notepad", "打开记事本")],
}

# ---- 命令 spec（inshellisense/Fig 风格精简版）：选项 + 子命令 ----
# 键=命令名（含别名）；opts=选项列表 (名称, 说明)；subs=子命令列表 (名称, 说明)。
# 输入 "-" 时联想选项、输入子命令时联想子命令；已用选项自动排除。
_CMD_SPECS = {
    "ping": {"opts": [("-t", "持续 Ping 直到 Ctrl+C"), ("-n", "发送次数，如 -n 4"),
                      ("-l", "缓冲区大小"), ("-a", "解析主机名"), ("-4", "强制 IPv4"),
                      ("-6", "强制 IPv6"), ("-w", "超时毫秒")], "subs": []},
    "tracert": {"opts": [("-d", "不解析主机名"), ("-h", "最大跳数"), ("-w", "超时毫秒"),
                         ("-4", "强制 IPv4"), ("-6", "强制 IPv6")], "subs": []},
    "netstat": {"opts": [("-a", "所有连接与端口"), ("-n", "数字地址不解析"),
                         ("-o", "显示 PID"), ("-t", "仅 TCP"), ("-u", "仅 UDP"),
                         ("-p", "按协议过滤"), ("-s", "按协议统计"), ("-r", "路由表")], "subs": []},
    "ipconfig": {"opts": [("/all", "完整配置"), ("/release", "释放 IP"), ("/renew", "续租 IP"),
                          ("/flushdns", "刷新 DNS 缓存"), ("/displaydns", "查看 DNS 缓存"),
                          ("/registerdns", "注册 DNS")], "subs": []},
    "ifconfig": {"opts": [("/all", "完整配置"), ("/flushdns", "刷新 DNS 缓存")], "subs": []},
    "tasklist": {"opts": [("/v", "详细模式"), ("/fi", "按条件过滤"), ("/fo", "输出格式"),
                          ("/nh", "无表头"), ("/svc", "显示服务")], "subs": []},
    "taskkill": {"opts": [("/pid", "按 PID 结束"), ("/im", "按映像名结束"),
                          ("/f", "强制结束"), ("/t", "结束子进程")], "subs": []},
    "dir": {"opts": [("/b", "简洁格式"), ("/s", "含子目录"), ("/a", "按属性过滤"),
                     ("/o", "排序方式"), ("/w", "宽格式")], "subs": []},
    "ls": {"opts": [("/b", "简洁格式"), ("/s", "含子目录")], "subs": []},
    "del": {"opts": [("/f", "强制删除只读"), ("/q", "安静模式"), ("/s", "含子目录")], "subs": []},
    "rm": {"opts": [("/f", "强制删除只读"), ("/q", "安静模式"), ("/s", "含子目录")], "subs": []},
    "copy": {"opts": [("/y", "覆盖不询问"), ("/v", "校验写入")], "subs": []},
    "cp": {"opts": [("/y", "覆盖不询问")], "subs": []},
    "move": {"opts": [("/y", "覆盖不询问")], "subs": []},
    "mv": {"opts": [("/y", "覆盖不询问")], "subs": []},
    "xcopy": {"opts": [("/s", "含子目录"), ("/e", "含空目录"), ("/y", "覆盖不询问"),
                       ("/i", "目标为目录")], "subs": []},
    "shutdown": {"opts": [("/s", "关机"), ("/r", "重启"), ("/a", "取消关机"),
                          ("/t", "倒计时秒数"), ("/f", "强制关闭程序")], "subs": []},
    "findstr": {"opts": [("/i", "忽略大小写"), ("/r", "正则匹配"), ("/n", "显示行号"),
                         ("/c", "字面字符串")], "subs": []},
    "grep": {"opts": [("/i", "忽略大小写"), ("/r", "正则匹配"), ("/n", "显示行号")], "subs": []},
    "nslookup": {"opts": [("-d", "调试模式"), ("-port", "指定端口")], "subs": []},
    "route": {"opts": [], "subs": [("print", "显示路由表"), ("add", "添加路由"),
                                   ("delete", "删除路由"), ("change", "修改路由")]},
    "net": {"opts": [], "subs": [("use", "连接/断开共享"), ("start", "启动服务"),
                                 ("stop", "停止服务"), ("pause", "暂停服务"),
                                 ("continue", "恢复服务"), ("share", "管理共享"),
                                 ("session", "管理会话"), ("user", "管理用户"),
                                 ("view", "查看共享资源"), ("statistics", "网络统计")]},
    "net use": {"opts": [("/persistent:yes", "持久连接"), ("/delete", "断开连接")], "subs": []},
    "net start": {"opts": [], "subs": []},
    "chcp": {"opts": [], "subs": []},
    "color": {"opts": [], "subs": []},
    "tree": {"opts": [("/f", "显示文件"), ("/a", "ASCII 字符")], "subs": []},
    "curl": {"opts": [("-I", "只取响应头"), ("-X", "指定方法"), ("-H", "自定义请求头"),
                      ("-o", "输出到文件"), ("-L", "跟随重定向"), ("-k", "忽略证书")], "subs": []},
    "systeminfo": {"opts": [("/s", "远程主机")], "subs": []},
    "taskkill /pid": {"opts": [], "subs": []},
}


class _NetWorker(QThread):
    """后台网络请求线程：请求在子线程跑，结果回 GUI 线程。"""

    result = pyqtSignal(str, str)   # (kind, text)

    def __init__(self, kind, url, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read().decode("utf-8", "replace")
            self.result.emit(self.kind, text)
        except Exception as e:
            self.result.emit(self.kind, "__ERR__" + str(e))


class _CmdRunner(QThread):
    """外部命令后台执行：subprocess 在子线程跑，超时/输出都回主线程。

    长输出卡顿的根因是 pyte 逐字符 feed（~8us/字符），50 万字符约 4s；
    同步 run + 一次喂完会让 GUI 冻结。这里只负责跑命令，输出由
    TerminalWidget 分块喂给 pyte，界面保持响应。
    """

    done = pyqtSignal(int, str, str)   # (returncode, stdout, stderr)
    output = pyqtSignal(str, bool)     # (text, is_stderr)：流式输出块，边跑边回显

    def __init__(self, cmdline, cwd, parent=None):
        super().__init__(parent)
        self.cmdline = cmdline
        self.cwd = cwd
        self._proc = None   # Popen 句柄：closeEvent 时可 kill，避免孤儿进程

    def run(self):
        """流式执行：起泵线程读 stdout/stderr 分块 emit，不等到进程结束。
        长时/无限循环命令（动画、服务器、ping -t）也能实时回显；
        超时不再硬杀 —— 用户按 Ctrl+C 或关窗时由 shutdown() kill。"""
        import threading
        try:
            self._proc = subprocess.Popen(
                self.cmdline, cwd=self.cwd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except Exception as e:
            self.done.emit(-2, "", str(e))
            return

        def pump(fd, is_err):
            try:
                while True:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        break
                    s = _decode_cmd(chunk)
                    if s:
                        self.output.emit(s, is_err)
            except Exception:
                pass

        ths = [
            threading.Thread(target=pump,
                             args=(self._proc.stdout.fileno(), False),
                             daemon=True),
            threading.Thread(target=pump,
                             args=(self._proc.stderr.fileno(), True),
                             daemon=True),
        ]
        for t in ths:
            t.start()
        rc = self._proc.wait()
        for t in ths:
            t.join(1.0)   # 等泵线程读到 EOF 发完最后一块
        self.done.emit(rc, "", "")

    def _kill_tree(self):
        """杀整棵进程树（taskkill /t 按父子关系遍历，孙进程也杀），
        失败时退回只杀 shell。返回是否已触发杀进程。"""
        p = self._proc
        if p is None or p.poll() is not None:
            return False
        try:
            subprocess.run(
                ["taskkill", "/pid", str(p.pid), "/t", "/f"],
                capture_output=True, timeout=3)
            return True
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            return True

    def shutdown(self, timeout_ms=8500):
        """关窗时调用：杀进程树并等待线程退出，避免孤儿进程/线程悬挂。"""
        self._kill_tree()
        if self.isRunning():
            try:
                self.wait(timeout_ms)
            except Exception:
                pass


def _decode_cmd(b: bytes) -> str:
    """Windows cmd 输出是 cp936(GBK)；先试 utf-8，失败回退 gbk，
    避免 ping/ipconfig 等中文输出在 utf-8 解码下变成乱码。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


class TerminalWidget(QWidget):
    cwd_changed = pyqtSignal(str)
    located = pyqtSignal(float, float, str, str)   # lat, lon, city, ip
    open_file = pyqtSignal(str)                    # 请求在编辑器标签打开文件

    COLS = 100
    ROWS = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.cwd = os.path.expanduser("~")

        self.screen = pyte.HistoryScreen(self.COLS, self.ROWS, history=2000)
        self.stream = pyte.Stream(self.screen)   # 保留：解析已交给 _fast_feed，仅作降级参照
        self._feed_pending = ""                  # 跨块未闭合的 ANSI 序列前缀

        self.history = []       # 已执行命令历史
        self.hist_pos = -1      # 当前浏览位置（-1=未翻历史；0..n-1=从最新往回数的步数）
        self._hist_orig = ""    # 翻历史前未发送输入的暂存（Down 回到底时还原）
        self.cmd = ""           # 当前输入行
        self._cx = 0            # 输入光标在 cmd 内的偏移（字符，非显示列）
        self._char_w = 0
        self._char_h = 0
        self._blink = True
        self._last_size = None
        self._dirty = set(range(self.ROWS))   # 需要重画的行（脏行），避免每次全量重绘

        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(500)

        # 首屏不在这里打印：构造时控件还是默认尺寸，等布局给出真实尺寸后
        # 再打（否则一旦行数变小，pyte 会把已打印的顶部内容压进 history，
        # 表现就是首屏一片空白）。见 showEvent/_first_banner。
        self._banner_done = False
        self._closed = False       # 关窗标志：切断流式输出的挂起回调（见 _stream_feed）
        self._feed_cancel = False  # Ctrl+C 标志：中断挂起的流式输出分块
        # 流式输出串行队列：所有输出块先进 FIFO，由唯一一条链逐块推进，
        # 避免多条 _stream_feed 闭包链交错喂 pyte（块边界切断 CSI 序列时
        # 下一块开头的 @ 等字符会被 pyte FSM 误当 ICH 终字符 → insert_characters 崩溃）。
        self._feed_q = []
        self._feed_running = False
        self._feed_pos = 0
        self._feed_max_q = 64   # 队列积压上限：超限丢中间旧块（追不上海量输出时）
        # 喂块 pacer：8ms 一拍（~125Hz）。实测 QTimer(0) 单发在事件循环忙碌
        # （泵线程 833 次/s 信号 + paint）时几乎不触发，让出续链会僵死；
        # 周期定时器稳定，每拍喂一块 4KB（~512KB/s 吞吐，远超动画输出 274KB/s）。
        self._feed_pacer = QTimer(self)
        self._feed_pacer.setInterval(8)
        self._feed_pacer.timeout.connect(self._feed_tick)
        self._net_jobs = []        # 持有的后台网络线程
        self._cmd_jobs = []        # 持有的后台命令线程
        self._mixer = None         # matrix 特效定时器
        self._sugs = []            # TODO: 补全候选项（token 前缀 → 命令名列表）
        self._sug_idx = 0
        self._sug_tmpl = []        # 与 _sugs 平行：True=命令模板（完整示例）
        self._sug_kind = []        # 与 _sugs 平行：cmd/opt/sub/arg/tmpl（绘制图标用）

        # 鼠标选择 / 滚动 / 剪贴板
        self._sel_anchor = None    # (row, col) 拖选起点
        self._sel_end = None       # (row, col) 拖选终点
        self._scroll_lines = 0     # 滚轮向上回看的行数（0 = 跟随底部）
        self._max_scroll = 0
        self._scroll_rows = []     # 滚动时缓存要显示的屏幕行
        self._has_scroll = False   # 是否有历史可回看

        # IME 中文输入支持（普通 QWidget 默认不接收输入法事件）
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_KeyCompression, False)

    # ---------- 首屏 ----------
    def showEvent(self, e):
        super().showEvent(e)
        if not self._banner_done:
            QTimer.singleShot(0, self._first_banner)

    def _first_banner(self):
        if self._banner_done or not self.isVisible():
            return
        # 布局尚未给真实尺寸时延后一帧再试（防止默认小尺寸打印后被
        # pyte 压进 history 导致首屏空白——见 __init__ 的注释）
        if self.width() <= 0 or self.height() <= 0:
            QTimer.singleShot(0, self._first_banner)
            return
        self._banner_done = True
        self._emit("\x1b[2J\x1b[H")      # 清屏 + 光标归位
        self._banner()
        self._show_prompt()

    @staticmethod
    def _kbd():
        """键盘滴答音效（限流，避免粘连）。QSoundEffect 异步无阻塞。
        eDEX-UI 映射：stdin.wav = 每次按键；keyboard.wav = 屏幕键盘弹出。"""
        audio.play("stdin", 0.35)

    # ---------- 渲染 ----------
    def _banner(self):
        """首屏：neofetch/Kali 风格 —— 加粗 ASCII 大 logo + 真机信息块 + 16 色板条。

        信息项（OS/Host/Kernel/Uptime/Packages/Shell/Resolution/DE/WM/Theme/
        Terminal/CPU/Memory）模仿 Kali 的 neofetch 排版；数据全部来自快速来源
        （platform / psutil / winreg 注册表读 CPU 型号与已装程序数 / Qt 屏幕），
        不调 wmic（慢 1-2 秒）。
        """
        G = _esc(GREEN)
        D = _esc(GREEN_DIM)
        A = _esc(AMBER)
        Z = "\x1b[0m"
        fields = []
        try:
            import platform
            import socket
            import psutil
            import theme as _theme

            def _rk(path, name=""):
                """读 HKLM 注册表字符串（失败返回空串）。"""
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                        return str(winreg.QueryValueEx(k, name)[0])
                except Exception:
                    return ""

            cpu_key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            cpu = (_rk(cpu_key, "ProcessorNameString").strip()
                   or platform.processor() or platform.machine() or "unknown")
            try:
                mhz = int(_rk(cpu_key, "~MHz")) or 0
            except (TypeError, ValueError):
                mhz = 0
            cores = os.cpu_count() or 0
            vendor = _rk(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer").strip()
            model = _rk(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName").strip()
            host = f"{vendor} {model}".strip() or platform.machine() or "unknown"

            def _npkg():
                """已安装程序数（三处 Uninstall 注册表项计数，~20ms）。"""
                try:
                    import winreg
                except ImportError:
                    return 0
                subs = ((winreg.HKEY_LOCAL_MACHINE,
                         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                        (winreg.HKEY_LOCAL_MACHINE,
                         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                        (winreg.HKEY_CURRENT_USER,
                         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
                n = 0
                for hive, sub in subs:
                    try:
                        with winreg.OpenKey(hive, sub) as k:
                            i = 0
                            while True:
                                try:
                                    winreg.EnumKey(k, i)
                                    n += 1
                                    i += 1
                                except OSError:
                                    break
                    except OSError:
                        pass
                return n

            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            up = int(time.time() - psutil.boot_time())
            d, r = divmod(up, 86400)
            h, r = divmod(r, 3600)
            m = r // 60
            root = os.path.splitdrive(self.cwd)[0] + os.sep
            try:
                du = psutil.disk_usage(root)
                disk = f"{du.total / 1024 ** 3:.0f}G ({du.percent:.0f}% used)"
            except OSError:
                disk = "n/a"
            ip = "127.0.0.1"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
            except OSError:
                pass
            res = "n/a"
            try:
                from PyQt6.QtWidgets import QApplication
                g = QApplication.primaryScreen().availableGeometry()
                res = f"{g.width()}x{g.height()}"
            except Exception:
                pass
            try:
                edition = platform.win32_edition() or ""
            except Exception:
                edition = ""
            # Windows 版本友好化：Win11 判定用 build ≥ 22000；版名映射缩写
            try:
                _build = int(platform.version().split(".")[-1])
            except ValueError:
                _build = 0
            _ed_map = {
                "Core": "Home", "CoreCountrySpecific": "Home (CN)",
                "CoreSingleLanguage": "Home (SL)", "Professional": "Pro",
                "ProfessionalN": "Pro N", "Enterprise": "Enterprise",
                "Education": "Education", "ProfessionalEducation": "Pro Education",
            }
            _win = "Windows 11" if _build >= 22000 else f"Windows {platform.release()}"
            os_name = " ".join(x for x in (_win, _ed_map.get(edition, edition),
                                           platform.machine()) if x)
            mib = 1024 ** 2
            cpu_line = cpu[:38]
            if mhz:
                cpu_line += f" ({cores}) @ {mhz / 1000:.2f}GHz"
            elif cores:
                cpu_line += f" ({cores})"
            fields = [
                ("OS", os_name),
                ("Host", host[:40]),
                ("Kernel", f"{platform.version()}-{platform.machine().lower()}"),
                ("Uptime", f"{d}d {h}h {m}m"),
                ("Packages", f"{_npkg()} (installed)"),
                ("Shell", "BL4CK://SHELL 1.0"),
                ("Resolution", res),
                ("DE", "Windows Explorer"),
                ("WM", f"DWM {platform.version()}"),
                ("Theme", str(getattr(_theme, "CURRENT", "matrix"))),
                ("Terminal", "BL4CK://SHELL"),
                ("CPU", cpu_line),
                ("Memory", f"{vm.used // mib}MiB / {vm.total // mib}MiB"),
                ("Swap", f"{sw.used // mib}MiB / {sw.total // mib}MiB"),
                ("Disk", disk),
                ("IP", ip),
                ("Processes", str(len(psutil.pids()))),
            ]
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    fields.insert(4, ("Battery", f"{b.percent}%"))
            except Exception:
                pass
        except Exception:
            pass

        t = time.strftime("%H:%M:%S")
        logo_w = max(len(x) for x in BL4CK_LOGO)
        bar_w = min(max(20, self.COLS - 4), logo_w)
        left = " " * max(2, (self.COLS - logo_w) // 2)
        self._emit(f"{G}  BL4CK://SHELL v1.0   NEON CONSOLE{Z}  {D}[{t}]{Z}\r\n")
        self._emit(f"{D}  " + "=" * bar_w + f"{Z}\r\n")
        for line in BL4CK_LOGO:
            self._emit(f"{left}{G}{line.ljust(logo_w)}{Z}\r\n")
        self._emit(f"{D}  " + "-" * bar_w + f"{Z}\r\n")
        # neofetch 头：user@host + 下划线
        self._emit(f"{G}  root@dsh{Z}\r\n")
        self._emit(f"{D}  -------{Z}\r\n")
        for k, v in fields:
            self._emit(f"{D}  {k:<11}: {G}{v}{Z}\r\n")
        # neofetch 16 色板条：▄ 上下拼色（下=lo、上=hi），两行铺满
        for row in range(2):
            seg = "".join(f"\x1b[38;5;{i + row * 8}m\x1b[48;5;{(i + row * 8 + 8) % 16}m▄▄▄"
                          for i in range(8))
            self._emit(f"  {seg}{Z}\r\n")
        self._emit(f"{D}  " + "=" * bar_w + f"{Z}\r\n")
        self._emit(f"  type {G}help{Z} for commands  {A}*{Z}  "
                   f"tab completes paths  {A}*{Z}  up/down recalls history\r\n\r\n")

    def _toggle_blink(self):
        self._blink = not self._blink
        self._invalidate_rows([self.screen.cursor.y])

    def _invalidate_rows(self, rows):
        """请求只重画这几行。

        关键：不再维护“持久脏集合”。Qt 在窗口遮挡/切标签/grab() 时会发来
        全量 Expose，此时必须整屏重画；靠持久脏集合会在那种情况下整块空白。
        这里只缩小重画区域，真正的重画范围由 paintEvent 的 e.rect() 决定。
        """
        if not self._char_h or not self._char_w:
            self.update()
            return
        rows = [r for r in rows if 0 <= r < self.ROWS]
        if not rows:
            return
        top = min(rows) * self._char_h
        bottom = (max(rows) + 1) * self._char_h
        self.update(0, top, self.width(), bottom - top)

    def _emit(self, text: str):
        """喂给 screen，一切显示都走这里。

        用自写 _fast_feed 正则解析（跳过 pyte.Stream 逐字符 FSM，真彩刷屏
        从 ~280KB/s 提升到数 MB/s）；跨块被切断的 CSI 序列由 _feed_pending
        续块拼接（这正是之前 2048 分块交错崩溃的同类场景，串行队列保证顺序）。

        增量重画：pyte 已把改动行记进 screen.dirty（draw/erase 标所在行、
        滚动标全屏），这里把 dirty + 光标行转成局部 update。paintEvent 按
        e.rect() 只画失效行；Expose/切标签/grab 时 Qt 送全窗口 rect → 全画，
        不会漏。滚回查看模式下退化全屏（历史行在视口里的位置随偏移变化）。
        """
        self._feed_pending = _fast_feed(self.screen, text, self._feed_pending)
        if self._scroll_lines > 0:
            self.update()
            if self.screen.dirty:
                self.screen.dirty.clear()
            return
        rows = {self.screen.cursor.y}      # 光标行总带上：光标移动本身不标 dirty
        d = self.screen.dirty
        if d:
            rows |= d
            self.screen.dirty.clear()
        if len(rows) >= self.ROWS:
            self.update()
        else:
            self._invalidate_rows(list(rows))

    def paintEvent(self, e):
        p = QPainter(self)
        fm = QFontMetrics(mono())
        self._char_w = max(1, fm.horizontalAdvance("W"))
        self._char_h = max(1, fm.height())
        p.setFont(mono())
        bg = QColor(BLACK.toRgb().name())

        # 按 e.rect() 只重画失效区域：_emit 已把 dirty+cursor 行做成局部
        # update，这里把行循环裁剪到 rect 覆盖的几行（Expose/切标签/grab/
        # resize 时 Qt 送全窗口 rect → 退化为整屏，不会漏画）。
        rect = e.rect()
        p.fillRect(rect, bg)
        r0 = max(0, rect.top() // self._char_h)
        rows_fit = max(1, self.height() // self._char_h)
        r1 = min(self.ROWS - 1, len(self.screen.display) - 1,
                 rows_fit - 1, rect.bottom() // self._char_h)

        # ---- 滚动视图：回看历史（滚轮回看时调用者全屏 update） ----
        if self._scroll_lines > 0:
            self._paint_scroll_view(p, rows_fit)
            p.end()
            return

        disp = self.screen.display
        buffer = self.screen.buffer
        for y in range(r0, r1 + 1):
            self._paint_row(p, y, disp, buffer)

        # ---- 左键拖选的选区高亮（半透明绿色覆盖） ----
        if self._sel_anchor is not None and self._sel_end is not None:
            a0, c0 = self._sel_anchor
            a1, c1 = self._sel_end
            if a0 > a1:
                a0, a1 = a1, a0
                c0, c1 = c1, c0
            elif a0 == a1 and c0 > c1:
                c0, c1 = c1, c0
            sr0 = max(r0, a0)
            sr1 = min(r1, a1, len(disp) - 1)
            if sr0 <= sr1:
                sel = QColor(0, 255, 90, 70)
                for rr in range(sr0, sr1 + 1):
                    line = disp[rr]
                    cc0 = c0 if rr == a0 else 0
                    cc1 = c1 if rr == a1 else len(line) - 1
                    p.fillRect(cc0 * self._char_w, rr * self._char_h,
                               (cc1 - cc0 + 1) * self._char_w, self._char_h, sel)

        # 光标块（只在本帧失效行内重画；光标移动由 _emit 带上 cursor.y）
        cur = self.screen.cursor
        if (self._blink and not getattr(cur, "hidden", False)
                and r0 <= cur.y <= r1):
            px = cur.x * self._char_w
            py = cur.y * self._char_h
            if 0 <= py < self.height() - self._char_h and 0 <= px < self.width():
                p.fillRect(px, py, self._char_w, self._char_h, GREEN)

        # 命令补全下拉（overlay，不写入 pyte buffer；出现/切换时调用者全屏 update）
        if self._sugs:
            self._sug_paint(p, cur.y * self._char_h, cur.x * self._char_w,
                            self._char_w, self._char_h)
        p.end()

    def _paint_scroll_view(self, p, rows_fit):
        """滚轮回看：视口 = 历史尾部 + 当前屏前面几行 组成的一整段，
        偏移 scroll_lines 行后取 rows_fit 行绘制。"""
        disp = self.screen.display
        buffer = self.screen.buffer
        hist = self.screen.history
        hist_rows = hist.top if hasattr(hist, "top") else hist
        hist_len = len(hist_rows)
        disp_len = len(disp)
        total = hist_len + disp_len
        end = max(0, total - self._scroll_lines)
        start = max(0, end - rows_fit)
        for i in range(start, end):
            y = i - start
            if i < hist_len:
                # 历史行：dict 列号→pyte.Char（含 ch/fg/bold）
                cells = hist_rows[i]
                self._paint_cells(p, y, cells, None)
            else:
                # 当前屏行：数据行号 d，绘制在视口行 y
                d = i - hist_len
                if d < len(disp):
                    self._paint_row(p, y, disp, buffer, row=d)

    def _paint_cells(self, p, y, cells, _buf):
        """按列号→pyte.Char 的 dict 逐段绘制（滚动视图的历史行用）。"""
        if not cells:
            return
        # dict（列号→Char）→ 排序列表
        if isinstance(cells, dict):
            items = sorted(cells.items())
        else:
            items = list(enumerate(cells))
        if not items:
            return
        n = len(items)
        i = 0
        while i < n:
            ci, cell = items[i]
            ch = getattr(cell, "data", "")   # pyte 0.8.2 Char 字段是 data，不是 ch！
            fg = getattr(cell, "fg", "default")
            bold = getattr(cell, "bold", False)
            seg = ch
            j = i + 1
            while j < n:
                nci, nc = items[j]
                nch = getattr(nc, "data", "")
                nfg = getattr(nc, "fg", "default")
                nbold = getattr(nc, "bold", False)
                if nfg == fg and nbold == bold and nci == ci + (j - i):
                    seg += nch
                    j += 1
                else:
                    break
            if seg.strip():
                p.setPen(self._fg_color(fg, bold))
                p.drawText(ci * self._char_w, y * self._char_h + self._char_h - 4, seg)
            i = j

    def _paint_row(self, p, y, disp, buffer, row=None):
        """绘制当前屏第 row 行（绘制在视口 y 行）。

        pyte 0.8.2 中文按 wcwidth 占 2 列：buffer 里该行是列号→Char 的
        dict，中文列后紧跟一个 data='' 的 stub 列。按列号遍历才能正确
        定位中文字符的绘制起点（display 字符串的字符索引会错位）。
        """
        if row is None:
            row = y
        if row >= len(disp):
            return
        buf = buffer.get(row)
        if buf is None:
            return
        items = sorted(buf.items())
        n = len(items)
        i = 0
        while i < n:
            ci, cell = items[i]
            ch = getattr(cell, "data", "")
            fg = getattr(cell, "fg", "default")
            bold = getattr(cell, "bold", False)
            seg = ch
            j = i + 1
            while j < n:
                nci, nc = items[j]
                nch = getattr(nc, "data", "")
                nfg = getattr(nc, "fg", "default")
                nbold = getattr(nc, "bold", False)
                if nfg == fg and nbold == bold and nci == ci + (j - i):
                    seg += nch
                    j += 1
                else:
                    break
            if seg.strip():
                p.setPen(self._fg_color(fg, bold))
                p.drawText(ci * self._char_w, y * self._char_h + self._char_h - 4, seg)
            i = j

    def _fg_color(self, fg, bold):
        base = GREEN if fg == "default" else fg
        if isinstance(base, tuple) and len(base) == 3:
            return QColor(*[max(0, min(255, int(v))) for v in base])
        if isinstance(base, str) and base not in ("default",):
            # pyte 0.8.2 的 fg 是形如 "00ff41" 的 6 位 hex（无 # 前缀）
            try:
                if len(base) == 6 or len(base) == 8:
                    return QColor("#" + base)
                return QColor(base)
            except Exception:
                return GREEN
        return GREEN if bold else GREEN

    # ---------- 命令循环 ----------
    def focusNextPrevChild(self, nextChild):
        # 终端在 QTabWidget 里：Tab 默认被拿去移动焦点/切标签，
        # 这里强制不让焦点转移，Tab 事件才会落到 keyPressEvent（补全用）。
        return False

    def inputMethodEvent(self, event):
        """中文 IME：提交的候选词逐字走 _type_char（普通 QWidget 默认
        收不到 commitString，这就是"不能输入中文"的根因）。"""
        commit = event.commitString()
        if commit:
            for ch in commit:
                self._type_char(ch)
            self.update()
        event.accept()

    def _cell_at(self, x, y):
        """像素 → (行, 列)；越界钳制到屏幕范围。"""
        ch = self._char_h or 1
        cw = self._char_w or 1
        r = int(y // ch)
        c = int(x // cw)
        r = max(0, min(r, self.ROWS - 1))
        c = max(0, min(c, self.COLS - 1))
        return r, c

    def _paste_text(self, text):
        # 多行粘贴 = 翻成多命令：首行及中间行立即执行，最后一行留在输入框；
        # \t 转 4 空格（pyte 里 Tab 跳列会错位）；不带换行的单行照旧逐字符输入。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        tail = lines[-1]
        for line in lines[:-1]:
            if line.strip():
                self._run(line)   # 完整命令循环（记历史 + 画提示符）
        for ch in tail.replace("\t", "    "):
            self._type_char(ch)

    def mousePressEvent(self, e):
        self.setFocus()   # 点击终端即获焦（否则按键/联想都收不到）
        if e.button() == Qt.MouseButton.RightButton:
            # 右键 = 粘贴（Windows Terminal 习惯）
            cb = QApplication.clipboard()
            self._paste_text(cb.text())
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            # 左键 = 开始拖选复制
            r, c = self._cell_at(e.position().x(), e.position().y())
            self._sel_anchor = (r, c)
            self._sel_end = (r, c)
            self._scroll_lines = 0   # 点击回到底部视图
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (e.buttons() & Qt.MouseButton.LeftButton) and self._sel_anchor is not None:
            r, c = self._cell_at(e.position().x(), e.position().y())
            self._sel_end = (r, c)
            self.update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._sel_anchor is not None:
            # 松开左键：把选中的文本放进剪贴板（左键拖选 = 复制）
            self._copy_selection()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def _copy_selection(self):
        if self._sel_anchor is None or self._sel_end is None:
            return
        disp = self.screen.display
        r0, c0 = self._sel_anchor
        r1, c1 = self._sel_end
        if (r0, c0) == (r1, c1):
            return
        if r0 > r1:
            r0, r1 = r1, r0
            c0, c1 = c1, c0
        elif r0 == r1 and c0 > c1:
            c0, c1 = c1, c0
        parts = []
        for rr in range(r0, r1 + 1):
            if rr >= len(disp):
                continue
            line = disp[rr]
            cc0 = c0 if rr == r0 else 0
            cc1 = c1 if rr == r1 else len(line) - 1
            parts.append("".join(line[cc0:cc1 + 1]).rstrip())
        text = "\n".join(p for p in parts if p)
        if text:
            QApplication.clipboard().setText(text)
        self._sel_anchor = None
        self._sel_end = None
        self.update()

    def wheelEvent(self, e):
        # 滚轮回看历史；pyte HistoryScreen 的 history 保留滚动缓冲
        delta = e.angleDelta().y()
        if delta > 0:
            self._scroll_lines += 1
        elif delta < 0:
            self._scroll_lines = max(0, self._scroll_lines - 1)
        # 上限：历史总行数 + 当前屏行数 - 一屏行数（至少 0）
        rows_fit = max(1, (self.height() // (self._char_h or 1)))
        hist = self.screen.history
        hist_len = len(hist.top) if hasattr(hist, "top") else 0
        max_scroll = max(0, hist_len + len(self.screen.display) - rows_fit)
        self._scroll_lines = min(self._scroll_lines, max_scroll)
        self._has_scroll = max_scroll > 0
        self.update()
        e.accept()

    def keyPressEvent(self, e):
        from PyQt6.QtGui import QKeySequence
        k = e.key()

        if k == Qt.Key.Key_Return or k == Qt.Key.Key_Enter:
            # 先存下命令再清空 self.cmd：否则 _run 末尾的 _show_prompt() 会把
            # 刚执行的命令再回显一遍（"为什么会出现两个指令"的根因）
            self._sugs = []
            self._sug_idx = 0
            self._sug_tmpl = []
            self._sug_kind = []
            cmd = self.cmd
            self.cmd = ""
            self._cx = 0
            self.hist_pos = -1
            self._scroll_lines = 0   # 回车回到底部视图
            # eDEX-UI：Enter = granted（键盘 Enter 键音）
            audio.play("granted", 0.5)
            self._run(cmd)
            return
        if k == Qt.Key.Key_Backspace:
            if self.cmd and self._cx > 0:
                # 删除光标前一个字符（支持光标在命令中间时删除）
                self.cmd = self.cmd[:self._cx - 1] + self.cmd[self._cx:]
                self._cx -= 1
                self._redraw_cmdline()
                self._update_sugs()
            return
        if k == Qt.Key.Key_Tab:
            e.accept()
            if self._sugs:
                self._sug_apply(keep=True)
                self._sug_nav(1)   # 推进选中，下次 Tab 填下一个候选
            else:
                self._complete()
            return
        if k == Qt.Key.Key_Backtab:
            # Shift+Tab：反向选择补全候选
            if self._sugs:
                self._sug_nav(-1)
            return
        if k == Qt.Key.Key_Up:
            if self._sugs:
                self._sug_nav(-1)
            else:
                self._hist_nav(-1)
            return
        if k == Qt.Key.Key_Down:
            if self._sugs:
                self._sug_nav(1)
            else:
                self._hist_nav(1)
            return
        if k == Qt.Key.Key_Left:
            if self._cx > 0:
                # 跨过光标左侧字符：按显示宽度后退（中文 2 列）
                ch = self.cmd[self._cx - 1]
                w = _disp_w(ch)
                self._cx -= 1
                self._emit(f"\x1b[{w}D")
            return
        if k == Qt.Key.Key_Right:
            if self._cx < len(self.cmd):
                ch = self.cmd[self._cx]
                w = _disp_w(ch)
                self._emit(f"\x1b[{w}C")
                self._cx += 1
            return
        if k == Qt.Key.Key_Home:
            if self._cx != 0:
                w = _disp_w(self.cmd[:self._cx])
                self._cx = 0
                self._emit(f"\x1b[{w}D")
            return
        if k == Qt.Key.Key_End:
            if self._cx != len(self.cmd):
                w = _disp_w(self.cmd[self._cx:])
                self._cx = len(self.cmd)
                self._emit(f"\x1b[{w}C")
            return
        if e.matches(QKeySequence.StandardKey.Paste):
            cb = QApplication.clipboard()
            self._paste_text(cb.text())
            return
        if k == Qt.Key.Key_C and (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            if self._mixer is not None:
                # matrix 特效运行中：Ctrl+C 退出特效，回到提示符
                self._matrix_stop()
                return
            # 中断挂起的流式分块输出（长输出命令 Ctrl+C 后不再继续喂剩余块）
            self._feed_cancel = True
            # 取消正在运行的外部命令线程（杀进程树，杜绝孤儿进程；
            # 不在 GUI 线程 wait——kill 后泵线程随管道 EOF 自然收尾）
            for j in list(self._cmd_jobs):
                if isinstance(j, _CmdRunner):
                    j._kill_tree()
            self._cmd_jobs = [j for j in self._cmd_jobs if j.isRunning()]
            self._emit("\r\n")
            self.cmd = ""
            self._cx = 0
            self._sugs = []
            self._sug_idx = 0
            self._sug_tmpl = []
            self._sug_kind = []
            self._show_prompt()
            return
        if e.text():
            self._kbd()
            self._type_char(e.text())

    def _type_char(self, ch):
        # 过滤控制字符
        if ord(ch) < 32 and ch not in "\t":
            return
        self._scroll_lines = 0   # 输入即回到底部视图
        if self._cx >= len(self.cmd):
            # 光标在行尾：直接追加（pyte 光标自然前进）
            self.cmd += ch
            self._emit(ch)
            self._cx = len(self.cmd)
        else:
            # 光标在命令中间：插入字符并重绘整行
            self.cmd = self.cmd[:self._cx] + ch + self.cmd[self._cx:]
            self._cx += 1
            self._redraw_cmdline()
        self._update_sugs()
        self.update()

    # ---------- 命令补全（VS Code / fish 风格） ----------
    # 候选命令池：内置命令 + 常用 Windows 命令 + Linux 别名，统一小写。
    _SUG_CMDS = [
        "cd", "dir", "ls", "pwd", "echo", "cat", "type", "clear", "cls",
        "help", "ps", "top", "tasklist", "taskkill", "ifconfig", "ip",
        "ipconfig", "ping", "tracert", "netstat", "whoami", "hostname",
        "date", "uname", "df", "free", "tree", "grep", "findstr", "curl",
        "whereami", "weather", "quote", "crypto", "matrix", "mkdir", "md",
        "rmdir", "rd", "del", "copy", "move", "ren", "set", "systeminfo",
        "ver", "net", "nslookup", "route", "shutdown", "chcp", "color",
        "title", "start", "exit", "vol", "path", "mklink", "xcopy", "edit",
    ]

    def _update_sugs(self):
        """根据当前输入实时计算补全候选（VS Code + inshellisense 风格）：
        - 无空格 → 命令名前缀/fuzzy 补全 + 完整示例模板；
        - 有空格 → 选项（-x 触发）/子命令/参数/文件 补全 + 模板。
        """
        cmd = self.cmd
        if not cmd:
            self._sugs = []
            self._sug_idx = 0
            self._sug_tmpl = []
            self._sug_kind = []
            self.update()
            return
        parts = cmd.split(" ")
        if len(parts) == 1:
            # 首 token：命令名补全 + 该命令的完整示例（VS Code 风格混排）
            p = cmd.lower()
            hits = [c for c in self._SUG_CMDS if c.startswith(p) and c != p]
            # fuzzy 次选：子序列匹配（fzf 风格，如 "tl" → tasklist/title），排序靠后
            if not hits:
                hits = [c for c in self._SUG_CMDS
                        if c != p and _subseq(p, c)]
            # 保持稳定顺序：常用命令优先（ps 不置顶，避免输入 p 时抢在 ping/pwd 前）
            hits.sort(key=lambda c: (0 if c in ("cd", "dir", "ls", "help", "cat", "clear", "cls") else 1, c))
            names = hits[:6]
            # 完整示例：当前输入前缀命中的命令模板（如 "ping" → "ping 8.8.8.8 -n 4"）
            pool = names + ([cmd.lower()] if cmd.lower() in self._SUG_CMDS else [])
            tmpls = []
            for c in pool:
                for ex, _d in _CMD_EXAMPLES.get(c, []):
                    if ex.lower().startswith(p) and ex.lower() != p:
                        tmpls.append(ex)
            self._sugs = names + tmpls[:2]
            self._sug_tmpl = [False] * len(names) + [True] * len(tmpls[:2])
            self._sug_kind = ["cmd"] * len(names) + ["tmpl"] * len(tmpls[:2])
        else:
            # 有空格：选项/子命令/参数 + 完整写法模板
            first = parts[0].lower()
            cur = parts[-1]   # 正在输入的最后一个 token（不转小写，文件区分大小写）
            cur_l = cur.lower()
            # 递归子命令 key：如 "net use" → 找 "net use" 的 spec
            spec_key = first
            for k in range(len(parts) - 1, 0, -1):
                key = " ".join(parts[:k]).lower()
                if key in _CMD_SPECS:
                    spec_key = key
                    break
            spec = _CMD_SPECS.get(spec_key)
            args = []
            kind = []
            if spec and (cur.startswith("-") or cur.startswith("/")):
                # 选项补全：排除已完整输入的选项（parts[:-1]），当前 partial 保留
                used_opts = {t.lower() for t in parts[:-1]
                             if t.startswith("-") or t.startswith("/")}
                for o, d in spec["opts"]:
                    if o.lower() in used_opts:
                        continue
                    # Windows 命令用 / 前缀，Linux 风格用 - 前缀；打 - 也触发 / 选项
                    if o.lower().startswith(cur_l) or (
                            cur_l in ("-", "/") and (o.startswith("-") or o.startswith("/"))):
                        args.append(o)
                        kind.append("opt")
            elif spec and spec["subs"]:
                # 子命令补全：如 net → use/start/stop
                for s, d in spec["subs"]:
                    if s.lower().startswith(cur_l) and s != cur_l:
                        args.append(s)
                        kind.append("sub")
            if not args:
                # 参数/文件补全（_arg_hits 返回 (候选, kind) 元组）
                a2 = self._arg_hits(first, cur_l)
                for name, kd in a2[:6]:
                    args.append(name)
                    kind.append(kd)
            # 命令模板：完整命令示例（若当前输入是该示例的前缀则给出）
            tmpls = []
            for ex, desc in _CMD_EXAMPLES.get(first, []):
                if ex.lower().startswith(cmd.lower()) and ex.lower() != cmd.lower():
                    tmpls.append((ex, desc))
            self._sugs = args[:6] + [ex for ex, _d in tmpls[:2]]
            self._sug_tmpl = [False] * len(args[:6]) + [True] * len(tmpls[:2])
            self._sug_kind = kind[:6] + ["tmpl"] * len(tmpls[:2])
        self._sug_idx = 0
        self.update()

    def _arg_hits(self, first, cur):
        """参数联想：first=首命令，cur=正在输入的参数前缀。
        返回 (候选文本, kind) 列表：file/folder=当前目录真文件名，arg=固定参数池。
        cur 为空（刚打完空格）时给出该命令的完整参数表。"""
        if first in _FILE_CMDS:
            try:
                names = sorted(os.listdir(self.cwd))
            except OSError:
                names = []
            out = []
            for n in names:
                if n.lower().startswith(cur):
                    full = os.path.join(self.cwd, n)
                    isdir = os.path.isdir(full)
                    out.append((n + ("/" if isdir else ""), "folder" if isdir else "file"))
            # 固定候选优先（如 type 的 nul/con）
            extra = [s for s in _ARG_SUGS.get(first, [])
                     if s.startswith(cur) and s not in [o[0] for o in out]]
            return [(s, "arg") for s in extra][:2] + out[:10]
        if first in ("help", "man"):
            return [(c, "cmd") for c in self._SUG_CMDS
                    if c.startswith(cur) and c != cur]
        if first in _ARG_SUGS:
            return [(s, "arg") for s in _ARG_SUGS[first]
                    if s.startswith(cur) and s != cur]
        return []

    def _sug_nav(self, d):
        if not self._sugs:
            return
        self._sug_idx = (self._sug_idx + d) % len(self._sugs)
        self.update()

    def _sug_apply(self, keep=False):
        """把当前选中候选填入命令行（Tab 循环 / 回车选中）。"""
        if not self._sugs:
            return
        i = self._sug_idx
        w = self._sugs[i]
        is_tmpl = bool(self._sug_tmpl) and self._sug_tmpl[i] if i < len(self._sug_tmpl) else False
        if is_tmpl:
            # 命令模板：整条完整示例直接替换当前输入（不用背语法）
            self.cmd = w
        elif " " in self.cmd:
            # 首次 Tab 补全后 _sugs 保留、_sug_nav(1) 推进，下一轮 Tab 进来时
            # cmd 已经带上候选（如 "ps "），此时不能再 head+空格+w 拼接成 "ps ping"；
            # 改为替换首 token（命令名本身），参数候选才走拼接。
            parts = self.cmd.split(" ", 1)
            if w in self._SUG_CMDS:
                self.cmd = w + " "
            else:
                self.cmd = parts[0] + " " + w
        else:
            # VS Code 风格：完整命令名后自动补一个空格，方便直接敲参数
            self.cmd = (w + " ") if w in self._SUG_CMDS else w
        self._emit("\r\x1b[K")
        self._show_prompt(no_nl=True)
        self._cx = len(self.cmd)   # 补全后光标到行尾
        self.update()
        if not keep:
            self._sugs = []
            self._sug_idx = 0
            self._sug_tmpl = []
            self._sug_kind = []

    def _sug_paint(self, p, y, x, char_w, char_h):
        """绘制补全下拉：提示符下方一行一个候选，选中项高亮。
        VS Code/inshellisense 风格：每种候选类型带前缀图标与专属颜色：
        cmd=命令 ▸ / opt=选项 - / sub=子命令 ▣ / arg=参数 · / tmpl=模板 ⚡（金色）。"""
        if not self._sugs:
            return
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor, QPen, QFont
        from wcwidth import wcwidth
        n = len(self._sugs)
        # 类型图标：cmd=▸ opt=- sub=▣ arg=· tmpl=⚡
        _ICONS = {"cmd": "\u25b8", "opt": "-", "sub": "\u25a3",
                  "arg": "\u00b7", "tmpl": "\u26a1", "file": "\u00b7",
                  "folder": "\u25a3"}
        kinds = []
        for i, s in enumerate(self._sugs):
            if self._sug_tmpl and i < len(self._sug_tmpl) and self._sug_tmpl[i]:
                kinds.append("tmpl")
            else:
                kinds.append(self._sug_kind[i] if i < len(self._sug_kind) else "arg")
        # 显示宽度按 wcwidth 累加（中文候选占 2 列），len() 会算错
        wcmd = max(sum(max(wcwidth(c), 1) for c in s) for s in self._sugs)
        descs = []
        for i, s in enumerate(self._sugs):
            if kinds[i] == "tmpl":
                # 命令模板：说明取模板自身的 desc
                for ex, d in _CMD_EXAMPLES.get(s.split(" ", 1)[0].lower(), []):
                    if ex == s:
                        descs.append(d)
                        break
                else:
                    descs.append("")
            elif kinds[i] == "opt":
                # 选项：说明从命令 spec 的 opts 找（如 ping -t → 持续 Ping）
                descs.append(_spec_desc(s))
            elif kinds[i] == "sub":
                # 子命令：说明从命令 spec 的 subs 找（如 net use → 连接共享）
                descs.append(_spec_desc(s))
            else:
                descs.append(_sug_desc(s))
        wdesc = max((sum(max(wcwidth(c), 1) for c in d) for d in descs if d), default=0)
        w_cells = wcmd + 2 + (wdesc + 1 if wdesc else 0) + 2   # +2 放图标
        w_px = min(self.width() - 4, w_cells * char_w + 12)
        h_px = n * char_h + 8
        bx = x
        by = y + char_h + 2
        if by + h_px > self.height() - 4:
            by = max(4, y - h_px - 2)   # 下方放不下就放上方
        # 半透明深色面板（背景/边框/选中行跟随当前主题色）
        p.fillRect(bx, by, w_px, h_px,
                   QColor(PANEL_BG.red(), PANEL_BG.green(), PANEL_BG.blue(), 220))
        p.setPen(QPen(QColor(GREEN.red(), GREEN.green(), GREEN.blue(), 160), 1))
        p.drawRect(bx, by, w_px, h_px)
        f = QFont(self.font())
        f.setPointSizeF(self._char_h * 0.62)
        p.setFont(f)
        for i, s in enumerate(self._sugs):
            cy = by + 4 + i * char_h
            if i == self._sug_idx:
                p.fillRect(bx + 1, cy, w_px - 2, char_h, QColor(0, 90, 35, 230))
            k = kinds[i]
            # 选中/未选中颜色对
            if k == "tmpl":
                col = QColor(255, 200, 60) if i == self._sug_idx else QColor(200, 150, 40)
            elif k == "opt":
                col = QColor(0, 220, 255) if i == self._sug_idx else QColor(0, 150, 190)
            elif k == "sub":
                col = QColor(200, 120, 255) if i == self._sug_idx else QColor(150, 80, 190)
            elif k == "cmd":
                col = QColor(0, 255, 90) if i == self._sug_idx else QColor(0, 170, 60)
            else:
                col = QColor(0, 255, 90) if i == self._sug_idx else QColor(0, 170, 60)
            p.setPen(QPen(col, 1))
            # 类型图标 + 候选文本
            p.drawText(QPointF(bx + 6, cy + char_h * 0.78), _ICONS.get(k, "\u00b7"))
            p.drawText(QPointF(bx + 8 + char_w, cy + char_h * 0.78), s)
            desc = descs[i]
            if desc:
                p.setPen(QPen(QColor(120, 140, 130) if i != self._sug_idx else QColor(150, 230, 170), 1))
                p.drawText(QPointF(bx + 8 + (wcmd + 3) * char_w + char_w, cy + char_h * 0.78), desc)

    def _redraw_cmdline(self):
        # 重绘当前输入行：回到行首、清行、重打提示符+命令，再把光标放回 _cx 处
        self._emit("\r\x1b[K")
        self._show_prompt(no_nl=True)
        # 光标现在在整行末尾，退回到 _cx 对应的显示列
        back = _disp_w(self.cmd) - _disp_w(self.cmd[:self._cx])
        if back > 0:
            self._emit(f"\x1b[{back}D")

    def _show_prompt(self, no_nl=False):
        cwd_short = self.cwd.replace(os.path.expanduser("~"), "~")
        p = _prompt_ansi() + cwd_short + PROMPT_END
        if no_nl:
            p = p
        self._emit(p)
        if self.cmd:
            self._emit(self.cmd)
        self.update()

    def _hist_nav(self, d):
        if not self.history:
            return
        # 方向：Up(-1) = 向更旧翻；Down(+1) = 向更新翻。
        # history 按时间序 append，最后一条最新；hist_pos 从 -1 起，
        # -1 表示"未翻"，0 表示最新一条，往后越大越旧（故 Up 应增大 hist_pos）。
        if self.hist_pos == -1 and d < 0:
            self._hist_orig = self.cmd   # 第一次按 Up：暂存未发送的输入
            self.hist_pos = 0            # 直接落到最新一条
        else:
            self.hist_pos -= d           # Up(-1)→+1 更旧；Down(+1)→-1 更新
        # 越界钳制：Up 到最旧停在 len-1；Down 越过 -1 回到未翻状态
        if self.hist_pos > len(self.history) - 1:
            self.hist_pos = len(self.history) - 1
        if self.hist_pos < -1:
            self.hist_pos = -1
        if self.hist_pos == -1:
            self.cmd = self._hist_orig
        else:
            self.cmd = self.history[len(self.history) - 1 - self.hist_pos]
        self._cx = len(self.cmd)
        self._emit("\r\x1b[K")
        self._show_prompt(no_nl=True)
        self._update_sugs()   # 历史命令也按前缀刷新补全候选

    def _complete(self):
        """Tab 补全：文件/目录名。"""
        head = self.cmd.rsplit(" ", 1)
        prefix = head[-1] if len(head) > 1 else ""
        full = os.path.join(self.cwd, prefix)
        basedir = os.path.dirname(full) if os.path.dirname(prefix) else self.cwd
        base = os.path.basename(prefix) if prefix else ""
        try:
            names = sorted(os.listdir(basedir))
        except OSError:
            return
        hits = [n for n in names if n.startswith(base)]
        if len(hits) == 1:
            # 命中唯一：保留用户已输入的目录部分（prefix 含目录时），
            # 否则只给 basename —— 避免 `cd D:\...\uni<Tab>` 丢目录变 `cd unique_target.txt`
            d = os.path.dirname(prefix)
            full_hit = os.path.join(d, hits[0]) if d else hits[0]
            self.cmd = (head[0] + " " if len(head) > 1 else "") + full_hit
            self._cx = len(self.cmd)
            # 刷新整行（清掉残留前缀再写完整路径），避免 "cd C:\WinWindows" 式重影
            self._emit("\r\x1b[K")
            self._show_prompt(no_nl=True)
        elif len(hits) > 1:
            self._emit("\r\n" + "  ".join(hits) + "\r\n")
            self._show_prompt(no_nl=True)

    def _run(self, cmdline: str):
        self._emit("\r\n")
        cmd = cmdline.strip()
        if not cmd:
            self._show_prompt()
            return
        self.history.append(cmd)

        # Linux 别名 → Windows 真命令
        first = cmd.split()[0].lower()
        if first in CMD_ALIAS:
            mapped = CMD_ALIAS[first] + cmd[len(first):]
            self._run_external(mapped)
            return

        # ping：把 Linux 风格参数翻成 Windows 的（-c 次数 → -n 次数，
        # 大小写不敏感；Windows ping 不认 -c 会直接报错）
        if first == "ping":
            import re as _re
            cmd = _re.sub(r"(?i)\s-c\s+(\d+)", r" -n \1", cmd)
            self._run_external(cmd)
            return

        if cmd == "help":
            self._emit(self._help_text())
        elif cmd in ("cls", "clear"):
            self._emit("\x1b[2J\x1b[H")
        elif cmd == "theme" or cmd.startswith("theme "):
            self._do_theme(cmd[6:].strip())
        elif cmd == "sound" or cmd.startswith("sound "):
            self._do_sound(cmd[6:].strip())
        elif cmd == "edit" or cmd.startswith("edit "):
            self._do_edit(cmd[5:].strip())
        elif cmd == "cd" or cmd.startswith("cd "):
            # 严格匹配："cdx" 之类不应被当成 cd；裸 "cd" 回用户主目录
            self._do_cd(cmd[3:].strip())
        elif cmd in ("dir", "ls"):
            self._do_dir()
        elif cmd == "cat" or cmd.startswith("cat "):
            self._do_cat(cmd[4:].strip())
        elif cmd == "pwd":
            self._emit(self.cwd + "\r\n")
        elif cmd == "echo" or cmd.startswith("echo "):
            # cmd[5:] = 去掉 "echo " 前缀（len("echo ")==5），不留前导空格
            self._do_echo(cmd[5:])
        elif cmd == "whereami":
            self._do_whereami()
        elif cmd == "weather" or cmd.startswith("weather "):
            self._do_weather(cmd[8:].strip())
        elif cmd == "quote":
            self._do_quote()
        elif cmd == "crypto" or cmd.startswith("crypto "):
            self._do_crypto(cmd[7:].strip())
        elif cmd == "matrix":
            self._matrix_start()
        elif first in ("whoami", "hostname", "date", "uname", "df", "free", "tree"):
            self._syscmd(first, cmd)
        else:
            self._run_external(cmd)
            return   # 外部命令异步执行，提示符由 _on_cmd_done 收尾时接

        # 补一个空行再加提示符，除非已经结束在提示符
        if self._mixer is None:   # matrix 特效运行时不画提示符
            self._show_prompt()

    def _syscmd(self, name, cmd):
        """真数据内置命令（不依赖第三方壳，全本机实时数据）。"""
        import platform as _platform
        if name == "whoami":
            self._emit(f"{os.environ.get('USERNAME','?')}\r\n")
        elif name == "hostname":
            self._emit(f"{_platform.node()}\r\n")
        elif name == "date":
            self._emit(time.strftime("%Y-%m-%d %H:%M:%S") + "\r\n")
        elif name == "uname":
            self._emit(
                f"OS={_platform.system()} {_platform.release()} {_platform.machine()}\r\n"
                f"python={_platform.python_version()}  "
                f"cpu={_platform.processor()}\r\n")
        elif name == "df":
            try:
                import psutil
                parts = []
                for d in psutil.disk_partitions(all=False):
                    try:
                        u = psutil.disk_usage(d.mountpoint)
                        parts.append(f"{d.mountpoint:<5} {u.total//2**30:>5}G "
                                     f"{u.used//2**30:>5}G {u.free//2**30:>5}G "
                                     f"{u.percent:>4.0f}%  {d.fstype}")
                    except OSError:
                        continue
                self._emit("\r\n".join(parts) + "\r\n")
            except Exception as e:
                self._emit(f"[ERR] {e}\r\n")
        elif name == "free":
            try:
                import psutil
                v = psutil.virtual_memory()
                s = psutil.swap_memory()
                self._emit(
                    f"              total     used     free    shared  avail%\r\n"
                    f"mem  {v.total//2**20:>8}M {v.used//2**20:>7}M "
                    f"{v.free//2**20:>7}M {v.shared//2**20:>7}M {v.percent:>6.0f}\r\n"
                    f"swap {s.total//2**20:>8}M {s.used//2**20:>7}M "
                    f"{s.free//2**20:>7}M\r\n")
            except Exception as e:
                self._emit(f"[ERR] {e}\r\n")
        elif name == "tree":
            self._do_tree()

    def _do_tree(self, maxdepth=2, maxfiles=60):
        """目录树：真数据，限制深度与条数（长输出是 pyte 卡顿的诱因之一）。"""
        lines = [self.cwd]
        cnt = 0
        def walk(d, depth, prefix):
            nonlocal cnt
            try:
                items = sorted(os.listdir(d))
            except OSError:
                return
            dirs = [i for i in items if os.path.isdir(os.path.join(d, i))]
            files = [i for i in items if not os.path.isdir(os.path.join(d, i))]
            for i, sub in enumerate(dirs):
                if cnt >= maxfiles:
                    return
                last = (i == len(dirs) - 1 and not files)
                lines.append(prefix + ("└── " if last else "├── ") + sub)
                if depth + 1 < maxdepth:
                    cnt += 1
                    walk(os.path.join(d, sub), depth + 1,
                         prefix + ("    " if last else "│   "))
            for i, f in enumerate(files[:8]):
                if cnt >= maxfiles:
                    return
                lines.append(prefix + ("└── " if i == len(files[:8]) - 1 else "├── ") + f)
                cnt += 1
        walk(self.cwd, 0, "")
        self._emit("\n".join(lines) + "\r\n")

    def _run_external(self, cmd):
        """外部命令：后台线程执行，输出分块喂给 pyte，不冻结 GUI。"""
        self._feed_cancel = False   # 新命令开始，重置 Ctrl+C 中断标志
        w = _CmdRunner(cmd, self.cwd, self)
        self._cmd_jobs.append(w)
        w.output.connect(self._on_cmd_output)
        w.done.connect(self._on_cmd_done)
        w.start()

    def _on_cmd_output(self, text, is_err):
        """流式输出块：边跑边喂给 pyte（不等到进程结束）。"""
        if self._closed or self._feed_cancel:
            return
        if is_err:
            # stderr 画在红色段
            self._stream_feed(f"{_esc(RED)}{text}\x1b[0m")
        else:
            self._stream_feed(text)

    def _on_cmd_done(self, rc, out, err):
        try:
            if err and rc == -2:
                audio.play("error", 0.5)
                self._emit(f"{_esc(RED)}[ERR] {err}\x1b[0m\r\n")
                self._show_prompt()
                return
            audio.play("stdout", 0.5)
            if self._mixer is None:
                self._show_prompt()
        finally:
            # 只保留还在跑的线程引用
            self._cmd_jobs = [j for j in self._cmd_jobs if j.isRunning()]

    def _stream_feed(self, text, done=None):
        """分块喂给 pyte（串行队列版）：所有输出块先入 _feed_q FIFO，
        由 __init__ 起的唯一一条 Ticker 链逐块取、逐块喂，保证块顺序严格、
        绝不交错。每块 ~2KB，块间 singleShot(0) 让出事件循环，长输出时
        界面持续刷新、不冻结。done() 在本段输出全部喂完后调用。"""
        if not text:
            if done:
                done()
            return
        buf = text  # stderr 的红色包装在 _on_cmd_output 完成
        self._feed_q.append((buf, done))
        # 追不上海量输出（如 a.py 无限刷屏）时：丢中间旧块，只留
        # 正在喂的块 + 最新块，避免队列无限膨胀、界面越看越滞后。
        if len(self._feed_q) > self._feed_max_q:
            keep = self._feed_q[:1] + self._feed_q[-1:]
            for _, d in self._feed_q[1:-1]:
                if d:
                    d()
            self._feed_q = keep
        self._start_feed_tick()

    def _start_feed_tick(self):
        """若还没有链在跑，启动唯一一条 Ticker 链（pacer 驱动）。"""
        if self._feed_running or not self._feed_q:
            return   # 已有链在跑：新块入队即可，当前链喂完会自然接上
        self._feed_running = True
        self._feed_pacer.start()

    def _feed_tick(self):
        """pacer 驱动批量喂给 pyte：每拍喂一块 4KB 后交给下 8ms 拍，让出事件循环。

        旧版两个失败点：a) BUDGET=32KB，让出频率约 8.5 次/s → 动画只有
        ~8fps，一顿一顿；b) 让出想靠 QTimer(0) 单发续链——实测这种
        0 间隔定时器在事件循环忙碌（泵线程 833 次/s 输出信号 + paint
        整屏 2ms）时几乎不触发（200 次只触发 1 次），链僵死、消费速率
        只剩 35KB/s，队列爆掉丢中间帧。改周期 pacer：8ms 一拍稳定
        ~125Hz，每拍解析 4KB（fast_feed ~0.8MB/s ≈ 5ms）并留 ~3ms 给
        paint/输入，吞吐 ~512KB/s 远超动画输出 274KB/s（60fps×4KB/帧），
        动画帧率跟随输出。段间切换不递归、不重复 update。"""
        CH = 4096                      # 单次 _emit 的最大块
        BUDGET = 4096                  # 每拍最多喂 4KB（≈动画一帧）再让出
        # ---- 积压帧对齐：消费跟不上产出时，中间帧毫无意义 ----
        # 无限刷屏动画（彩虹甜甜圈等）每帧以 \x1b[H 归位 + 全屏重绘
        # （90×40 字符，~2-4KB/帧）。积压时只保留最后一个这样的帧头，
        # 丢弃之前全部旧帧残余——动画帧率 = pacer 触发率而非队列吞吐，
        # CPU 只解析最新一帧。阈值 2000B 保证只对「整屏重绘帧」生效：
        # 滚动日志/进度条里偶发的 \x1b[H（后文 <2KB）不会被误当帧边界
        # 丢内容。无匹配 → 走正常喂块路径，不丢任何字节。
        if len(self._feed_q) > 1:
            blob = self._feed_pending + "".join(t for t, _ in self._feed_q)
            cut = blob.rfind("\x1b[H")
            if cut >= 0 and len(blob) - cut > 2000:
                blob = blob[cut:]
                self._feed_pending = ""
                for _, d in self._feed_q:
                    if d:
                        d()
                self._feed_q = []
                self._feed_pos = 0
                if blob:
                    self._feed_q.append((blob, None))
        fed = 0
        while True:
            if self._closed or self._feed_cancel:
                self._feed_q.clear()
                self._feed_running = False
                self._feed_pacer.stop()
                self.update()
                return
            if not self._feed_q:
                self._feed_running = False
                self._feed_pacer.stop()
                self.update()
                return
            if fed >= BUDGET:
                # 本拍预算用完：等 pacer 下 8ms 拍再续，让事件循环跑 paint/输入
                return
            # 把队列积压合并成 ≤BUDGET 的大块一次 _emit：小碎块（泵线程
            # 每块 ~330B）逐个 fast_feed 有 ~1.8ms/次固定开销，12 块一拍
            # 实测 21.6ms；合并后一拍 5ms。拼接内部是连续字节流，跨 _emit
            # 边界才需要 _feed_pending，语义不变。
            take = []
            take_n = 0
            while fed < BUDGET and self._feed_q:
                t, d = self._feed_q[0]
                total = len(t)
                if self._feed_pos >= total:
                    # 本块喂完：弹出并继续处理下一块
                    self._feed_q.pop(0)
                    self._feed_pos = 0
                    if d:
                        d()
                    continue
                n = min(total - self._feed_pos, BUDGET - fed)
                take.append(t[self._feed_pos:self._feed_pos + n])
                take_n += n
                self._feed_pos += n
                fed += n
                if self._feed_pos < total:
                    break      # 队首块没取完，剩下等下一拍
            if take_n:
                self._emit("".join(take))

    def _help_text(self):
        G = _esc(GREEN)
        Z = "\x1b[0m"
        def row(prefix, names):
            return G + ("%-10s" % prefix) + Z + "  " + "  ".join(names) + "\r\n"
        return (
            "\r\n" + G + "  BL4CK://SHELL — NEON CONSOLE HELP\x1b[0m\r\n"
            "\r\n"
            + row("file", ["cd", "dir/ls", "pwd", "cat/type", "tree", "echo"])
            + row("system", ["whoami", "hostname", "date", "uname", "df", "free", "ver"])
            + row("proc", ["ps/top", "tasklist", "taskkill", "systeminfo"])
            + row("net", ["whereami", "weather [city]", "quote", "crypto [coin]",
                          "ping", "netstat", "ifconfig", "tracert", "nslookup"])
            + row("shell", ["cls/clear", "help", "matrix", "curl", "grep/findstr"])
            + row("win", ["mkdir/md", "rmdir/rd", "del", "copy", "move", "ren",
                          "set", "net", "route", "shutdown", "chcp", "color",
                          "title", "start", "exit", "vol", "path", "xcopy", "mklink"])
            + row("theme", ["theme [name]", "sound on|off"])
            + f"{_esc(AMBER)}  hint:\x1b[0m 输入命令首字母 → 下方弹出候选，"
              "Tab/Shift+Tab 循环补全，↑↓ 选择\r\n"
            + f"{_esc(AMBER)}  mouse:\x1b[0m 左键拖选复制 / 右键粘贴 / "
              "滚轮回看历史\r\n"
            + f"{_esc(AMBER)}  alias:\x1b[0m 任意未识别命令直接透传 Windows cmd"
              "（ipconfig/ping/tasklist/... 全可用）\r\n"
        )

    def _do_theme(self, name):
        """theme / theme <name>：列出或切换 eDEX-UI 主题。"""
        if not name:
            names = theme_names()
            cur_name = getattr(hub, "_cur", "matrix")
            head = _esc(GREEN) + " themes:" + "\x1b[0m\r\n"
            body = "  ".join(
                (_esc(GREEN) if n == cur_name else _esc(GREEN_DIM)) + n + "\x1b[0m"
                for n in names
            )
            self._emit(head + body + "\r\n")
            return
        if name not in theme_names():
            audio.play("error", 0.4)
            self._emit(f"{_esc(RED)}[ERR] no such theme: {name}\x1b[0m\r\n")
            return
        if not apply_theme(name):
            self._emit(f"{_esc(RED)}[ERR] theme load failed\x1b[0m\r\n")
            return
        hub._cur = name
        audio.play("theme", 0.5)
        self._emit(f"{_esc(GREEN)}theme -> {name}\x1b[0m\r\n")

    def _do_sound(self, arg):
        """sound / sound on|off：查看/切换音效总开关。"""
        if arg == "on":
            audio.set_enabled(True)
            audio.play("info", 0.4)
            self._emit(f"{_esc(GREEN)}sound -> on\x1b[0m\r\n")
        elif arg == "off":
            audio.set_enabled(False)
            self._emit(f"{_esc(GREEN_DIM)}sound -> off\x1b[0m\r\n")
        else:
            st = "ON" if audio.is_enabled() else "OFF"
            self._emit(f"{_esc(GREEN)}sound -> {st}  (sound on|off)\x1b[0m\r\n")

    def _do_edit(self, path):
        """edit <路径>：在编辑器标签打开/新建文件（相对 cwd 解析）。"""
        if not path:
            self._emit(f"{_esc(GREEN_DIM)}usage: edit <path>\x1b[0m\r\n")
            return
        nd = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        nd = os.path.abspath(nd)
        if os.path.isdir(nd):
            audio.play("error", 0.4)
            self._emit(f"{_esc(RED)}[ERR] {path} is a directory\x1b[0m\r\n")
            return
        audio.play("folder", 0.4)
        self._emit(f"{_esc(GREEN)}open in editor -> {nd}\x1b[0m\r\n")
        self.open_file.emit(nd)

    def repaint_prompt(self):
        """主题切换后由 main 调用：重画当前提示符行（用新主题色）。"""
        if not self.isVisible():
            return
        try:
            self._emit("\r\x1b[K")
            self._show_prompt(no_nl=True)
            if self.cmd:
                self._emit(self.cmd)
        except Exception:
            pass

    def _do_cd(self, path):
        if not path:
            return
        nd = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        nd = os.path.abspath(nd)
        if os.path.isdir(nd):
            self.cwd = nd
            self.cwd_changed.emit(nd)
            audio.play("folder", 0.4)
            self._emit(f"{_esc(GREEN)}path -> {nd}\x1b[0m\r\n")
        else:
            audio.play("error", 0.4)
            self._emit(f"{_esc(RED)}[ERR] no such dir: {path}\x1b[0m\r\n")

    def _do_dir(self):
        try:
            entries = sorted(os.listdir(self.cwd))
        except OSError as e:
            self._emit(f"[ERR] {e}\r\n")
            return
        rows = []
        for i in range(0, len(entries), 4):
            rows.append("  ".join(e.ljust(20) for e in entries[i:i + 4]))
        self._emit("\r\n".join(rows) + "\r\n")

    def _do_cat(self, path):
        """内置 cat：读文本文件原样输出（无 type 命令的换行/表头噪音）。"""
        if not path:
            self._emit("[ERR] usage: cat <file>\r\n")
            return
        p = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                self._emit(f.read())
            self._emit("\r\n")
        except OSError as e:
            self._emit(f"[ERR] {e}\r\n")

    def _do_echo(self, text):
        self._emit(text + "\r\n")

    # ---------- 网络命令（后台线程，不卡界面）----------
    def _net(self, kind, url):
        w = _NetWorker(kind, url, self)
        self._net_jobs = [j for j in self._net_jobs if j.isRunning()]
        self._net_jobs.append(w)
        w.result.connect(self._on_net)
        self._emit(f"{_esc(GREEN_DIM)}  [{kind}] requesting ...\x1b[0m\r\n")
        w.start()

    def _on_net(self, kind, text):
        # 异步结果回来时提示符已经画过了：先断行，结果画完再补提示符
        #（否则结果首行会粘在 "➜ " 后面，且之后输入没有提示符）
        self._emit("\r\n")
        try:
            if text.startswith("__ERR__"):
                self._emit(f"{_esc(RED)}[ERR] {kind}: {text[7:]}\x1b[0m\r\n")
                return
            if kind == "whereami":
                self._net_whereami(text)
            elif kind == "weather":
                self._net_weather(text)
            elif kind == "quote":
                self._net_quote(text)
            elif kind == "crypto":
                self._net_crypto(text)
        except Exception as e:
            self._emit(f"{_esc(RED)}[ERR] {kind} parse: {e}\x1b[0m\r\n")
        finally:
            self._show_prompt()

    def _do_whereami(self):
        url = ("http://ip-api.com/json/?fields="
               "status,country,city,lat,lon,isp,query")
        self._net("whereami", url)

    def _net_whereami(self, text):
        d = json.loads(text)
        if d.get("status") != "success":
            self._emit("[ERR] geo lookup failed\r\n")
            return
        lat, lon = d.get("lat", 0.0), d.get("lon", 0.0)
        city = d.get("city", "?")
        ip = d.get("query", "?")
        audio.play("info", 0.4)
        self._emit(
            f"{_esc(GREEN)}  public ip  : {ip}\r\n"
            f"  country    : {d.get('country','?')}\r\n"
            f"  city       : {city}\r\n"
            f"  lat / lon  : {lat} / {lon}\r\n"
            f"  isp        : {d.get('isp','?')}\x1b[0m\r\n")
        try:
            self.located.emit(float(lat), float(lon), str(city), str(ip))
        except Exception:
            pass

    def _do_weather(self, city):
        if city:
            url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=j1"
        else:
            url = "https://wttr.in/?format=j1"
        self._net("weather", url)

    def _net_weather(self, text):
        d = json.loads(text)
        cc = d["current_condition"][0]
        area = d.get("nearest_area", [{}])[0]
        name = area.get("areaName", [{}])[0].get("value", "?")
        desc = cc.get("weatherDesc", [{}])[0].get("value", "?")
        self._emit(
            f"  {name} : {desc}\r\n"
            f"  temp       : {cc.get('temp_C','?')} C  "
            f"(feels {cc.get('FeelsLikeC','?')})\r\n"
            f"  humidity   : {cc.get('humidity','?')}%\r\n"
            f"  wind       : {cc.get('windspeedKmph','?')} km/h "
            f"{cc.get('winddir16Point','?')}\r\n")

    def _do_quote(self):
        # zenquotes: [{q, a, h}]；之前用过的 official-joke-api 在本网络超时
        self._net("quote", "https://zenquotes.io/api/random")

    def _net_quote(self, text):
        d = json.loads(text)
        row = d[0] if isinstance(d, list) else d
        self._emit(f"  \x1b[38;2;135;255;175m'{row.get('q','?')}'\x1b[0m\r\n"
                   f"      - {row.get('a','?')}\r\n")

    def _do_crypto(self, sym):
        pairs = {
            "btc": "BTC_USDT", "eth": "ETH_USDT", "sol": "SOL_USDT",
            "doge": "DOGE_USDT", "ada": "ADA_USDT", "xrp": "XRP_USDT",
            "bnb": "BNB_USDT", "ltc": "LTC_USDT",
        }
        pair = pairs.get((sym or "btc").lower(), "BTC_USDT")
        url = ("https://api.gateio.ws/api/v4/spot/tickers"
               f"?currency_pair={urllib.parse.quote(pair)}")
        self._net("crypto", url)

    def _net_crypto(self, text):
        d = json.loads(text)
        for row in d if isinstance(d, list) else [d]:
            pair = row.get("currency_pair", "?")
            try:
                price = float(row.get("last", 0))
            except (TypeError, ValueError):
                price = 0.0
            self._emit(f"  {pair:<12} $ {price:,.2f}\r\n")

    # ---------- matrix 特效 ----------
    def _matrix_start(self):
        self._emit(f"\r\n{_esc(GREEN)}[matrix] Ctrl+C to exit\x1b[0m\r\n")
        self._mixer = QTimer(self)
        self._mixer.timeout.connect(self._matrix_tick)
        self._mixer.start(50)

    def _matrix_tick(self):
        # 生成一屏暗绿字符雨行，整体滚动
        cols = max(20, self.COLS)
        row = []
        dim = _esc(GREEN_DARK)
        bri = _esc(GREEN_DIM)
        for _ in range(cols):
            if random.random() < 0.55:
                row.append(dim + random.choice(MATRIX_CHARS))
            elif random.random() < 0.25:
                row.append(bri + random.choice(MATRIX_CHARS))
            else:
                row.append(dim + " ")
        # 偶尔整段亮头
        self._emit("".join(row) + "\x1b[0m\r\n")
        self.update()

    def _matrix_stop(self):
        if self._mixer is not None:
            self._mixer.stop()
            self._mixer.deleteLater()
            self._mixer = None
        self._emit(f"\x1b[0m\r\n{_esc(AMBER)}[matrix] stopped\x1b[0m\r\n")
        self._show_prompt()

    def resizeEvent(self, e):
        # 用真实字体度量算行列，否则终端网格和像素尺寸对不上（文字被裁/错位）
        fm = QFontMetrics(mono())
        self._char_w = max(1, fm.horizontalAdvance("W"))
        self._char_h = max(1, fm.height())
        cols = max(20, self.width() // self._char_w)
        rows = max(10, self.height() // self._char_h)
        if (cols, rows) != (self.COLS, self.ROWS):
            self.COLS = cols
            self.ROWS = rows
            try:
                # pyte 0.8.2 签名: resize(lines=None, columns=None)
                self.screen.resize(lines=rows, columns=cols)
            except TypeError:
                try:
                    self.screen.resize(rows, cols)
                except TypeError:
                    pass
        self.update()  # 尺寸变了：整屏重画
        super().resizeEvent(e)

    def shutdown_threads(self):
        """统一收尾：kill 子进程并等待全部后台线程退出。
        供本控件 closeEvent 与 MainWindow 顶层关窗时调用
        （子控件 closeEvent 收不到：Qt 只给顶层窗口发 close 事件）。"""
        self._closed = True
        for j in list(self._net_jobs) + list(self._cmd_jobs):
            if isinstance(j, _CmdRunner):
                # 先杀进程树再等线程：避免孤儿进程 + 线程悬挂
                j.shutdown()
            elif j.isRunning():
                try:
                    # 等比 _NetWorker 的 urlopen 超时(8s)更长，确保线程退出
                    j.wait(8500)
                except Exception:
                    pass

    def closeEvent(self, e):
        self.shutdown_threads()
        super().closeEvent(e)   # 冗余调用：实例自身已是顶层窗口时兜底


def gch(ch):
    return ch


from PyQt6.QtWidgets import QApplication