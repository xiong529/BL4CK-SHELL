# KSH//NT — Secure Console

类 Linux 终端模拟器 + 代码编辑器，Windows 后端，PyQt6 构建。
外观与交互致敬 eDEX-UI：开机自检动画、真实硬件数据、3D 世界地图、命令补全。

> License: MIT（详见 [LICENSE](LICENSE)）· 仿终端 UI 项目，仅供学习与技术展示，请勿用于任何非法用途。

## 功能

- **启动动画**：BIOS 自检流 + 真实硬件探测（CPU/内存/磁盘/网络 IP/运行时间，后台线程）
- **终端**：pyte 终端模拟、流式命令输出（长进程/无限输出不卡死）、命令补全（VS Code/inshellisense 风格）、命令历史、多行粘贴
- **编辑器**：6 语言语法高亮（Python/JSON/HTML/Markdown/CSS/JS）、补全弹窗、括号/引号自动配对、当前行高亮、查找
- **信息面板**：CPU/内存/磁盘/网络流量实时图表、进程列表、端口、文件树（与终端 cwd 双向联动）
- **世界地图**：预计算的 3D 地球动画，城市光晕与弧线
- **主题**：20+ 主题一键切换，全局重着色
- **音效**：事件音效（可关闭）

## 运行

```powershell
pip install -r requirements.txt
python main.py
```

## 依赖

- Python 3.9+
- PyQt6
- pyte
- psutil
- wcwidth

## 项目结构

| 文件 | 说明 |
|---|---|
| `main.py` | 主窗口装配（标题栏/三栏布局/标签页/boot 覆盖层） |
| `term.py` | 终端：pyte 模拟、串行 feed 队列、ANSI 极速解析、命令补全 |
| `editor.py` | 编辑器：规则表高亮引擎、补全、自动配对 |
| `panels.py` / `sidebar.py` / `monitor.py` | 左侧信息栏 / 右侧栏 / 监控页 |
| `worldmap.py` | 预计算 3D 世界地图动画 |
| `theme.py` / `audio.py` | 主题系统 / 音效 |
| `boot.py` | 开机自检动画 |
| `langdata/python.json` | Python 词表（JSON 数据驱动） |
| `assets/` | 音效与主题 JSON |
| `_regress.py` | 回归测试（20 用例） |

## 设计参考

本项目的编辑器模块在实现时参考了以下开源项目的设计思路（代码为自行实现，参考源码存于本地 `_refs/`，不随仓库发布）：

- [zimolab/PyQCodeEditor](https://github.com/zimolab/PyQCodeEditor) — MIT License
- [im-syn/Syn-CodeEditor](https://github.com/im-syn/Syn-CodeEditor) — MIT License

## 免责声明

- 项目内的音效与主题文件风格参考 eDEX-UI（GPL-3.0），如需商用请自行替换或确认许可。
- 终端会真实执行用户输入的命令，请仅在可信环境中运行。
