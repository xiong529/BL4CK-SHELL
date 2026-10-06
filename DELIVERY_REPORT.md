# KSH//NT 编辑器改造交付报告

目标：① 从成熟开源编辑器项目（PyQt/Python 栈）逐模块"学过来"；
② 全面审查本仓库代码并修复发现的 bug。本文为两份交付物：
**所学模块改进清单** 与 **代码审查与修复报告**。

---

## 一、参考项目与学习来源

| 项目 | 仓库 | 选取原因 | 学习重点 |
|---|---|---|---|
| PyQCodeEditor | zimolab/PyQCodeEditor (10★) | 模块化控件库，组件划分清晰 | 词表 JSON 数据驱动、当前行/括号高亮、自动配对、语法色表 |
| Syn-CodeEditor | im-syn/Syn-CodeEditor (2★, MIT) | 完整 IDE（配置/主题/标签页/工具栏） | 语言规则表化、高亮器通用引擎、代码组织 |

（均下载到 `_refs/` 供后续查阅；另参考 eDEX-UI 官方仓库的音频/主题/地图/文件面板设计，此四件套已于前一轮交付。）

## 二、所学模块改进清单（对照落地）

### 1. 词表 JSON 化 —— 学自 PyQ `QLanguage`（数据驱动）
- **现状**：词表硬编码在代码里，改一个关键词要改代码。
- **落地**：新增 `langdata/python.json`；`editor.py` 内 `_load_lang()` 外部 JSON 优先、内嵌常量兜底、进程内单例缓存——高亮与补全同源，编辑词表零代码改动。

### 2. 当前行高亮 + 括号配对 —— 学自 PyQ `_highlightCurrentLine/_highlightParenthesis`
- **现状**：编辑器没有光标行提示、括号配对只有颜色。
- **落地**：`_update_extra_selections` 合并 **当前行浅底色 + 左右括号金色配对 + 查找高亮** 三类选区，互不覆盖；行号区当前行号提亮。

### 3. 自动配对括号 / 引号 + `{⏎}` 扩行 —— 学自 PyQ `_doAutoParentheses`
- **现状**：输 `(` 不会自动补 `)`。
- **落地**：输 `( [ { ' "` 自动补右符号且光标居中；闭括号/引号智能跳过；`{` 后回车自动补 `}` 并扩行缩进（对齐 VS Code 编辑器行为）。

### 4. 全语言高亮规则表化 —— 学自 PyQ/Syn 规则表 + 通用引擎
- **现状**：JSON/HTML/MD/CSS/JS 五个高亮器各自手写、正则分散。
- **落地**：统一 `RuleHighlighter` 通用引擎 + `_LANG_RULES` 每语言规则表（正则只编译一次；支持 `words` 词集、`prev` 前缀特判、`marker` 注释起点特判）；Python 保留三引号状态机特例。五语言行为逐点等价验证 17/17 通过。

### 5. 语法色集中管理 —— 学自 Syn 主题色表
- **现状**：13 处 hex 颜色散落各处，换主题无从下手。
- **落地**：`_TOKEN_COLORS` 色表 + `_fmt()` 工厂；六个高亮器、补全弹层、括号金、当前行底、查找高亮全部同源取色，换主题只动一张表。

## 三、代码审查与修复报告

审查方式：双子代理并行审计（editor.py 一份、term.py+main.py 一份），发现项全部手工复现确认后再修，逐项回归。

### editor.py
| 级别 | 问题 | 修复 |
|---|---|---|
| CRITICAL | 打开失败后继续可编辑/自动保存 → 覆盖原文件 | `_readonly_fail` 守卫，打开失败即禁编辑与自动保存 |
| HIGH | `import` 插入位置错乱/重复 | `_insert_import` 重写：顶部 import 块追加 + 全文去重 + `os`/`os.path` 区分 |
| HIGH | 弹层残留/之字形焦点 | 弹层改 `Tool+NoFocus+WindowDoesNotAcceptFocus`，不抢焦点；focusOut 延后一帧判定 |
| HIGH | 高亮器泄漏 | `_set_lang` 先 `setDocument(None)` 卸下旧高亮器再重建 |
| MEDIUM | 三引号内 `x=1` 污染名字表 | 扫描前整体挖掉三引号块 |
| LOW | 注释内触发补全 | `_context_before_cursor` 识别 `#` 注释上下文，直接抑制 |
| LOW | 光标移动每次 `os.path.getsize` stat | 1 秒缓存 stat 结果 |
| LOW | 末行/右缘弹层越出屏幕 | 自动上移/收窄，对齐 VS Code |
| LOW | 空字符串 keyClicks 崩溃 | 补全入口守卫 `_comp` 未初始化即返回 |

### term.py
| 级别 | 问题 | 修复 |
|---|---|---|
| HIGH | 流式输出卡死/挂起线程 | `_stream_feed` 改 `singleShot` 续块 + `_closed` 关窗标志切断 |
| HIGH | 子进程僵尸 | `_CmdRunner` 改 `Popen` 句柄持有 + `shutdown()` 超时 kill |
| MEDIUM | Ctrl+C 不中断长输出 | `_feed_cancel` + 外部命令 kill |
| MEDIUM | 历史方向错误 | `_hist_nav` 修正为 Up 向更旧 |
| MEDIUM | 命令补全吞词 | `_sug_apply` 替换 token 而非整串 |
| MEDIUM | sudo 单匹配误吞目录 | `_complete` 单匹配保留目录部分 |
| LOW | `ls` 走了外部 `dir`（带表头） | 从 CMD_ALIAS 剔除，内置 `_do_dir` 干净输出 |
| LOW | `cat` 无内置 | 新 `_do_cat` 读文件（cwd 相对解析） |
| LOW | `ps` 抢在 `ping`/`pwd` 前 | 移出常用优先集 |
| LOW | 首屏空白竞态 | `_first_banner` 尺寸守卫，未布局时延后 |
| LOW | Ctrl+V 崩溃 | `QApplication()` 二次构造 → `QApplication.clipboard()` 静态调用 |

### main.py
| 级别 | 问题 | 修复 |
|---|---|---|
| MEDIUM | 关闭未保存标签直接丢 | dirty 确认对话框 |
| MEDIUM | 文件树与终端 cwd 不同步 | `_on_left_cwd` 双向同步 |

### audio.py
| 级别 | 问题 | 修复 |
|---|---|---|
| LOW | QSoundEffect 线程退出挂起 | `shutdown()` 停止释放全部音效，`aboutToQuit` 时调用 |

## 四、验证结果

| 套件 | 范围 | 结果 |
|---|---|---|
| `_regress.py` 回归 | 补全排序/Tab 选中/历史/Ctrl+C/多行粘贴/CmdRunner 等 20 项 | **20/20 PASS** |
| 高亮规则表等价 | JSON/HTML/MD/CSS/JS 17 个关键颜色点 | **17/17 PASS** |
| 综合冒烟 | 词表装载/当前行/自动配对/规则表编译/色表/剪贴板/文件树联动 | **27/27 PASS** |
| 全模块 py_compile | 10 个 .py | 全 OK |