#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的机器闸门①：检查长提示词是否符合 12 段契约。

（闸门②是 scripts/check_skill.py：检查这个技能自己的定义能不能被宿主自动识别。）

用法：
    python scripts/check_prompt.py <prompt.md> [<prompt2.md> ...]
    python scripts/check_prompt.py --selftest        # 自测：正例必须 0 ERROR，反例必须 >=5 ERROR

退出码：有 ERROR 时非零退出。发现问题就打印 `ERROR: ...` 行，不静默通过。
检查项清单见 references/校验清单.md。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
import tempfile
import time
from pathlib import Path

def _setup_stdout() -> None:
    """管道/重定向时强制 UTF-8（避免 Windows 上按 GBK 输出被读成乱码）；
    真实控制台保留本机编码，只保证不可表示的字符不炸。"""
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        else:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass


_setup_stdout()

# ---------------------------------------------------------------- 词表（权威）

#: 模糊词黑名单：出现在需求/验收段是 ERROR，出现在交付物/其他段是 WARN。
#: references/模糊词换算表.md 是给人看的那一份，改这里要同步改那边。
VAGUE_WORDS = [
    "尽量", "尽快", "尽可能", "最好能", "适当", "差不多", "大概", "一些", "若干", "大量",
    "等等", "之类的", "类似的", "相关的", "看着办", "稍微", "有点", "顺便",
    "在可行范围内", "如需要", "必要时", "视情况", "视需要",
    "好看", "美观", "大气", "高级感", "有质感", "流畅", "秒开", "高性能", "稳定可靠",
    "易用", "好用", "友好", "方便", "简洁", "清爽", "合理", "完善", "丰富", "强大", "灵活", "优雅",
    "优化一下", "改进一下", "处理一下", "基本可以", "用户体验好", "效果好", "感觉",
    # 下面这批在 references/模糊词换算表.md 里一直是「必须换算」的条目，但脚本原先没查：
    # 表里写「快 / 快点 / 尽快」「稳定 / 可靠」「兼容 / 支持多平台」「安全」都要求换算，
    # 脚本却只认「尽快」「稳定可靠」，于是这些词靠自觉。补进来让表与闸门共用一份判据。
    "快点", "简单点", "不卡", "稳定", "可靠", "支持多平台", "兼容", "快", "安全",
]

#: 占位符：任何段落里出现都是 ERROR。
PLACEHOLDERS = ["TODO", "TBD", "待补充", "待填", "待定", "XXX", "???"]
#: 占位符分两类：ASCII 词要大小写不敏感 + 词边界（`TODOs` 是英文单词，小写 `todo` 也是
#: 没改完的证据），中文词按子串（审计 F11）。
PLACEHOLDER_ASCII = ("TODO", "TBD", "XXX", "???")
PLACEHOLDER_CJK = ("待补充", "待填", "待定")
#: 文件名 / path token：`docs/TODO.md` 里的 TODO 是文件名，不是没改完的占位符（审计 F11）。
#: 识别与抹除在 `strip_file_tokens()` 里做：旧版 `FILE_TOKEN_RE.sub` 的 `[\w./\\-]+` 在
#: 长串标点行（`.`/`-`/`/`、`a/` 重复）上会从每个起点回溯一遍，是 O(n²)：
#: 32KB 点号 11.6s、1MB 单行直接卡死（审计 R6-gate1-01）。扫描口径与旧正则一致，
#: 只有「整段词字符 run 一起抹掉」这点变化 —— 对占位符判断等价（run 里切不出两个独立
#: token），但复杂度是线性的。
_TOKEN_RUN_RE = re.compile(r"[\w./\\-]+")
#: 文件名扩展名的形状（`FILE_TOKEN_RE` 里的 `\.[A-Za-z][A-Za-z0-9]{0,7}`）。
_TOKEN_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}")
#: HTML 注释块（允许跨行）。`pf-ignore` 只认注释里的那一份：围栏 / 行内代码里的写法示例
#: 不是真豁免，也不该被当成 E11 报出来（审计 F5 / F12）。注释区间用 `_comment_spans()`
#: 线性扫描：旧版 `re.finditer("<!--(.*?)-->")` 在大量未闭合 `<!--` 上是 O(n²)
#: （64KB 的 `<!--` 要 7.9 秒、1MB 直接卡死，审计 R6-gate1-02）。
#: 注释内容：`pf-ignore: W2 理由`；理由可以折行，解析时拼成一句。
IGNORE_RE = re.compile(r"^\s*pf-ignore\s*[:：]\s*(.*)$", re.S)
IGNORE_CODES = tuple(f"W{i}" for i in range(1, 10))
#: ERROR 码表同时是「pf-ignore 不能豁免谁」的判据：漏一个码，写 `pf-ignore: E11` 就会掉进
#: 「不认识这个码」分支，得到一句与事实不符的提示（审计 P19）。
ERROR_CODES = ("E0", "E1", "E2", "E2b", "E3", "E3b", "E4", "E5", "E6", "E7", "E8", "E9", "E10", "E11")
#: 理由至少几个字：写「误报」算说了，写「-」「ok」不算。
IGNORE_REASON_MIN = 4


def codes_hint(codes: tuple[str, ...]) -> str:
    """把码表压成 `W1…W9、C1…C5` 这样的短提示。

    闸门③ 会带着自己的 `C1…C5` 一起传进来（闸门① 只认 `W1…W9`），写全 14 个码
    会把一行报错撑到没人读。
    """
    groups: list[tuple[str, int, int]] = []
    for code in codes:
        digits = re.sub(r"\D", "", code)
        if not digits:
            continue
        letter, num = code[0], int(digits)
        if groups and groups[-1][0] == letter:
            groups[-1] = (letter, groups[-1][1], num)
        else:
            groups.append((letter, num, num))
    return "、".join(f"{letter}{lo}…{letter}{hi}" for letter, lo, hi in groups)

#: 12 段契约：段落名 -> 标题里出现任一关键词即算命中。
REQUIRED_SECTIONS: list[tuple[str, list[str]]] = [
    ("1 角色与目标", ["目标", "角色"]),
    ("2 背景与已知事实", ["背景", "上下文", "已知事实", "现状"]),
    ("3 交付物", ["交付物", "交付清单", "产出物", "产出清单", "交付内容"]),
    ("4 需求（R#）", ["需求明细", "需求清单", "需求列表", "需求"]),
    ("5 非目标", ["非目标", "不做", "范围外", "不在范围"]),
    ("6 技术约束", ["技术约束", "技术栈", "约束", "依赖与版本"]),
    ("7 验收标准（A#）", ["验收"]),
    ("8 验证方式", ["验证", "自测", "怎么测", "测试方式"]),
    ("9 执行步骤", ["执行步骤", "步骤", "实施步骤", "执行计划", "分步"]),
    ("10 假设与开放问题", ["假设", "开放问题", "待确认", "不确定"]),
    ("11 何时必须停下来问人", ["问人", "停下来", "升级路径", "求助", "何时必须", "必须询问"]),
    ("12 汇报格式", ["汇报", "输出格式", "回复格式", "交付说明", "结果格式"]),
]

#: 这两段命中别名就按 ERROR 级查模糊词（其余段落只报 W9）—— 见 `references/校验清单.md` 的码表。
#: 判据是「命中」而不是「绑定」：段别名命中过的每一条标题都要查，绑定选错了也不能有静默降级的窗口。
ERROR_LEVEL_LABELS = ("4 需求（R#）", "7 验收标准（A#）")

#: 交付物/需求里的文件路径。判据是「路径形状」，不是「长得像扩展名」：
#: 白名单扩展名允许裸文件名（`prompt.md`）；白名单外的扩展名（go / gif / xml / db…）必须带目录。
#: 这样 `Next.js`、`Tailwind.css`、`example.com` 这类库名/域名不会被当成交付路径 ——
#: 旧正则允许任意 token 撞兜底扩展名，交付物段写一句「用 Next.js 做」就能让 E8 假绿（审计 F3）。
EXT_WHITELIST = (
    r"md|py|js|mjs|cjs|ts|tsx|jsx|json|jsonc|txt|csv|tsv|html|css|scss|"
    r"yml|yaml|toml|ini|sh|ps1|cmd|bat|sql|xlsx|docx|pptx|pdf|png|jpg|jpeg|svg|webp|exe|zip|log|env|"
    r"c|h|r|m|o|a|7z"
)
#: 两段路径（段长封顶 255）：旧版 `[...]*` 在「无空白超长行」上是 O(n²) 回溯
#: （交付物段里塞一行 200KB 的字母，has_path_token 能卡到分钟级，审计 R5-gate1-01）。
#: 文件系统单段路径本来就不超过 255，封顶不影响任何真实路径。
PATH_RE = re.compile(
    rf"(?:[A-Za-z0-9_\-.]{{0,255}}[\\/][A-Za-z0-9_\-.]{{0,255}})\."   # 带目录：白名单 + 兜底扩展名
    rf"(?:{EXT_WHITELIST}|[A-Za-z][A-Za-z0-9]{{1,7}})"
    rf"|[A-Za-z0-9_\-]{{1,255}}\.(?:{EXT_WHITELIST})",                 # 裸文件名：只认白名单
    re.IGNORECASE,
)
#: 交付物/需求里先剥掉的「非路径」token：URL 与前端库名。
#: `https://example.com/release`、`Next.js`、`Tailwind.css` 都长得像带扩展名的路径。
#: scheme 长度封顶 64：旧版 `[A-Za-z0-9+.\-]*` 在无空白长字母行上同样是 O(n²)
#: （1MB 单行 >10 分钟，审计 R5-gate1-01）；真实 scheme 最长也就十几字符，封顶无副作用。
URL_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]{0,63}://|www\.)\S+")


def strip_urls(text: str) -> str:
    """把 URL 换成空格（E8 的 has_path_token 与 E10 的占位符扫描共用）；先过廉价守卫。

    `://` 与 `www.` 是 URL_RE 两个分支各自必须出现的字面量：两者都没有的行不可能命中，
    直接返回即可 —— 无空白长行因此连正则都不进（审计 R5-gate1-01：1MB 单行 >10 分钟）。
    真带 `://` 的行由封顶的 scheme 量词保证 O(64n)，不会退化成平方回溯。
    """
    if "://" not in text and "www." not in text:
        return text
    return URL_RE.sub(" ", text)


def strip_file_tokens(text: str) -> str:
    r"""把文件名 / path token 换成空格（E10 扫占位符前用）。

    等价于旧版 `FILE_TOKEN_RE.sub(" ", text)`（`docs/TODO.md` 整段抹掉，里面的 TODO
    不算占位符），但按「词字符 run」整体处理：每个 run 只要含 `.扩展名` 形状就抹掉，
    否则原样保留。旧正则的 `[\w./\\-]+` 会在长串标点行上从每个起点回溯（32KB 点号
    11.6s、1MB 卡死），这里先 `("." in text)` 与 per-run 扩展名检测，整体 O(n)
    （审计 R6-gate1-01）。
    """
    if "." not in text:
        return text
    parts: list[str] = []
    last = 0
    for match in _TOKEN_RUN_RE.finditer(text):
        run = match.group(0)
        if "." not in run or _TOKEN_EXT_RE.search(run) is None:
            continue
        parts.append(text[last:match.start()])
        parts.append(" " * len(run))
        last = match.end()
    if not parts:
        return text
    parts.append(text[last:])
    return "".join(parts)


LIB_NAME_RE = re.compile(
    r"(?<![\w./\\-])(?:node|next|nuxt|vue|react|angular|svelte|tailwind|ember|alpine|deno|bun|"
    r"express|nest|three|jquery|webpack|vite|rollup|eslint|prettier|babel|jest|vitest)"
    r"\.(?:js|mjs|cjs|ts|tsx|jsx|css|scss|sass|less)",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\d")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
#: ATX 标题：CommonMark 允许 1~6 个 `#`（审计 F15）。
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
#: 围栏：开栏记录字符种类与 run 长度，闭合要求同种字符且 run 不短于开栏、后面只剩空白
#: —— 否则 ```` 围栏会被内层 ``` 提前关掉，示例里的标题/条目变成幽灵（审计 F6）。
FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
#: setext 标题的下划线：标题文字在上一行，`===` 是一级、`---` 是二级（`---` 也可能是
#: 分隔线，所以 `split_sections` 只对「普通段落行」认这一种，见 `_setext_level`）。
SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
#: CommonMark 缩进代码块：4 空格或 Tab 起头。只在上一行是空行时才算（审计 F13）——
#: 否则列表续行（2~3 空格）、表格续行、`>` 引用块都会被卷进来。
INDENT_CODE_RE = re.compile(r"^(?: {4}|\t)")

#: 段落标题的编号，两种写法都要认：
#:   前缀式 `4 需求` / `4. 需求` / `4、需求` / `4）需求`
#:   包裹式 `（4）需求` / `(4) 需求` / `【4】需求`
#: 绑定段落时带编号的优先——它是「这是正文段落，不是文档标题」的信号。
#: （2026-10-12：原先只认前缀式，导致 `## （4）需求（R#）` 被判成「没编号」，
#:   于是一级标题里的「需求」二字照样抢走绑定，整条 E9 检查空转。）
#: （2026-10-12 第二次：前缀式的分隔符原来是**可选**的，于是 `## 3D 需求素材说明`、
#:   `## 4K 验收说明`、`## 100 分交付需求说明` 这类「数字开头的段名」也被判成
#:   「带编号」，照样抢走绑定（同样是 E9 空转成 W9）。现在必须真的有分隔符，或者
#:   数字后面跟空白；`## 4 需求（R#）`（仓库 12 段契约自己的写法）仍然算带编号。）
SECTION_NUM_RE = re.compile(
    r"^(?:[（(【\[]\s*\d{1,2}\s*[）)】\]]"          # 包裹式 `（4）需求`
    r"|(?<![A-Za-z0-9])\d{1,2}\s*[\.、\)）:：]\s*"   # 前缀式 `4. 需求` / `4、需求` / `4）需求`
    r"|\d{1,2}\s+(?=\S))"                            # 前缀式 `4 需求`（数字 + 空白）
)
ITEM_STRIP_RE = re.compile(r"^[>\|\-\*\+\d\.\)\s]+")
#: 条目编号后面紧跟这些词时，那个编号是纸张/图纸规格（`A4 纸`、`A3 幅面`），不是条目
#: 编号 —— 旧版把它当验收 A4，凭空报 E4 + E5（审计 F7）。
PAPER_SPEC_WORDS = ("纸", "图纸", "幅面", "幅", "开本")


class Finding:
    def __init__(self, level: str, code: str, message: str, line: int = 0) -> None:
        self.level = level
        self.code = code
        self.message = message
        self.line = line

    def render(self) -> str:
        where = f" (第 {self.line} 行)" if self.line else ""
        return f"{self.level}: {self.code} {self.message}{where}"


def strip_emphasis(text: str) -> str:
    return text.replace("**", "").replace("__", "").replace("`", "")


def _comment_spans(text: str) -> list[tuple[int, int]]:
    """HTML 注释的区间列表 `[(start, end), …]`，线性扫描、未闭合的 `<!--` 不算注释。

    旧版用 `re.sub(r"<!--.*?-->")`：每个 `<!--` 起点都会把后面的正文扫一遍找 `-->`，
    大量未闭合的 `<!--` 因此是 O(n²)（64KB 的 `<!--` 要 7.9 秒、1MB 卡死，审计
    R6-gate1-02）。这里用两次 `str.find` 顺序推进，单次扫完整段文本。
    """
    spans: list[tuple[int, int]] = []
    at = 0
    while True:
        start = text.find("<!--", at)
        if start < 0:
            return spans
        end = text.find("-->", start + 4)
        if end < 0:
            return spans
        spans.append((start, end + 3))
        at = end + 3


def strip_comments(text: str) -> str:
    """把 HTML 注释换成等量空行：注释是给人的元信息，不是正文。

    为什么必须剥：`<!-- pf-ignore: W2 … -->` 里的 `W2` **带着数字**。注释紧跟在条目后面时
    它就是这条需求的续行，`NUMBER_RE` 一看到那个 `2` 就认为「这条需求有数字、可验证」，
    W2 于是静默消失 —— 豁免注释自己把要豁免的检查弄成了假绿（实测：`- **R3** 界面要顺手。`
    加一行 `<!-- pf-ignore: W2 … -->` 之后 W2 既不报也不豁免，直接不见了）。
    换成等量空行是为了保住行号：E9/W9 的报错行号要指向编辑器里那一行。
    """
    spans = _comment_spans(text)
    if not spans:
        return text
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        parts.append("\n" * text.count("\n", start, end))
        last = end
    parts.append(text[last:])
    return "".join(parts)


def normalize_heading(raw: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw).strip()
    text = strip_emphasis(text)
    text = SECTION_NUM_RE.sub("", text)  # 去掉 "5. " / "5、" 这类编号
    return text.replace(" ", "").replace("\u3000", "").replace("：", "").replace(":", "")


def display_heading(raw: str) -> str:
    """标题原样展示：只去掉 `#` 与 `**`，**保留编号**。

    报错文案里要用标题本身。拿 `normalize_heading()` 的结果去报会把 `### 4.1 需求补充说明`
    渲染成「1需求补充说明」（编号只剩后半截、还和段名粘在一起），用户按名字搜不到
    —— 行号能救回来，但没人愿意先跳过去看一眼再回来找。
    """
    return strip_emphasis(re.sub(r"^\s{0,3}#{1,6}\s*", "", raw)).strip()


def heading_has_number(raw: str) -> bool:
    """标题里有没有 `8 段` 这种编号。

    绑定段落时带编号的标题优先：编号是「这是正文段落」的信号，而文档标题
    （`# 任务：按下面的需求做一个…`）也会命中别名，不能让它把正文段落抢走。
    """
    head = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw).strip()
    return bool(SECTION_NUM_RE.match(strip_emphasis(head)))


def heading_number(raw: str) -> int | None:
    """标题编号里的数字（`## 4. 需求（R#）` → 4；`## （4）需求` → 4；`## 3D 素材` → None）。

    绑定「第 N 段」时，号数与 N 一致的那条标题优先 —— 只看「带不带编号」不够：
    `# 3 用户需求说明` 也是一条带编号的同义标题，位置还常常更靠前，光按文档顺序
    它就会把 `## 4 需求（R#）` 的绑定抢走（审计用例 C2-h1num-above）。
    """
    head = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw).strip()
    match = SECTION_NUM_RE.match(strip_emphasis(head))
    if not match:
        return None
    digits = re.search(r"\d+", match.group(0))
    return int(digits.group(0)) if digits else None


#: 别名前面紧跟「非 / 不」时不算命中：「非目标」是第 5 段，不是第 1 段里的「目标」。
NEGATION_CHARS = "非不无"

#: `判定：` 的三种取值（AGENTS.md 第 4 节）：命令 / 阈值 / 人工，三选一。
JUDGE_KINDS = ("命令", "阈值", "人工")

#: 判定方式必须是 `判定：` 后面**紧跟**三态词，但允许常见的包裹写法（加粗 / 反引号 /
#: 中文引号 / 括号）：`判定：**人工**` 明确写了判定，旧版一律报 E5（审计 R5-gate1-03）。
#: 纯子串匹配会被两种写法骗过：`判定：不需要人工（脚本自动跑）`、`判定方式另见下文，人工确认`
#: —— 两者都没有有效判定，却因为正文里出现了「人工」而通过 E5（审计 F5）。
JUDGE_RE = re.compile(r"判定\s*[:：]\s*(?:[`*_「『“‘（(【\[]\s*)*(命令|阈值|人工)")

#: 段落里只写这些词（可带标点或括号）等于没写。
EMPTY_CLAIMS = {"", "无", "没有", "none", "n/a", "不适用", "暂无", "略"}


def alias_hit(title: str, aliases: list[str]) -> bool:
    """标题是否命中别名；被「非/不」否定掉的不算（「非目标」不等于「目标」）。"""
    for alias in aliases:
        at = title.find(alias)
        while at != -1:
            if at == 0 or title[at - 1] not in NEGATION_CHARS:
                return True
            at = title.find(alias, at + 1)
    return False


def judge_kind(text: str) -> str | None:
    """条目里 `判定：` 后面紧跟的三态取值；没写、写成否定、或另见他处都返回 None。

    旧版拿「`判定` 之后出现的第一个三态词」判：`判定：不需要人工` 因为后面有「人工」被当成
    人工判定，`判定方式另见下文，人工确认` 也一样（审计 F5）。现在要求冒号后紧跟取值。
    """
    match = JUDGE_RE.search(text)
    return match.group(1) if match else None


#: 一条 A# 自己的正文里，「子条目 / 表格行」不是这条验收的判定：
#: `- **A3** …` 下面再挂 `- 参照 A1：判定：命令 …` 不能给 A3 兜底（审计 R5-gate1-04）。
#: 但子条目**自己以 `判定：` 开头**是常见排版（判定缩进一行写，审计 R6-gate1-04）：
#: 这种行要先剥掉列表符号再当续行算，否则 E5 假红。表格行 `| 判定 | 人工 |` 不算。
_JUDGE_LEAD_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,2}[.)]\s+)(?:\*\*|__)?判定\s*[:：]")
NESTED_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s|\d{1,2}[.)]\s|\|)")


def judge_text(block: list[str]) -> str:
    """A# 块里真正属于这条验收的正文：跳过围栏行与子条目 / 表格行。

    `判定：` 必须写在这条 A# 的条目行或它的续行上；写在同块的子条目、表格行或围栏
    示例里都不算 —— 旧版把整块拼起来交给 `JUDGE_RE`，于是 E5 被同块的「判定：命令」
    「传染」成假绿：A3 本体根本没写判定，`--selftest` 里 0 ERROR（审计 R5-gate1-04）。
    不带列表符号的续行仍然保留，正常排版（换行写判定）不受影响；子条目自己以
    `判定：` 开头的（缩进写判定）剥掉列表符号后也算续行（审计 R6-gate1-04）。
    """
    normalized: list[str] = []
    for line in block:
        if NESTED_ITEM_RE.match(line) and _JUDGE_LEAD_RE.match(line):
            line = NESTED_ITEM_RE.sub("", line, count=1)
        normalized.append(line)
    fenced = fence_lines(block)
    return "\n".join(
        line for j, line in enumerate(normalized)
        if not fenced[j] and (j == 0 or not NESTED_ITEM_RE.match(line))
    )


def is_empty_claim(text: str) -> bool:
    """段落是否为空或只写了「无」（容忍「无。」「（无）」这类标点/括号写法）。

    注释不算内容：`## 10 假设与开放问题` 下面只挂一行 `<!-- 等用户补 -->` 仍然是空段。
    """
    cleaned = re.sub(r"[\s。，,、；;：:！!？?\-—~～*•·]+", "",
                     strip_emphasis(strip_comments(text)))
    return cleaned.strip("（）()【】[]<>「」").lower() in EMPTY_CLAIMS


def has_path_token(text: str) -> bool:
    """text 里有没有真正的「路径形状」token：先剥 URL 与库名，再按 PATH_RE 找。

    E8 问的是「交付物写了具体路径吗」。`https://example.com/release` 与
    `用 Next.js 写` 都不算 —— 它们没有交付路径，却能让旧正则命中 `.com` / `.js`（审计 F3）。
    """
    # 下面三个正则的每个分支都要求 token 里有 `.`（白名单/兜底扩展名、`.js` 库名）：
    # 整段没有点就不可能命中，直接返回 —— 否则交付物段里一行 128KB 字母会让 PATH_RE
    # 在每个起点上试满 255 字符（O(255n)，1MB 级要 8 秒，审计 R5-gate1-01）。
    if "." not in text:
        return False
    text = strip_urls(text)
    text = LIB_NAME_RE.sub(" ", text)
    return bool(PATH_RE.search(text))


def placeholder_hit(text: str) -> str | None:
    """行内第一个真占位符：ASCII 词按大小写不敏感 + 词边界，中文词按子串。

    `TODO` 是占位符，`TODOs` 是英文单词，`docs/TODO.md` 是文件名 —— 后两种都不算；
    反过来小写 `todo:` 也是没改完的证据（审计 F11）。返回报告用的规范写法。
    """
    for token in PLACEHOLDER_ASCII:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text, re.I):
            return token
    for token in PLACEHOLDER_CJK:
        if token in text:
            return token
    return None


def fence_lines(lines: list[str]) -> list[bool]:
    """标记每行是否在 ``` / ~~~ 围栏内 —— 围栏里的 `#` 是注释，不是标题。

    围栏按 CommonMark 配对：同一种字符、闭合 run 不短于开栏、闭合行后面只能有空白。
    旧版只看前三个字符，```` 开的栏会被内层 ``` 提前关掉，示例里的标题/条目于是变成
    幽灵标题 / 幽灵需求（审计 F6）。
    """
    flags: list[bool] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        stripped = line.strip()
        match = FENCE_RE.match(stripped)
        inside = fence is not None
        if match is not None:
            run, rest = match.group(1), match.group(2)
            if fence is None:
                fence = (run[0], len(run))
                inside = True
            elif run[0] == fence[0] and len(run) >= fence[1] and not rest.strip():
                fence = None
                inside = False
        flags.append(inside)
    return flags


def indented_code_lines(lines: list[str], fenced: list[bool]) -> list[bool]:
    """标记 CommonMark 缩进代码块（4 空格 / Tab 起头）里的行。

    这类代码块没有围栏，旧版把它当正文：示例里的 `- **R9**` 变成幽灵需求（审计 F13）。
    判定故意收窄：只有「上一行是空行 + 本行 4 空格/Tab 起头」才进代码块，一直延续到
    第一行非空且缩进不足的正文为止。这样列表续行（2~3 空格）、表格续行、`>` 引用块
    都不会被误跳。
    """
    flags: list[bool] = [False] * len(lines)
    inside = False
    for idx, line in enumerate(lines):
        if fenced[idx]:
            inside = False
            continue
        blank = not line.strip()
        indented = INDENT_CODE_RE.match(line) is not None
        if not inside:
            prev_blank = idx == 0 or not lines[idx - 1].strip()
            if indented and prev_blank:
                inside = True
        elif not blank and not indented:
            inside = False
        flags[idx] = inside
    return flags


def _setext_level(lines: list[str], idx: int) -> int | None:
    """`lines[idx]` 是不是 setext 标题的文字行；返回标题级别（1 或 2）。"""
    current = lines[idx]
    if not current.strip() or idx + 1 >= len(lines):
        return None
    if HEADING_RE.match(current):
        return None
    # 列表项 / 引用 / 表格行后面跟 `---` 是分隔线，不是 setext 标题；但 `===` 不可能是
    # 分隔线（`8. 验证方式` 下一行画 `====` 就是合法的 setext 一级标题）。
    list_like = re.match(r"^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+|>|\|)", current)
    underline = lines[idx + 1]
    match = SETEXT_RE.match(underline)
    if match is None or "|" in underline:
        return None
    char = match.group(1)[0]
    if list_like is not None and char == "-":
        return None
    # 单个 `-` 更像列表符号/分隔线，至少两个才当二级标题下划线。
    if len(match.group(1)) < 2:
        return None
    return 1 if char == "=" else 2


class FileReadError(Exception):
    """读文件失败（OSError）：由 report() 转成一行 `ERROR: 读不了文件…`，不抛 traceback。

    退出码仍然是 1（闸门不通过），但用户看到的是一句人话，而不是 Python 堆栈
    —— 文件被占用/没权限/路径断开都算「这一关没过」，不是脚本坏了（审计 F2）。
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"读不了文件：{path}（{reason}）")


def split_lines(text: str) -> list[str]:
    """只按 `\\n` 切行（先把 CRLF / CR 统一成 `\\n`）。

    不用 `str.splitlines()`：它把 U+2028 / U+2029 / U+0085 也当换行。从 PDF、聊天记录里
    粘进来的不可见字符会让行号比编辑器多出几行，报错指到别的段落（审计 F8 / 踩坑记录 #28）。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def read_text(path: Path) -> str:
    """按 BOM/编码回退读全文，行尾统一成 `\\n`；读不了抛 FileReadError。

    顺序：UTF-16 BOM（记事本另存的 UTF-16 不能落 E0）→ utf-8-sig → utf-8 → gbk →
    utf-8（errors=replace，最后一道兜底保证一定能读）。
    旧版只对 UnicodeDecodeError 回退，OSError（文件被占用、没权限）会直接抛 traceback；
    而且 UTF-16 文件会按 utf-8 读出乱码，落一个莫名其妙的 E0（审计 F2 / F17）。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FileReadError(path, str(exc)) from exc
    text = ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            text = ""
    if not text:
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


#: 零宽 / 不可见字符：从网页、Word、聊天记录里粘进标题和编号时肉眼看不见，却让
#: 「需求」「R3」「判定：人工」全部匹配失败 —— 闸门假红（审计 R6-gate1-03）。
#: U+200B 零宽空格、U+200C ZWNJ、U+200D ZWJ、U+2060 word joiner、U+FEFF BOM/零宽不换行空格。
INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
#: 全角字母：中文输入法全角态打出的 `Ｒ3` / `Ａ3` 和半角视觉一样，编号却认不出来（同上）。
FULLWIDTH_LETTER_TRANS = str.maketrans(
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)


def normalize_invisible(text: str) -> str:
    """去掉零宽/不可见字符，NBSP 换成普通空格，全角字母换成半角字母。

    只动「看不见的东西」与全角字母：正文语义、行数、行号都不变，所以在读文件后一次性做
    （`check_file` 入口）。不碰全角标点（`，。：`）和全角数字（`\\d` 本来就认）。
    审计 R6-gate1-03：标题里夹一个 U+200B 就报 E1、`Ｒ3` 认不出就报 E2、判定词夹零宽就
    报 E5 —— 全是复制粘贴带进来的假红，用户看着那行明明是对的。
    """
    return INVISIBLE_RE.sub("", text.replace("\u00a0", " ")).translate(FULLWIDTH_LETTER_TRANS)


def split_sections(lines: list[str]) -> tuple[list[tuple[str, int, int, int, bool]], list[str]]:
    """返回 ([(归一化标题, 标题级别, 起始行号, 结束行号, 有没有编号)], 无标题前的前言行)。

    级别和编号都要留着：文档标题（`# 任务：…`）和正文段落一样会被当成候选段落，
    而它只是一句话的名字，不该抢走任何一段的绑定（见 `check_file` 里的 E1 绑定）。

    注释里的标题不是标题：整段被 `<!-- ... -->` 包住时应该报「缺少必需段落」，
    而不是拿注释里的标题当段落（审计 F1）。**调用方负责先剥注释**：`check_file()` 用
    `strip_comments()` 把正文行视图准备好再传进来，这里不再重复剥一遍（重复层会让变异
    检验改掉任何一层都不变红）。
    """
    headings: list[tuple[str, int, int, bool]] = []
    fenced = fence_lines(lines)
    indented = indented_code_lines(lines, fenced)
    skip_underline = False
    for idx, line in enumerate(lines):
        if skip_underline:
            # setext 标题的下一行是下划线本身，不是新的标题文字（审计 F15）。
            skip_underline = False
            continue
        if fenced[idx] or indented[idx]:
            continue
        if HEADING_RE.match(line):
            depth = line.lstrip()
            headings.append((normalize_heading(line), len(depth) - len(depth.lstrip("#")), idx,
                             heading_has_number(line)))
            continue
        level = _setext_level(lines, idx)
        if level is not None:
            headings.append((normalize_heading(line), level, idx, heading_has_number(line)))
            skip_underline = True
    sections: list[tuple[str, int, int, int, bool]] = []
    for i, (title, level, start, numbered) in enumerate(headings):
        end = headings[i + 1][2] if i + 1 < len(headings) else len(lines)
        sections.append((title, level, start, end, numbered))
    preamble = lines[: headings[0][2]] if headings else lines
    return sections, preamble


def body_of(lines: list[str], start: int, end: int) -> str:
    """段落正文（不含标题行）；`lines` 必须已经剥过注释（见 `check_file` 的两套行视图）。

    注释不是正文：拿注释里写的段落标题/条目当内容，会让「整段被注释掉」的文档一路绿灯
    （审计 F1）。`check_file()` 统一剥注释（用等量换行替换，行号仍对得上），这里直接切行。
    """
    return "\n".join(lines[start + 1 : end])


def item_start(text: str, letter: str) -> int | None:
    """这一行是不是 `R1` / `A2` 条目，是则返回编号；不是返回 None。

    四种写法都要认（`examples/` 与 `templates/` 里都在用）：列表加粗 `- **R1** …`、
    引用块 `> **R1** …`、纯文本 `R1：…` / `R1. …`、表格单列 `| A1 | … |`。

    旧版剥掉列表符号后只要 `R`/`A` 加数字开头就算条目，`A4 纸` 于是变成验收 A4，
    凭空造出 E4/E5/E6（审计 F7）。现在两条收窄：编号后不能紧跟字母/数字（`R2D2` 不算），
    表格外的「A4 纸 / A3 图纸 / A4 幅面」这类纸张规格不算。
    """
    stripped = text.strip()
    probe = ITEM_STRIP_RE.sub("", stripped).lstrip(">*`").lstrip()
    bold = re.match(rf"(?:\*\*|__){letter}\d+(?:\*\*|__)", probe) is not None
    match = re.match(rf"(?:\*\*|__)?{letter}(\d+)(?:\*\*|__)?(?![\dA-Za-z_])", probe)
    if match is None:
        return None
    if not bold:
        rest = probe[match.end():].lstrip()
        if any(rest.startswith(word) for word in PAPER_SPEC_WORDS):
            return None
    return int(match.group(1))


def _strip_cited(text: str) -> str:
    """剥掉行首 `>` 与「…」引号内容，剩下还有字才算作者自己的话。

    口径与 `references/校验清单.md` 的「E9 / W9 的豁免口径」一行一致：`> [用户原话]「要好看」`
    不算，但 `> **R1** 界面要美观` 仍会被抓 —— 加个 `>` 绕不过闸门。注意引号豁免对正文行同样
    生效（`- 触发条件：用户提出「顺便删掉重复文件」→ …` 里的「顺便」是引文，不算作者的模糊词）。
    """
    line = text.strip()
    if line.startswith(">"):
        line = line[1:].strip()
    return re.sub(r"「[^」]*」", "", line).strip()


#: 验收条目「声明」需求号的关键词：写在括号里，或跟在「对应 / 覆盖 / 满足 / 实现 / 见」后面。
REF_DECL_RE = re.compile(r"(?:对应|覆盖|满足|实现|见)\s*[:：]?\s*")


def declared_refs(block: list[str], num: int) -> set[int]:
    """这条 `A#` 声明了哪些需求号（E4 判「有没有引用」、E6 判「需求有没有被覆盖」）。

    三种算声明：**条目行本身**（`- **A1（对应 R1、R7）**：…`）、续行里跟在
    「对应/覆盖/满足/实现/见」后面的 `R#`、以及续行括号里带这些关键词的那一段。

    为什么不是「整块正文里出现 `R#` 就算」：正文里顺带提一句「与 R2 的写法要分开」不算
    覆盖 R2（实测过这种写法能让 R2 的 E6 消失、整份文档一路绿灯）。
    为什么也不只是「条目行」：Markdown 的列表续行属于同一条目，把 `（对应 R1、R7）` 换行
    写在下一行是正常排版 —— 旧版只看标记行，于是同一张条目表两套口径（`判定：` 在续行算数、
    引用在续行不算），撞出 1 个假 E4 + 2 个假 E6，直接卡在「0 ERROR 才允许交付」上。
    """
    refs: set[int] = set()
    # 注释不是正文：`<!-- pf-ignore: W2 见 R9 -->` 不该被当成「这条 A# 声明了 R9」。
    block = strip_comments("\n".join(block)).split("\n")
    for line in block:
        if item_start(line, "A") == num:
            refs |= {int(m) for m in re.findall(r"R(\d+)(?!\d)", line)}
            continue
        for m in REF_DECL_RE.finditer(line):
            refs |= {int(x) for x in re.findall(r"R(\d+)(?!\d)", line[m.end():])}
        for m in re.finditer(r"[（(]([^）)]*)[）)]", line):
            if REF_DECL_RE.search(m.group(1)):
                refs |= {int(x) for x in re.findall(r"R(\d+)(?!\d)", m.group(1))}
    return refs


def item_body(text: str) -> str:
    """条目块去掉开头那截标记（`- **R9** `、`> - **A1（对应 R1、R7）**：`）之后的正文。

    W2 问的是「这条需求有没有数字 / 路径 / 具体对象」，而标记里的编号本身就是数字：直接拿整块
    去搜 `\\d` 会恒命中，规则永远报不出来（实测 `- **R9** 界面要美观好看` 不报 W2）。
    """
    probe = text.strip()
    probe = re.sub(r"^(?:[-*+>|]\s*)+", "", probe)  # 列表 / 引用 / 表格符号
    probe = re.sub(r"^\*+", "", probe)  # `**R9**` 的前半个星号
    probe = re.sub(r"^[RA]\d+", "", probe)  # 编号本身
    probe = re.sub(r"^\*+", "", probe)  # 收尾星号
    probe = re.sub(r"^[（(][^）)]*[）)]", "", probe)  # （对应 R1、R7）
    return probe.lstrip("：: \t")


def collect_items(lines: list[str], letter: str,
                  ranges: list[tuple[int, int]] | None = None) -> tuple[dict[int, list[str]], list[tuple[int, int]]]:
    """收集 `R1` / `A2` 开头的条目；给了 `ranges` 时只认这些行区间里的条目。

    返回 ({编号: 条目正文行}, [(编号, 行号), ...] 重复编号)。条目块在遇到下一条目或下一个标题时结束。
    `lines` 必须已经剥过注释：`<!-- - **R9** 旧版草稿 -->` 不能变成「幽灵需求」逼出 E6
    （审计 F1）。剥注释在 `check_file()` 统一做，这里不再重复。

    **条目归属段落**（审计 R5-gate1-02）：第 8 段按需求逐条列命令、第 9 段写「R2：…」
    步骤、表格首列写 `R1`，这些都是**引用**，不是新条目 —— 旧版全文扫描把它们拼进同号
    条目块、凑成重复编号，合规文档因此被判 `E2b` 红。现在由调用方传入第 4 / 第 7 段
    （含子标题子树）的行区间，别段里的 `R#`/`A#` 一律忽略。`ranges=None` 才是全文扫描，
    只用于「段落根本没绑定上」的兜底（此时 E1 已红，不能再叠一条 E2 误导人）。
    """
    starts: list[tuple[int, int]] = []
    #: 每条条目的行号 → 它所属区间的结束行：块不能越过区间尾巴去吞别段正文。
    span_end: dict[int, int] = {}
    fenced = fence_lines(lines)
    indented = indented_code_lines(lines, fenced)
    for idx, line in enumerate(lines):
        # 围栏里的 `- **R9** 这里只是写法示例` 是**引用的样例**，不是作者的条目：与 `split_sections`
        # （会跳过围栏内的标题）保持同一口径。不跳过就有两个方向的错：围栏示例变成「幽灵需求」
        # 逼出 E6，或者整份文档的条目全写在围栏里却一路绿灯（审计 BUG-B）。缩进代码块同理
        # （4 空格 / Tab 起的示例不是条目，审计 F13）。
        if fenced[idx] or indented[idx]:
            continue
        num = item_start(line, letter)
        if num is not None and ranges is not None:
            owner_end = next((end for start, end in ranges if start <= idx < end), None)
            if owner_end is None:
                continue  # 这一段不是第 4/7 段：`R#`/`A#` 只是引用
            span_end[idx] = owner_end
        if num is not None:
            starts.append((idx, num))

    items: dict[int, list[str]] = {}
    seen: dict[int, int] = {}
    duplicates: list[tuple[int, int]] = []
    for i, (idx, num) in enumerate(starts):
        seen[num] = seen.get(num, 0) + 1
        if seen[num] == 2:
            duplicates.append((num, idx + 1))
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        if idx in span_end:
            end = min(end, span_end[idx])
        block: list[str] = []
        for j in range(idx, end):
            if j != idx and not fenced[j] and not indented[j] and HEADING_RE.match(lines[j]):
                break
            block.append(lines[j])
        items.setdefault(num, []).extend(block)
    return items, duplicates


def check_file(path: Path, use_ignore: bool = True,
               suppressed: list[tuple[Finding, str]] | None = None) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    raw = normalize_invisible(read_text(path))
    # 两套行视图，行号一一对应（strip_comments 用等量换行替换注释）：
    #   text_lines —— 原文：E10 占位符要查注释里/围栏里的残留，pf-ignore 注释本身也是注释；
    #   lines      —— 正文：段落、条目、路径、模糊词都只看它，注释里的东西一律不算（审计 F1）。
    text_lines = split_lines(raw)
    lines = split_lines(strip_comments(raw))
    fenced = fence_lines(lines)
    #: 缩进代码块（4 空格 / Tab，没有围栏）：示例里的条目/标题同样不算正文（审计 F13）。
    indented = indented_code_lines(lines, fenced)

    sections, _preamble = split_sections(lines)
    if not sections:
        findings.append(Finding("ERROR", "E0", "找不到任何 Markdown 标题；长提示词必须按 12 段分节"))
        return findings, len(raw)

    #: 豁免注释：`<!-- pf-ignore: W2 理由 -->`。只对 WARN 生效，写法问题报 E11（见 parse_ignores）。
    #: 用原文行扫：注释本身在正文视图里已经被剥掉了。
    ignores, ignore_problems = parse_ignores(text_lines, fenced=fenced)
    where = section_index(lines, sections)
    findings.extend(ignore_problems)

    def _item_line(letter: str, num: int,
                   ranges: list[tuple[int, int]] | None = None) -> int:
        """条目所在的行号（1 基）；找不到返回 0 —— E4/E5/E6 靠它把误报定位到具体条目。

        `ranges` 与 `collect_items` 同口径：只认第 4/7 段里的条目，别段里同号的引用行
        不能把报错行号带偏。
        """
        for i, ln in enumerate(lines):
            if fenced[i] or indented[i]:
                continue
            if ranges is not None and not any(start <= i < end for start, end in ranges):
                continue
            if item_start(ln, letter) == num:
                return i + 1
        return 0

    # ---- E1 必需段落 -------------------------------------------------------
    # 绑定必须**优先**选带编号的标题，否则文档标题会把段落抢走：
    # `# 任务：按下面的需求做一个图书借阅登记小工具` 里的「需求」会命中第 4 段，
    # 真正的 `## 4 需求（R#）` 就变成「没有 label 的段落」，于是本段该跑的 E9（ERROR 级
    # 模糊词检查）空转，只剩兜底的 W9（WARN 级），整段滑过闸门（实测可以做到 0 ERROR）。
    # 口径与 `references/校验清单.md`「E9/W9 豁免口径」一行的设计一致：绑定错了就等于
    # 那段没被查，且闸门必须出声，不能静默降级。
    used: set[int] = set()
    matched: dict[str, tuple[int, int]] = {}
    #: 段别名「命中」过的每一条标题的行号区间，不只是最终绑定的那一条 —— 见下面的 E9。
    suspects: dict[str, list[tuple[int, int]]] = {}
    #: 全文一级标题的条数：只有「唯一的一条一级标题」才按文档标题跳过（见下）。
    h1_total = sum(1 for _title, level, _start, _end, _numbered in sections if level == 1)
    for label, aliases in REQUIRED_SECTIONS:
        # (序号, 标题, 标题级别, 有没有编号, 号数是否等于本段号, 段内有没有本段条目)
        candidates: list[tuple[int, str, int, bool, bool, bool]] = []
        num = label.split(" ", 1)[0]
        want_num = int(num)
        item_letter = {"4": "R", "7": "A"}.get(num)   # 只有需求段/验收段有「段内条目」判据
        for i, (title, level, start, end, numbered) in enumerate(sections):
            if i in used:
                continue
            # 文档标题（首段 + 一级标题 + 没编号 + 全文唯一的一级标题）不参与绑定：
            # `# 任务：按下面的需求做一个图书借阅登记小工具` 这种标题里的「需求」一旦抢走
            # 第 4 段，真正的 `## 需求（R#）` 就变成「没有 label 的段落」，本段该跑的 E9 空转、
            # 只剩 W9（实测能到 0 ERROR 通过）。
            # 但不能「见首段一级标题就跳过」：12 段全写成一级标题时，首段就是第 1 段本身，
            # 一律跳过会误报「E1 缺少必需段落：1 角色与目标」（假红，见自测用例）。
            if i == 0 and level == 1 and not numbered and h1_total <= 1:
                continue
            if label.startswith("4 ") and ("非目标" in title or "验收" in title):
                continue
            if not alias_hit(title, aliases):
                continue
            # 段里有没有本段自己的条目（`R#` / `A#`）：绑错段落等于那段没被查，
            # 所以「这段里真有条目」比「谁写在更前面」更有说服力（审计用例 C1-hijack-h2sub）。
            has_items = bool(item_letter) and any(
                not fenced[j] and not indented[j] and item_start(lines[j], item_letter) is not None
                for j in range(start + 1, end)
            )
            candidates.append((i, title, level, numbered,
                               heading_number(lines[start]) == want_num, has_items))
        if not candidates:
            findings.append(Finding("ERROR", "E1", f"缺少必需段落：{label}"))
            continue
        # 别名命中过的每一段都记下来：E9 挂在这些区间上（见下面的 E9 循环），
        # 绑定选错了也不会有「模糊词静默降级成 W9」的窗口（审计 BUG-A）。
        if label in ERROR_LEVEL_LABELS:
            suspects[label] = [(sections[c[0]][2], sections[c[0]][3]) for c in candidates]
        # 排序键（从强到弱）：段内真有本段条目 → 带编号 → 号数与本段一致 → 文档序号 → 标题级别。
        # 「段内真有条目」必须排第一：一条空的 `# 4 需求（R#）`、或 `# 3 用户需求说明`
        # （带编号、更靠前、但空）压在真需求段上方时，只按「带不带编号」比会让空段胜出，
        # 真段落的模糊词随之降级成 W9、整份文档 0 ERROR 通过（审计 BUG-A 的两种形态）。
        # 排序键定义成具名函数：W8 文案要说清「为什么是这条胜出」，得能比较两条候选的键。
        def _rank(c: tuple[int, str, int, bool, bool, bool]) -> tuple[bool, bool, bool, int, int]:
            return (not c[5], not c[3], not c[4], c[0], c[2])

        candidates.sort(key=_rank)
        hit = candidates[0]
        used.add(hit[0])
        matched[label] = (sections[hit[0]][2], sections[hit[0]][3])
        # 段落被「没编号」的标题抢走的兜底提示。绑定优先选「段内真有本段条目」的那条，
        # 其次才看带不带编号、号数对不对 —— 所以这里报的是「同段还有一条没编号的标题」：
        # 它自己不危险，但它说明作者在写散标题；一旦带编号的那条被删掉（或本来就没写），
        # 绑定就会落到它身上，按绑定查的检查（例如假设段的 W3）随之空转。
        numbered = [c for c in candidates if c[3]]
        anonymous = [c for c in candidates if not c[3]]
        # 「同级争抢」提示：两条候选同为 N 级标题（如 `## 7. 验收标准（A#）` 与 `## 7.1 验收方法`）——
        # 这层信息必须说出来，否则使用者看不出绑定为什么落在这一条上（审计里就吃过一次
        # 「段名与行号指向两个不同段落」的亏）。「为什么是它胜出」那句话必须按实际比法说：
        # 只有整条排序键都打平才轮到文档顺序，否则就是在说谎
        # （审计复核拿「段内有条目的没编号标题」这种形态驳过一次）。
        same_level = [c for c in candidates if c[2] == hit[2] and c[0] != hit[0]]
        tie_note = ""
        if same_level:
            other = same_level[0]
            # 报标题原行 + 行号，不报归一化后的段名：复制粘贴留下的两条同名标题归一化后
            # 一模一样，只报段名会变成「「X」与「X」同为 1 级标题」（自测用例就有这种输入）。
            hit_raw = lines[sections[hit[0]][2]].strip()
            other_raw = lines[sections[other[0]][2]].strip()
            why = (
                "两条的排序键完全打平，按文档顺序取了靠前的那条"
                if _rank(hit) == _rank(other)
                else "本次比的是「段内有没有本段条目 / 号数对不对」，不是谁写在前面"
            )
            tie_note = (
                f"；另一条同级标题是 `{other_raw}`（第 {sections[other[0]][2] + 1} 行），"
                f"本次选的是 `{hit_raw}`（第 {sections[hit[0]][2] + 1} 行）"
                f"—— {why}"
            )
        if len(candidates) > 1 and numbered and anonymous:
            findings.append(
                Finding(
                    "WARN", "W8",
                    f"第 {num} 段「{label}」有 {len(candidates)} 条标题同时命中：带编号的"
                    + "、".join(f"「{c[1]}」" for c in numbered)
                    + "与没编号的"
                    + "、".join(f"「{c[1]}」" for c in anonymous)
                    + f"。本次绑定用了{'带编号的' if hit[3] else '没编号的'}「{hit[1]}」"
                    + f"（`## {label}` 这种写法最稳），"
                    "但没编号的同义标题会随文档变动抢走绑定，按绑定查的段落检查"
                    "（例如假设段的 W3）可能跟着漂"
                    "（需求段与验收段的模糊词按「命中」判，不受影响）；"
                    f"建议只保留一条 `{label}` 标题，其余同义标题改掉{tie_note}",
                    sections[anonymous[0][0]][2] + 1,
                )
            )
        elif tie_note:
            # 候选**都带编号**时不能说「都没编号」：`## 7. 验收标准（A#）` 与 `## 7.1 验收方法`
            # 都算带编号，文案必须按实际候选说（审计用例 C1-w8b-numbered-tie）。
            same_kind = "都没编号" if not numbered else "都带编号"
            findings.append(
                Finding(
                    "WARN", "W8",
                    f"第 {num} 段「{label}」有 {len(candidates)} 条标题同时命中（{same_kind}）"
                    f"：本次绑定用了「{hit[1]}」{tie_note}；"
                    f"建议只保留一条 `{label}` 标题，其余同义标题改掉",
                    sections[hit[0]][2] + 1,
                )
            )

    # 段位置与子树：E9 要按子树查模糊词，R#/A# 条目也只认绑定段（含子树）里的
    # （审计 R5-gate1-02：第 8 段按需求列命令曾被当成重复需求）。
    def _section_pos(start: int) -> int | None:
        for i, (_title, _level, s, _end, _numbered) in enumerate(sections):
            if s == start:
                return i
        return None

    def _with_subtree(start: int, end: int) -> tuple[int, int]:
        """绑定段 + 它下面的子标题子树。

        `### 输出` 这种子标题里的模糊词也属于需求/验收段：旧版只查绑定段自己的正文，
        子标题内容降级成 W9，等于给 A# 的模糊词留了一条静默通道（审计 F16）。
        只往下扩到「级别比绑定段更深」的连续标题，不做全局扩散。
        """
        pos = _section_pos(start)
        if pos is None:
            return start, end
        level = sections[pos][1]
        last = pos
        while last + 1 < len(sections) and sections[last + 1][1] > level:
            last += 1
        return start, sections[last][3]

    def _item_ranges(label: str) -> list[tuple[int, int]] | None:
        """第 4 / 第 7 段的条目区间（含子标题子树）；绑定不到时返回 None = 全文兜底。

        E1 已经报「缺少必需段落」时不能再因为「条目为空」叠一条 E2：那份文档可能只是
        标题写坏了、条目还在正文里。绑定成功时，其它段里的 `R#`/`A#` 一律算引用
        —— 第 8 段按需求列命令、第 9 段步骤、表格首列写编号都是正常排版（审计 R5-gate1-02）。
        """
        span = matched.get(label)
        return None if span is None else [_with_subtree(span[0], span[1])]

    # ---- R# 需求 -----------------------------------------------------------
    req_ranges = _item_ranges("4 需求（R#）")
    requirements, dup_r = collect_items(lines, "R", ranges=req_ranges)
    if len(requirements) < 3:
        findings.append(
            Finding("ERROR", "E2", f"编号需求不足：找到 {len(requirements)} 条 R#，至少 3 条（形如 `R1 ...`）")
        )
    for num, line_no in dup_r:
        findings.append(Finding("ERROR", "E2b", f"需求编号 R{num} 重复出现（编号必须唯一）", line_no))

    # ---- A# 验收 -----------------------------------------------------------
    acc_ranges = _item_ranges("7 验收标准（A#）")
    acceptances, dup_a = collect_items(lines, "A", ranges=acc_ranges)
    for num, line_no in dup_a:
        findings.append(Finding("ERROR", "E3b", f"验收编号 A{num} 重复出现（编号必须唯一）", line_no))
    if len(acceptances) < 3:
        findings.append(
            Finding("ERROR", "E3", f"编号验收不足：找到 {len(acceptances)} 条 A#，至少 3 条（形如 `A1（对应 R1）...`）")
        )
    referenced: set[int] = set()
    for num, block in sorted(acceptances.items()):
        # E4 / E6 的判据是「这条 A# 自己声明的需求号」—— 见 declared_refs()：条目行、续行里
        # 「对应/覆盖/…」后面的、以及带这些关键词的括号段。正文里顺带提一句别的 R# 不算覆盖。
        refs = declared_refs(block, num)
        if not refs:
            findings.append(Finding("ERROR", "E4", f"验收 A{num} 没有引用任何需求编号（应写「对应 R1」）",
                                    _item_line("A", num, acc_ranges)))
        referenced |= refs
        # 判定归属这条 A# 自己的条目行/续行：同块的子条目、表格行、围栏示例都不算
        # （审计 R5-gate1-04：A3 本体没写判定，块内示例里的 `判定：命令` 曾让它假绿）。
        if judge_kind(judge_text(block)) is None:
            findings.append(
                Finding("ERROR", "E5", f"验收 A{num} 没有 `判定：`，或没写判定方式（命令 / 阈值 / 人工 三者之一）",
                        _item_line("A", num, acc_ranges))
            )
    for num in sorted(set(requirements) - referenced):
        findings.append(Finding("ERROR", "E6", f"需求 R{num} 没有任何验收条目覆盖它",
                                _item_line("R", num, req_ranges)))

    # ---- E7 非目标 ---------------------------------------------------------
    goal_label = "5 非目标"
    if goal_label in matched:
        start, end = matched[goal_label]
        body = body_of(lines, start, end)
        if is_empty_claim(body):
            findings.append(Finding("ERROR", "E7", "非目标段落为空或只写了「无」；至少写 1 条不做的事"))
        else:
            # W6 数的是「条数」，不是「行数」：1 条非目标后面跟一行续行说明，或者写成
            # 单列表格（表头 + 分隔行 + 1 条数据），都仍然只写了 1 条。旧判据等价于
            # 「§5 总共只有 1 行非空」，上面两种写法都会漏报（第 8 轮审计 R8-03）。
            body_lines = [ln for ln in split_lines(body) if ln.strip()]
            bullets = [ln for ln in body_lines
                       if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", ln)]
            table_rows = [ln for ln in body_lines if ln.strip().startswith("|")]
            if bullets or table_rows:
                # 表格前两行是表头与分隔行，剩下的才算条目。
                data_rows = table_rows[2:] if len(table_rows) >= 2 else []
                count = len(bullets) + len(data_rows)
            else:
                count = len(body_lines)
            if count < 2:
                # 带行号 = 豁免注释只在 §5 内生效（无行号会被当成文件级，别的段写注释也能压掉）。
                findings.append(Finding(
                    "WARN", "W6", "非目标只写了 1 条，建议补到 2 条以上", start + 1))

    # ---- E8 交付物路径 -----------------------------------------------------
    deliv_label = "3 交付物"
    if deliv_label in matched:
        start, end = matched[deliv_label]
        body = body_of(lines, start, end)
        if not has_path_token(body):
            findings.append(
                Finding("ERROR", "E8", "交付物段落里没有任何带扩展名的路径（如 `out/report.csv`）")
            )
        _flag_vague(findings, "交付物", body, start, "WARN")

    # ---- E9 模糊词 ---------------------------------------------------------
    # `_section_pos` / `_with_subtree` 定义在 E1 绑定之后（R#/A# 条目归属也用它）。
    # 用段落的实际行号区间做去重键：标题字数由使用者决定（`## 4. 需求（R#）` 或 `## 需求（R#）` 都合法），
    # 拿 label 字符串比对会在不编号的标题上失配，同一段就会被 E9 和 W9 各报一次。
    checked: set[tuple[int, int]] = set()
    #: E9 查过的行区间（含子树）：W9 兜底循环必须跳过这些行，否则子树里的模糊词又被报一遍。
    error_spans: list[tuple[int, int]] = []
    for label in ERROR_LEVEL_LABELS:
        ranges = list(suspects.get(label, ()))
        bound = matched.get(label)
        if bound is not None and bound not in ranges:
            ranges.insert(0, bound)
        bound_span = _with_subtree(bound[0], bound[1]) if bound is not None else None
        for start, end in ranges:
            if bound_span is not None and (start, end) != bound and \
                    bound_span[0] <= start and end <= bound_span[1]:
                # 命中别名的标题就在绑定段的子树里（`### 输出验收`）：子树已经按 ERROR 查过，
                # 再按另一条标题查一遍会对同一行报两条 E9（审计 F16 的 37b 夹具）。
                continue
            span = bound_span if bound_span is not None and (start, end) == bound else (start, end)
            if span in checked:
                continue  # 同一条标题被两个别名命中时只查一遍
            checked.add(span)
            error_spans.append(span)
            if bound is not None and (start, end) != bound:
                # 这一段只是标题**命中**了别名，不是最后绑定的那一段（比如 `## 需求变更由谁说了算`）。
                # 报出来时点名它自己的标题：否则用户跑到行号那一看是别的标题，会以为闸门指错了地方。
                shown = display_heading(lines[start]) or label.split(" ", 1)[1]
            else:
                shown = label.split(" ", 1)[1]
            _flag_vague(findings, shown, body_of(lines, span[0], span[1]), start, "ERROR")

    for i, (title, _level, start, end, _numbered) in enumerate(sections):
        if any(s <= start and end <= e for s, e in error_spans):
            continue  # 这一段已由 E9 按 ERROR 查过（含绑定段的子树），不要再报一遍 W9
        _flag_vague(findings, title, body_of(lines, start, end), start, "WARN")

    # ---- E10 占位符 --------------------------------------------------------
    # 用原文行：占位符写在哪里都算残留（注释里、围栏里都是没改完的证据）。
    # 但「占位符」是词不是子串：`docs/TODO.md` 是文件名、`TODOs` 是英文单词，都不能算；
    # 反过来小写 `todo` 也是没改完的证据，要抓（审计 F11）。
    for idx, line in enumerate(text_lines, start=1):
        probe = CODE_SPAN_RE.sub("", line)
        probe = strip_urls(probe)
        probe = strip_file_tokens(probe)
        token = placeholder_hit(probe)
        if token is not None:
            findings.append(Finding("ERROR", "E10", f"残留占位符 `{token}`", idx))

    # ---- 其余警告 ----------------------------------------------------------
    # 行号不是装饰：`pf-ignore` 的作用域按「注释与被豁免的 WARN 在同一段」判，
    # 所以这几条也得说清自己在哪一段（拿段落标题行当锚点）。
    def _section_line(label: str) -> int:
        span = matched.get(label)
        return span[0] + 1 if span else 0

    if len(raw) < 600:
        findings.append(Finding("WARN", "W1", f"全文只有 {len(raw)} 字符，信息量可能不足以让陌生 AI 开工"))
    if len(acceptances) > 25:
        findings.append(Finding("WARN", "W4", f"验收条目 {len(acceptances)} 条偏多，考虑合并",
                                _section_line("7 验收标准（A#）")))
    if len(requirements) >= 15:
        findings.append(Finding("WARN", "W5", f"需求条目 {len(requirements)} 条偏多，考虑分层（必须有 / 可选）",
                                _section_line("4 需求（R#）")))

    for num, block in sorted(requirements.items()):
        # requirements 是 collect_items 在**已剥注释**的行上收集的（见 check_file 的两套行视图），
        # 所以这里不用再剥一次；注释里的数字不会混进来骗过下面的 NUMBER_RE。
        text = "\n".join(block)
        line_no = _item_line("R", num, req_ranges)
        # 只看标记（`- **R9**`）之后的正文：标记里的编号是数字，会骗过下面的 NUMBER_RE。
        body = item_body(text)
        # 代码段（`openpyxl`）本身就是「具体对象」，所以有代码段时照样豁免。
        joined = CODE_SPAN_RE.sub("", body)
        if not NUMBER_RE.search(joined) and not has_path_token(joined) and "`" not in body:
            findings.append(
                Finding(
                    "WARN",
                    "W2",
                    f"需求 R{num} 没有任何数字/路径/具体对象，可能不可验证"
                    "（量化写在配对的 A# 里时可以忽略本条）",
                    line_no,
                )
            )
        # 只抓「A，并且 B」这种并列句式；「三类：…、以及 …」是列举，「42 人同时提交」是场景，都不算。
        for word in ("并且", "以及", "同时"):
            if re.search(r"[，,]\s*" + word, joined):
                findings.append(Finding("WARN", "W7", f"需求 R{num} 里出现「，{word}」，可能是一句多需求，考虑拆成两条",
                                        line_no))
                break

    assume_label = "10 假设与开放问题"
    if assume_label in matched:
        start, end = matched[assume_label]
        if is_empty_claim(body_of(lines, start, end)):
            findings.append(Finding("WARN", "W3", "假设段为空；一条假设都没有，通常说明替用户拍板了",
                                    start + 1))

    if use_ignore and ignores:
        findings, dropped = apply_ignores(findings, ignores, where)
        if suppressed is not None:
            suppressed.extend(dropped)

    return findings, len(raw)


def _flag_vague(findings: list[Finding], label: str, body: str, start: int, level: str) -> None:
    """段落内的模糊词：逐行扫，行号按原段落偏移算。

    旧版先对整段做 `CODE_SPAN_RE.sub("", ...)` 再切行：行内代码一旦跨行，换行就被删掉，
    后面的行号整体前移（同一句话 E9 报第 22 行、W2 报第 24 行，审计 F9）。现在逐行剥
    行内代码，行数不变；围栏里的行跳过 —— 围栏里的示例不是作者的话（审计 31-fence-vague）。
    """
    body_lines = split_lines(strip_comments(body))
    # 只按 \n 切行：splitlines() 会把 U+2028 之类当换行，行号整体漂移（审计 F8）。
    fenced = fence_lines(body_lines)
    for idx, line in enumerate(body_lines):
        if fenced[idx]:
            continue
        probe = _strip_cited(CODE_SPAN_RE.sub("", line))
        if not probe:
            continue  # 空行，或整行都是引文（`> [用户原话]「…」`）：引用原文里的词不算你的锅
        if probe.startswith("#"):
            continue  # 标题行是段落名本身，不算内容
        for word in VAGUE_WORDS:
            if word in probe:
                findings.append(
                    Finding(level, "E9" if level == "ERROR" else "W9",
                            f"「{label}」段出现模糊词「{word}」，需换算成数字/路径/判定方式", start + idx + 2)
                )
                break


def parse_ignores(lines: list[str], codes: tuple[str, ...] = IGNORE_CODES,
                  fenced: list[bool] | None = None,
                  error_codes: tuple[str, ...] = ERROR_CODES) -> tuple[list[tuple[int, str, str]], list[Finding]]:
    """收集 `<!-- pf-ignore: W2 为什么这条可以忽略 -->`。

    返回 `([(注释所在行的 0 基下标, 码, 理由)], [写法有问题的 Finding])`。
    写法问题一律 ERROR（E11）：豁免是「我知道这条 WARN 在说什么、为什么它不影响执行」的声明，
    不是关闸门的开关。所以缺码、缺理由、码不认识、或者想豁免 ERROR，都在这里被拦下。

    `codes` 是「本闸门能豁免的码」：闸门① 只认自己的 `W1…W9`；闸门③ 传 `W1…W9 + C2/C4/C5`，
    这样 `<!-- pf-ignore: C2 … -->` 在闸门③ 里算数、在闸门① 里会老老实实报「不认识这个码」。
    `error_codes` 是「本闸门里属于 ERROR、不能豁免的码」：默认就是 `ERROR_CODES`；闸门③ 把
    `C1`/`C3` 也传进来，于是 `pf-ignore: C1` 明确报「是 ERROR」而不是「不认识」（审计 P31/P32）。

    只认**注释块**里的写法：围栏 / 缩进代码块 / 行内代码里的 `pf-ignore` 是写法示例，
    既不豁免任何东西，也不该逼出一条假 E11（审计 F5 / R6-gate1-05）。注释块允许跨行，
    理由按空白拼成一句（审计 F12）；注释区间用 `_comment_spans()` 线性扫描（R6-gate1-02）。
    """
    ignores: list[tuple[int, str, str]] = []
    problems: list[Finding] = []
    text = "\n".join(lines)
    if fenced is None:
        fenced = fence_lines(lines)
    indented = indented_code_lines(lines, fenced)
    for c_start, c_end in _comment_spans(text):
        body = text[c_start + 4:c_end - 3]
        if "pf-ignore" not in body:
            continue
        idx = text.count("\n", 0, c_start)
        if idx < len(fenced) and fenced[idx]:
            continue  # 围栏里的示例不是真豁免
        if idx < len(indented) and indented[idx]:
            continue  # 缩进代码块里的示例不是真豁免
        line_start = text.rfind("\n", 0, c_start) + 1
        if lines[idx][: c_start - line_start].count("`") % 2 == 1:
            continue  # 行内代码里的示例不是真豁免
        hit = IGNORE_RE.match(body.strip())
        if not hit:
            problems.append(
                Finding("ERROR", "E11",
                        "`pf-ignore` 没写码（写法：`<!-- pf-ignore: W2 为什么这条可以忽略 -->`）",
                        idx + 1)
            )
            continue
        code_hit = re.match(r"^([A-Za-z]\d+[a-z]?)\s*(.*)$", hit.group(1).strip(), re.S)
        if not code_hit:
            problems.append(
                Finding("ERROR", "E11",
                        "`pf-ignore` 没写码（写法：`<!-- pf-ignore: W2 为什么这条可以忽略 -->`）",
                        idx + 1)
            )
            continue
        code = code_hit.group(1).upper()
        # 跨行注释的理由可能被排版折成几行：拼成一句再判长度，不静默丢弃（审计 F12）。
        reason = " ".join(code_hit.group(2).split())
        if code in error_codes:
            problems.append(
                Finding("ERROR", "E11",
                        f"`pf-ignore: {code}` 无效：豁免只对 WARN 生效，{code} 是 ERROR，"
                        "必须改掉才算过闸门", idx + 1)
            )
            continue
        if code not in codes:
            problems.append(
                Finding("ERROR", "E11",
                        f"`pf-ignore: {code}` 不认识这个码（本闸门可豁免的只有 {codes_hint(codes)}）",
                        idx + 1)
            )
            continue
        if len(reason) < IGNORE_REASON_MIN:
            problems.append(
                Finding("ERROR", "E11",
                        f"`pf-ignore: {code}` 必须写理由（至少 {IGNORE_REASON_MIN} 个字）："
                        "下一个人要靠它判断这是有意的取舍还是漏改", idx + 1)
            )
            continue
        ignores.append((idx, code, reason))
    return ignores, problems


def section_index(lines: list[str], sections: list[tuple[str, int, int, int, bool]]) -> dict[int, tuple[int, int]]:
    """行号（0 基）-> 段落区间 `(起始行, 结束行)`。

    用区间而不是标题字符串：两个同名段（`## 备注` 出现两次）会共享同一个标题名，旧版
    于是让一条豁免注释跨段生效（审计 F10）。区间唯一，同名段也互不串。
    """
    where: dict[int, tuple[int, int]] = {}
    for _title, _level, start, end, _numbered in sections:
        for idx in range(max(start, 0), min(end, len(lines))):
            where.setdefault(idx, (start, end))
    return where


def apply_ignores(
    findings: list[Finding],
    ignores: list[tuple[int, str, str]],
    where: dict[int, tuple[int, int]],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """按注释豁免 WARN，返回 `(留下的 findings, [(被豁免的 finding, 理由)])`。

    作用域：注释与它要豁免的 WARN 在**同一段**里才算数（把 `pf-ignore: W2` 写在别处，
    下一个人看到那条 WARN 时根本不知道有人处理过）。段落按行号区间判，同名段不会串。
    不带行号的检查（W1 全文长度）是文件级，写在哪儿都算 —— 它说的就是整份文件。
    """
    kept: list[Finding] = []
    dropped: list[tuple[Finding, str]] = []
    for f in findings:
        reason = None
        if f.level == "WARN":
            for idx, code, why in ignores:
                if code != f.code:
                    continue
                if not f.line:
                    reason = why
                    break
                here = where.get(f.line - 1)
                target = where.get(idx)
                if here is not None and target is not None and here == target:
                    reason = why
                    break
        if reason is None:
            kept.append(f)
        else:
            dropped.append((f, reason))
    return kept, dropped


def report(path: Path, use_ignore: bool = True) -> int:
    suppressed: list[tuple[Finding, str]] = []
    try:
        findings, size = check_file(path, use_ignore=use_ignore, suppressed=suppressed)
    except FileReadError as exc:
        # 读不了文件也是「这一关没过」：打印一行 ERROR 而不是 Python 堆栈（审计 F2）。
        print(f"\n=== 检查 {path} ===")
        print(f"ERROR: {exc}")
        print("结果：1 ERROR, 0 WARN —— 未通过")
        return 1
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]
    print(f"\n=== 检查 {path} （{size} 字符） ===")
    for f in sorted(findings, key=lambda x: (x.level != "ERROR", x.line)):
        print(f.render())
    for f, why in suppressed:
        print(f"豁免: {f.render()} —— 理由：{why}")
    if errors:
        print(f"结果：{len(errors)} ERROR, {len(warns)} WARN —— 未通过")
    else:
        tail = "（WARN 需逐条说明为什么不影响执行）" if warns else ""
        if suppressed:
            tail = f"（另有 {len(suppressed)} 条 WARN 按 `pf-ignore` 注释豁免，`--no-ignore` 可看全）"
        print(f"结果：0 ERROR, {len(warns)} WARN —— 通过" + tail)
    return 1 if errors else 0


SELFTEST_FIXTURE = """# 任务：给学生会做一个图书借阅登记小工具

## 1. 角色与目标
你是 Python 工程师。目标：把借阅登记表导出成 CSV，交付物是 out/borrow.csv。

## 2. 背景与已知事实
现有登记数据在 data/borrow.xlsx，共 4 列：书名、借阅人、借出日期、归还日期。[用户原话]
每周五花 15 分钟手工抄一遍。[AI 推断]

## 3. 交付物
| 路径 | 格式 | 数量 |
| --- | --- | --- |
| out/borrow.csv | UTF-8，只输出本周新增行 | 1 个 |

## 4. 需求（R#）
- **R1** 读取 data/borrow.xlsx 的 4 列，写出 out/borrow.csv，缺的归还日期写空字符串。
- **R2** 输出界面要美观，空行最多跳过 3 行。
- **R3** 入口是 scripts/borrow.py，正常退出码 0，异常退出码 1。

## 5. 非目标
- 不做 Web 界面，也不写前端页面。
- 不接数据库，不写定时任务。

## 6. 技术约束
- Python 3.12，只用标准库加 openpyxl 3.1。
- 脚本统一放 scripts/ 目录，不新建包。

## 7. 验收标准（A#）
- **A1（对应 R1）** 跑完生成 out/borrow.csv 且 4 列齐全。判定：命令 `python scripts/borrow.py`。
- **A2（对应 R2）** 遇到空行不中断。判定：阈值 跳过行数 ≤ 3。
- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。

## 8. 验证方式
```bash
python scripts/borrow.py
```
期望输出：out/borrow.csv 生成，退出码 0。

## 9. 执行步骤
1. 读表并写出 out/borrow.csv（对应 R1）
2. 末尾统一跳过空行（对应 R2）
3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）

## 10. 假设与开放问题
- 假设表头在第 1 行（未确认：若表头在第 2 行要改 header 参数）。

## 11. 何时必须停下来问人
- 工作表数量超过 1 个时，停下来问人再继续。
- 需要新增第三方依赖时，停下来问人。
- 表头不在第 1 行时，停下来问人再确认列名。

## 12. 汇报格式
1. 改了什么（文件清单）
2. 怎么验的（贴命令与输出）
"""


def selftest() -> int:
    root = Path(__file__).resolve().parent.parent
    examples = sorted((root / "examples").glob("*.prompt.md"))
    if not examples:
        print("ERROR: examples/ 下找不到 *.prompt.md，无法自测")
        return 1
    failed = 0
    for path in examples:
        findings, _ = check_file(path)
        errors = [f for f in findings if f.level == "ERROR"]
        expect_bad = "反例" in path.name
        if expect_bad:
            ok = len(errors) >= 5
            want = "期望 >=5 ERROR"
        else:
            ok = len(errors) == 0
            want = "期望 0 ERROR"
        print(f"{'PASS' if ok else 'FAIL'}  {path.name}: {len(errors)} ERROR（{want}）")
        if not ok:
            failed += 1
            for f in errors[:8]:
                print("      " + f.render())

    # 仓库自带的骨架模板是给人抄的，它自己必须先干净：0 ERROR 0 WARN。
    # 它同时是「文档标题不许带段落别名」的活样例 —— 标题里一旦塞进「目标」，
    # 第 1 段就会多一条同义标题，W8 会在这里变红（审计里就是这么发现的）。
    skeleton = root / "templates" / "提示词骨架.md"
    if not skeleton.is_file():
        print("FAIL  templates/提示词骨架.md: 找不到这个文件")
        failed += 1
    else:
        skeleton_findings, _ = check_file(skeleton)
        s_errors = [f for f in skeleton_findings if f.level == "ERROR"]
        s_warns = [f for f in skeleton_findings if f.level == "WARN"]
        ok = not s_errors and not s_warns
        print(
            f"{'PASS' if ok else 'FAIL'}  templates/提示词骨架.md: "
            f"{len(s_errors)} ERROR, {len(s_warns)} WARN（期望 0 ERROR 0 WARN）"
        )
        if not ok:
            failed += 1
            for f in skeleton_findings[:8]:
                print("      " + f.render())

    # 每个 case 固定「期望报出的错误码集合」。加入 case 前请先用真实文件跑一遍确认，
    # 不要照着直觉写期望值。这里放的是审计中真出现过的绕过手法。
    # 每个 case 的输入改动都写在 `build` 里：不要靠共享的中间变量，容易静默不生效。
    base_title = SELFTEST_FIXTURE.splitlines()[0]
    clean_title = "# 任务：做一个借阅登记小工具"     # 中性标题：不带任何段落别名
    hijack_title = "# 任务：按下面的需求做一个图书借阅登记小工具"
    vague_line = "输出界面要美观，"
    plain_r3 = "- **R3** 入口是 scripts/borrow.py，正常退出码 0，异常退出码 1。"
    #: 一条没有数字 / 路径 / 代码段的需求：专门用来喂出 W2（豁免用例需要一个 WARN 当靶子）。
    plain_r3_new = "- **R3** 界面要顺手。"
    #: 夹具里那条完整、合法的 A3：「判定」写法相关的用例都在这条上做替换。
    a3_line = "- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。"
    #: §5 原始的两条非目标（夹具锚点），以及「只写 1 条」的两种常见排版变形。
    s5_all = "- 不做 Web 界面，也不写前端页面。\n- 不接数据库，不写定时任务。"
    s5_one_cont = "- 不做 Web 界面。\n  （命令行就够，用户不需要图形界面。）"
    s5_one_table = "| 不做的方向 | 原因 |\n| --- | --- |\n| Web 界面 | 用户只用命令行 |"

    def sub(text: str, old: str, new: str) -> str:
        """替换前先断言目标真的在文本里。

        夹具漂移（标题改个点、改个词）时 `str.replace` 会**静默返回原文**，用例就变成
        「空跑也 PASS」—— 几个用例一起空跑，`--selftest` 照样打印「全部通过」。
        这里把静默失败变响：目标找不到就抛 AssertionError，由下面的循环记 FAIL。
        """
        if old not in text:
            raise AssertionError(f"替换目标不存在（夹具漂移了？）：{old!r}")
        return text.replace(old, new, 1)

    def from_clean() -> str:
        """基准件：把 R2 里的模糊词换成可判定的写法，这样 E9 只由 case 自己引入。"""
        return sub(SELFTEST_FIXTURE, vague_line, "输出控制在两行以内，")

    def with_plain_r3() -> str:
        """把 R3 换成一条没有数字/路径/具体对象的需求：豁免用例拿它当 W2 的靶子。"""
        return sub(from_clean(), plain_r3, plain_r3_new)

    def insert_before_requirement(heading: str) -> str:
        """在真正的第 4 段前插一条同义标题：它带编号/数字开头时曾经把绑定抢走。"""
        return sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", f"{heading}\n\n## 4. 需求（R#）")

    def all_h1() -> str:
        """去掉文档标题，把 12 段全写成一级标题（12 段契约没有规定标题级别）。"""
        text = sub(SELFTEST_FIXTURE, base_title + "\n\n", "")
        for n in range(1, 13):
            text = sub(text, f"## {n}. ", f"# {n}. ")
        return text

    inline = [
        # 标题里带「需求」二字，不该把第 4 段的绑定抢走：E9 必须照样命中「美观」
        ("标题含需求词", lambda: sub(SELFTEST_FIXTURE, base_title, hijack_title), {"E9"}),
        # 对照：标题里没有段落别名（且与夹具标题不同）、R2 已可判定时，本该一条 ERROR 都没有
        ("标题中性", lambda: sub(from_clean(), base_title, clean_title), set()),
        # 反引号包起来的模糊词：现有设计整段豁免，必须整段不报（不是漏报，是口径）
        ("反引号豁免", lambda: sub(from_clean(), "输出控制在两行以内，", "输出界面要`美观`，"), set()),
        # 代码围栏里的占位符也要抓：TODO 不管写在哪都算残留
        ("围栏内占位符",
         lambda: sub(from_clean(), "## 6. 技术约束", "## 6. 技术约束\n```text\nTODO: 待补\n```"),
         {"E10"}),
        # 标题编号写成「包裹式」`（4）` 时，它仍然算「带编号的正文段落」，
        # 不该因为形式不同就被一级标题抢走绑定（否则 E9 静默失效）。
        ("标题编号全角括号",
         lambda: sub(sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "## （4）需求（R#）"),
                     base_title, hijack_title), {"E9"}),
        ("标题编号半角括号",
         lambda: sub(sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "## (4) 需求（R#）"),
                     base_title, hijack_title), {"E9"}),
        # 仓库 12 段契约自己的写法（数字 + 空格、没有分隔符）必须继续算「带编号」
        ("标题编号空格式",
         lambda: sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "## 4 需求（R#）"), {"E9"}),
        # 需求段**完全不编号**（`## 需求（R#）`）时，文档标题里那两个「需求」也不能把它抢走：
        # 旧排序键里「浅的优先」会让一级标题胜出，于是 E9 降级成 W9、整份文档 0 ERROR 通过。
        # 标题必须带「需求」二字才真的构成争抢，否则 H1 压根不参与候选、这条用例就是空跑。
        ("标题抢走未编号的需求段",
         lambda: sub(sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "## 需求（R#）"),
                     base_title, hijack_title), {"E9"}),
        # 反向对照：真的把需求段写成一级标题（`# 需求（R#）`）是有意结构，不能被新规则误伤。
        ("需求段写在一级标题",
         lambda: sub(sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "# 需求（R#）"),
                     base_title, hijack_title), {"E9"}),
        # 更浅的带编号同义标题（`# 3 用户需求说明`）曾经排在 `## 4. 需求（R#）` 前面：
        # 旧排序键第二项是「标题级别」，一级永远赢二级 —— E9 空转成 W9，连 W8 都不报。
        ("更浅的带编号同义标题",
         lambda: insert_before_requirement("# 3 用户需求说明"), {"E9"}),
        # 数字开头的段名不算编号：`3D` / `4K` / `100 分` 曾经被判成「编号 3/4/100」而抢走绑定。
        ("数字开头的段名 3D",
         lambda: insert_before_requirement("## 3D 需求素材说明"), {"E9"}),
        ("数字开头的段名 4K",
         lambda: insert_before_requirement("## 4K 需求素材说明"), {"E9"}),
        ("数字开头的段名 100 分",
         lambda: insert_before_requirement("## 100 分需求说明"), {"E9"}),
        # `4K` 段里也有 `R#` 条目时，只有「4K 不是编号」这一条能救场：旧正则把它当编号 4，
        # 于是与真需求段在排序键的前三层上全部打平，最后靠文档顺序取胜（E9 照样空转）。
        # 期望不含 E6：`## 4K 需求素材说明` 没绑定成第 4 段（绑的是后面真正的 `## 4. 需求（R#）`），
        # 按 R5-gate1-02 的条目归属规则，别段里的 `R9` 是引用、不算新需求 —— 旧版报 E6 正是
        # 「全文扫条目」的副产品（第 8/9 段按需求列命令会被同类误报成重复需求）。
        ("数字开头的段名 4K（段内也有条目）",
         lambda: sub(SELFTEST_FIXTURE, "## 4. 需求（R#）",
                     "## 4K 需求素材说明\n\n- **R9** 素材必须真 4K，不缩放。判定：人工 看素材尺寸。\n\n"
                     "## 4. 需求（R#）"),
         {"E9"}),
        # 一条**空的**带编号标题压在真需求段上方：两条候选都带编号，旧排序键按「号数相符」
        # 选中了那条空的，真段落的模糊词降级成 W9（审计 BUG-A 的第二种形态）。
        # 判据必须优先看「段内真有条目」，而不是谁带编号、谁号数对得上。
        ("空的一级同义标题压着真需求段",
         lambda: sub(SELFTEST_FIXTURE, "## 4. 需求（R#）", "# 4 需求（R#）\n\n## 需求（R#）"),
         {"E9"}),
        # 带编号的浅层同义标题 + **未编号**的真需求段：审计 BUG-A 的第一形态
        # （旧排序键第一键是「带编号」，空标题永远赢过装着全部条目的真段落）。
        ("带编号的同义标题压着未编号的需求段",
         lambda: sub(SELFTEST_FIXTURE, "## 4. 需求（R#）",
                     "# 3 用户需求说明\n\n（这里只是一句概述。）\n\n## 需求（R#）"),
         {"E9"}),
        # 反过来：**命中了别名但不是最终绑定**的那条标题（`## 需求变更由谁说了算`）也得按 E9 查。
        # 判据已经从「绑定」改成「命中」，这条用例防止它被悄悄退回「只查绑定段」。
        ("命中别名的另一条标题也要查",
         lambda: sub(from_clean(), "## 5. 非目标",
                     "## 需求变更由谁说了算\n\n- 变更流程要美观。\n\n## 5. 非目标"),
         {"E9"}),
        # 围栏里的 `R9` 是写法示例，不是作者的条目：不能变成「幽灵需求」逼出 E6（审计 BUG-B 误报侧）。
        ("围栏里的需求示例不算条目",
         lambda: sub(from_clean(), "## 4. 需求（R#）\n",
                     "## 4. 需求（R#）\n\n格式示例：\n\n```text\n- **R9** 这里只是写法示例\n```\n"),
         set()),
        # 反向：条目**全**写在围栏示例里 = 正文一条都没有，必须报编号不足（审计 BUG-B 漏报侧）。
        # 围栏里要给满 3 条 R# / 3 条 A#：只放 1 条时剥不剥围栏都凑不满 3 条，用例就成了空跑。
        ("条目全写在围栏示例里",
         lambda: sub(sub(from_clean(),
                         "- **R1** 读取 data/borrow.xlsx 的 4 列，写出 out/borrow.csv，缺的归还日期写空字符串。\n"
                         "- **R2** 输出控制在两行以内，空行最多跳过 3 行。\n"
                         "- **R3** 入口是 scripts/borrow.py，正常退出码 0，异常退出码 1。",
                         "```text\n- **R1** 读取 data/borrow.xlsx 的 4 列，写出 out/borrow.csv。\n"
                         "- **R2** 输出控制在两行以内，空行最多跳过 3 行。\n"
                         "- **R3** 入口是 scripts/borrow.py，正常退出码 0，异常退出码 1。\n```"),
                     "- **A1（对应 R1）** 跑完生成 out/borrow.csv 且 4 列齐全。判定：命令 `python scripts/borrow.py`。\n"
                     "- **A2（对应 R2）** 遇到空行不中断。判定：阈值 跳过行数 ≤ 3。\n"
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。",
                     "```text\n- **A1（对应 R1）** 跑完生成 out/borrow.csv。判定：命令 `python scripts/borrow.py`。\n"
                     "- **A2（对应 R2）** 遇到空行不中断。判定：阈值 跳过行数 ≤ 3。\n"
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。\n```"),
         {"E2", "E3"}),
        # 标题写在围栏里不算段落（与条目侧同一口径）。撤销 `split_sections` 里的围栏跳过时，
        # 这条用例必须变红 —— 此前这半条口径一条守门用例都没有，`--selftest` 全绿是假的。
        ("围栏里的标题不算段落",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "```text\n## 4. 需求（R#）\n```"),
         {"E1"}),
        # `~~~` 也是围栏（CommonMark 两种都认）：文档只写 ``` 时，这条用例看住实现。
        ("波浪线围栏也算围栏",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "~~~text\n## 4. 需求（R#）\n~~~"),
         {"E1"}),
        # 12 段全写成一级标题、没有文档标题时，首段就是第 1 段本身：
        # 旧规则一律跳过「首段 + 一级标题」→ 假红 `E1 缺少必需段落：1 角色与目标`。
        ("12 段全写成一级标题", all_h1, {"E9"}),
        # A# 块里的正文顺带提一句 R2 不算 R2 被覆盖：E6 的判据只看这条 A# 自己声明的 R#。
        # （基准件用 from_clean()：否则 R2 自带的模糊词会先报 E9，把 E6 这条信号淹掉。
        #  提到的 R2 必须写在**正文行**上，写在条目行上就真的是这条 A# 的声明了。）
        ("验收正文提到别的需求",
         lambda: sub(from_clean(), "- **A2（对应 R2）**",
                     "- **A2（对应 R1）**\n  这里只是解释：与 R2 的写法要分开，别混在一起"),
         {"E6"}),
        # 把「（对应 R1、R7）」换行写到续行是正常排版：同一张条目表不能两套口径
        # （`判定：` 写在续行算数、引用写在续行却报 E4，还连带两条假 E6 —— 审计撞出来的）。
        ("引用写在续行也算数",
         lambda: sub(from_clean(), "- **A2（对应 R2）**",
                     "- **A2**\n  （对应 R2）遇到空行不中断"),
         set()),
        # 反向：续行括号里的 `R#` 只有带「对应/覆盖/满足/实现/见」才算声明，
        # `（与 R2 的写法要分开）` 不算覆盖 R2。
        ("续行括号里没有对应词不算覆盖",
         lambda: sub(from_clean(), "- **A2（对应 R2）**",
                     "- **A2（对应 R1）**\n  （与 R2 的写法要分开，别混在一起）"),
         {"E6"}),
        # W8 有两条渲染路径（`references/校验清单.md` 的 W8 行分别写了这两种情况），
        # 以前一条用例都没有：W8 是 WARN，只比对 ERROR 集合的自测根本看不见它。
        # 用例一：一条带编号、一条没编号 —— 绑定优先选带编号的，但要提示歧义。
        ("同义标题：一条带编号一条没编号",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "## 需求（R#）\n\n## 4. 需求（R#）"),
         set(), {"W8"}),
        # 用例二：两条同级别、都没编号 —— 排序键分不出高下，只能按文档顺序取，文案要说「都没编号」。
        ("同义标题：两条都没编号",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "## 需求（R#）\n\n## 需求（R#）"),
         set(), {"W8"}),
        # 用例三：两条同级别、都带编号（`7` 与 `7.1`）—— 靠「段内真有 A# 条目」才选中 7 段，
        # 文案要说「都带编号」（旧文案在这种情况下谎报「都没编号」，审计用例 C1-w8b-numbered-tie）。
        # 子标题必须插在 A# 条目**之后**：插在段落标题紧下面，条目就归 7.1 了，绑定落到 7.1 反而是对的。
        ("同义标题：两条都带编号（子标题）",
         lambda: sub(from_clean(), "## 8. 验证方式",
                     "## 7.1 验收方法\n\n这里补充说明判定口径。\n\n## 8. 验证方式"),
         set(), {"W8"}),
        # 用例四：带编号的那条是**空的**，条目其实挂在没编号的那条下面 —— 绑定会选没编号的
        # （「段内真有本段条目」排第一），文案就必须说「没编号的 X」。旧文案写死「带编号的」，
        # 在这种形态下与它对号入座的行号自相矛盾（审计复核 D-7 撞出来的）。
        ("同义标题：绑定落在没编号那条时文案要跟着说",
         lambda: sub(from_clean(), "## 4. 需求（R#）",
                     "## 4. 需求（R#）\n\n（本节先留空，条目写在下面那条标题下。）\n\n## 需求清单"),
         set(), {"W8"}, "本次绑定用了没编号的「需求清单」"),
        # ---- 注释 / 路径 / 判定 / 不可见字符（审计 F1–F8）----------------------
        # 整段被 HTML 注释包住 = 这段没写：注释里的标题不是标题，必须报缺段（审计 F1）。
        ("注释里的标题不算段落",
         lambda: sub(sub(from_clean(), "## 5. 非目标", "<!--\n## 5. 非目标"),
                     "- 不接数据库，不写定时任务。", "- 不接数据库，不写定时任务。\n-->"),
         {"E1"}),
        # 注释里的 `R#` 不是条目：多行注释里的整条需求不能变成「幽灵需求」逼出 E6（审计 F1）。
        ("注释里的需求不算条目",
         lambda: sub(from_clean(), "- **R1** 读取",
                     "<!--\n- **R9** 旧版草稿\n-->\n- **R1** 读取"),
         set()),
        # 注释里的数字也不算需求正文：`<!-- 参考 2024 版说明 -->` 不该把 W2 顶掉（审计 F1 的注释口径）。
        ("注释里的数字不算需求正文",
         lambda: sub(with_plain_r3(), plain_r3_new,
                     plain_r3_new + "\n  <!-- 参考 2024 版说明 -->"),
         set(), {"W2"}),
        # 交付物里只写 URL：URL 不是交付路径，E8 必须报（旧版命中 `.com` 假绿，审计 F3）。
        ("交付物只有 URL 报 E8",
         lambda: sub(from_clean(),
                     "| out/borrow.csv | UTF-8，只输出本周新增行 | 1 个 |",
                     "| 见 https://example.com/release | 网页说明 | 1 个 |"),
         {"E8"}),
        # 库名 `Next.js` / `Tailwind.css` 也不是交付路径（旧版命中 `.js` / `.css` 假绿，审计 F3）。
        ("交付物只有库名报 E8",
         lambda: sub(from_clean(),
                     "| out/borrow.csv | UTF-8，只输出本周新增行 | 1 个 |",
                     "| 用 Next.js 做页面，样式参考 Tailwind.css | 网页 | 1 个 |"),
         {"E8"}),
        # `判定：` 后面写否定式不算判定（旧版纯子串匹配：看到「人工」就当人工判定，审计 F5）。
        ("判定写成否定式报 E5",
         lambda: sub(from_clean(), "判定：人工 看 main 分支。", "判定：不需要人工，脚本自动跑。"),
         {"E5"}),
        # `判定方式另见下文，人工确认`：没有紧跟三态取值，也不算判定（同上）。
        ("判定另见他处报 E5",
         lambda: sub(from_clean(), "判定：人工 看 main 分支。", "判定方式另见下文，人工确认。"),
         {"E5"}),
        # U+2028 不是换行符：粘进正文的不可见分隔符不能凭空造出一个标题（审计 F8）。
        ("U+2028 不造幽灵标题",
         lambda: sub(from_clean(), "## 4. 需求（R#）\n",
                     "## 4. 需求（R#）\u2028## 需求变更由谁说了算\n"),
         set(), set()),
        # ---- 围栏 / 条目 / 编号 / 占位符（审计 F5–F15）------------------------
        # 围栏里的 `pf-ignore` 只是写法示例：不能关掉正文里真该报的 W2（审计 F5）。
        # 用 `~~~` 围栏：R3 条目正文里若有反引号，W2 本来就有「引了代码不算没量化」的豁免口径。
        ("围栏里的豁免注释不生效",
         lambda: sub(with_plain_r3(), plain_r3_new,
                     plain_r3_new + "\n\n~~~markdown\n<!-- pf-ignore: W2 围栏里的示例 -->\n~~~"),
         set(), {"W2"}, "", set()),
        # 行内代码里的豁免注释同理：它既不能免除 WARN，也不能反而报 E11（审计 F5）。
        ("行内代码里的豁免注释示例不报 E11",
         lambda: sub(from_clean(), "## 6. 技术约束",
                     "## 6. 技术约束\n\n写法示范：`<!-- pf-ignore: W99 理由 -->`\n\n这段说明要好看。"),
         set(), {"W9"}),
        # 围栏里的错码/错写法示例也不该报 E11：示例不是给这份文档写的豁免（审计 F5）。
        ("围栏里的坏码示例不报 E11",
         lambda: sub(from_clean(), "## 6. 技术约束",
                     "## 6. 技术约束\n\n~~~text\n<!-- pf-ignore: W99 随便 -->\n~~~\n\n这段说明要好看。"),
         set(), {"W9"}),
        # 四个反引号的围栏不能被内层三个反引号提前关闭：示例里的 R9 不能变幽灵需求（审计 F6）。
        ("四个反引号围栏不被内层三反引号关闭",
         lambda: sub(from_clean(), "## 5. 非目标",
                     "````markdown\n```text\n- **R9** 这里只是写法示例\n```\n````\n\n## 5. 非目标"),
         set()),
        # 「A4 纸」是纸张规格不是验收条目 A4：不能逼出假 E4/E5（审计 F7）。
        ("A4 纸不是验收编号",
         lambda: sub(from_clean(), "- **A1（对应 R1）**", "- A4 纸打印，双面。\n- **A1（对应 R1）**"),
         set()),
        # 围栏里的模糊词是示例文本，不算文档正文（与反引号豁免同口径，审计 F9 的反面）。
        ("围栏里的模糊词不报",
         lambda: sub(from_clean(), "## 6. 技术约束", "## 6. 技术约束\n```text\n界面要美观\n```"),
         set()),
        # 同名段落不能共享豁免作用域：第一个「备注」里的 pf-ignore 不能吃掉第二个的风险（审计 F10）。
        ("同名段落不串豁免",
         lambda: sub(from_clean(), "## 5. 非目标",
                     "## 备注\n\n<!-- pf-ignore: W9 这段先留着 -->\n\n## 备注\n\n这段说明要好看。\n\n## 5. 非目标"),
         set(), {"W9"}, "", set()),
        # `docs/TODO.md` 是文件名，不是残留占位符（审计 F11）。
        ("文件名 TODO.md 不算占位符",
         lambda: sub(from_clean(), "| out/borrow.csv | UTF-8，只输出本周新增行 | 1 个 |",
                     "| docs/TODO.md | 待办清单 | 1 个 |"),
         set()),
        # 小写 `todo` 同样是没改完的证据，要按占位符报出来（审计 F11 的漏检侧）。
        ("小写 todo 也要报 E10",
         lambda: sub(from_clean(), "## 6. 技术约束", "## 6. 技术约束\ntodo: 等用户补"),
         {"E10"}),
        # 多行 `pf-ignore` 是合法写法：理由换行写到下一行也要生效，不能静默失效（审计 F12）。
        ("多行豁免注释生效",
         lambda: sub(with_plain_r3(), plain_r3_new,
                     plain_r3_new + "\n<!-- pf-ignore: W2\n这版先不做量化 -->"),
         set(), set(), "", {"W2"}),
        # 缩进代码块里的条目是示例：不能变幽灵需求逼出 E6（审计 F13）。
        ("缩进代码块里的条目不算需求",
         lambda: sub(from_clean(), "## 4. 需求（R#）\n",
                     "## 4. 需求（R#）\n\n    示例：- **R9** 这里只是说明\n\n"),
         set()),
        # 5 级标题也必须是标题：不能因为级别深就判「缺少第 8 段」（审计 F15）。
        ("5 级标题也算标题",
         lambda: sub(from_clean(), "## 8. 验证方式", "##### 8. 验证方式"),
         set()),
        # setext 标题同样是标题（`8 验证方式` 下一行画线），不能被当成正文（审计 F15）。
        ("setext 标题也算标题",
         lambda: sub(from_clean(), "## 8. 验证方式", "8 验证方式\n--------"),
         set()),
        # 绑定段下面的子标题（`### 输出`）里的模糊词属于验收内容：必须按 E9 报，不能降级成 W9（审计 F16）。
        ("验收段子标题里的模糊词按 E9 报",
         lambda: sub(from_clean(),
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。",
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：人工 看 main 分支。\n\n"
                     "### 输出\n\n验收时界面要美观。"),
         {"E9"}, set()),
        # ---- 豁免注释 --------------------------------------------------------
        # 同段的 `pf-ignore` 吃掉这条 WARN，并如实报告「豁免了什么、理由是什么」。
        ("豁免注释：同段的 W2 被吃掉",
         lambda: sub(with_plain_r3(), plain_r3_new, plain_r3_new + "\n<!-- pf-ignore: W2 这版先不做量化 -->"),
         set(), set(), "", {"W2"}),
        # 写在别的段落里不算数：否则下一个人看到那条 WARN 时不知道有人处理过。
        # （注意注释必须落在**另一段标题的后面**：写在 `## 5.` 之前它仍属于第 4 段。）
        ("豁免注释：写在别的段落不算数",
         lambda: sub(with_plain_r3(), "## 5. 非目标",
                     "## 5. 非目标\n\n<!-- pf-ignore: W2 这版先不做量化 -->"),
         set(), {"W2"}, "", set()),
        # 想豁免 ERROR 本身就是错误：`0 ERROR 才允许交付` 不能被一条注释绕过去。
        ("豁免注释：想豁免 ERROR 报 E11",
         lambda: sub(SELFTEST_FIXTURE, vague_line, vague_line + "\n<!-- pf-ignore: E9 我觉得没事 -->"),
         {"E9", "E11"}),
        # 缺理由 / 码不认识 → E11，且被豁免的 WARN 照旧报出来。
        ("豁免注释：缺理由报 E11",
         lambda: sub(with_plain_r3(), plain_r3_new, plain_r3_new + "\n<!-- pf-ignore: W2 -->"),
         {"E11"}, {"W2"}, "", set()),
        ("豁免注释：码不认识报 E11",
         lambda: sub(with_plain_r3(), plain_r3_new, plain_r3_new + "\n<!-- pf-ignore: W99 随便 -->"),
         {"E11"}, {"W2"}, "", set()),
        # ERROR 码表必须含 E11 自己：否则写 `pf-ignore: E11` 会掉进「不认识这个码」，
        # 文案从「ERROR 不能豁免」退化成「没这个码」——校验清单里 14 个码的承诺就不成立了（审计 P19）。
        ("豁免注释：写 pf-ignore: E11 要报「是 ERROR」而不是「不认识」",
         lambda: sub(with_plain_r3(), plain_r3_new, plain_r3_new + "\n<!-- pf-ignore: E11 我也说不清 -->"),
         {"E11"}, {"W2"}, "豁免只对 WARN 生效", set()),
        # ---- R5-gate1 回归：判定写法与 E5 归属 -------------------------------
        # 判定值允许常见包裹：加粗 / 反引号 / 中文引号 / 括号。旧 JUDGE_RE 只认冒号后
        # 紧贴三态词，这四种写法全被误报 E5（审计 R5-gate1-03）。
        ("判定写法：加粗包裹",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：**人工** 看 main 分支。"),
         set()),
        ("判定写法：反引号包裹",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：`人工` 看 main 分支。"),
         set()),
        ("判定写法：中文引号包裹",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：「人工」 看 main 分支。"),
         set()),
        ("判定写法：括号包裹",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：（人工） 看 main 分支。"),
         set()),
        # 包裹里塞否定仍然不算写了判定：E5 要照报，不能因为放宽包裹就变绿。
        ("判定写法：包裹里的否定不算",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定：**不是人工** 看 main 分支。"),
         {"E5"}),
        # 判定归属这条 A# 自己：同块子条目里的「判定：命令」不能把 A3 的 E5 传染成绿
        # （审计 R5-gate1-04；旧版把整块拼起来交给 JUDGE_RE）。
        ("判定归属：子条目里的判定不算 A3 自己的",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定见下。\n"
                     "  - 参照 A1：判定：命令 `python scripts/borrow.py`。"),
         {"E5"}),
        ("判定归属：围栏示例里的判定不算 A3 自己的",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。判定见下。\n"
                     "```text\n判定：命令 python scripts/borrow.py\n```"),
         {"E5"}),
        # 正常排版：判定写在 A# 的续行上仍然算这条验收的判定。
        ("判定归属：换行续写判定",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。\n  判定：人工 看 main 分支。"),
         set()),
        # ---- R5-gate1 回归：别段里的 R#/A# 条目只是引用 -----------------------
        # 第 8 段按需求逐条列命令、表格首列写编号、第 9 段步骤里写 R#/A#，都是正常排版：
        # 旧版全文收集条目，把它们当成「新需求/新验收」，合规文档被 E2b/E3b/E6 误杀
        # （审计 R5-gate1-02；ghost.prompt.md 在 CLI 上是 3 ERROR）。
        ("别段条目只是引用：验证段按需求列命令",
         lambda: sub(from_clean(), "期望输出：out/borrow.csv 生成，退出码 0。",
                     "期望输出：out/borrow.csv 生成，退出码 0。\n\n按需求逐条验证：\n"
                     "- R1：`python scripts/borrow.py --check`\n"
                     "- R2：`python scripts/borrow.py --skip-blank`\n"
                     "- R3：`python scripts/borrow.py --exit`"),
         set()),
        ("别段条目只是引用：验证段表格首列写编号",
         lambda: sub(from_clean(), "期望输出：out/borrow.csv 生成，退出码 0。",
                     "期望输出：out/borrow.csv 生成，退出码 0。\n\n"
                     "| 需求 | 命令 |\n| --- | --- |\n"
                     "| R1 | `python scripts/borrow.py --check` |\n"
                     "| R2 | `python scripts/borrow.py --skip-blank` |\n"
                     "| R3 | `python scripts/borrow.py --exit` |"),
         set()),
        ("别段条目只是引用：步骤段写 A1",
         lambda: sub(from_clean(), "3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）",
                     "3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）\n"
                     "- A1：先确认 out/borrow.csv 已生成。"),
         set()),
        ("别段条目只是引用：步骤段写 R9",
         lambda: sub(from_clean(), "3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）",
                     "3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）\n"
                     "4. R9：把汇总清单也发一份。"),
         set()),
        # 反过来：同一段里真重复的 R# 仍然必须报 E2b（修复不能把重复检测一起放过）。
        ("同一段里真重复 R# 仍报 E2b",
         lambda: sub(from_clean(), "- **R2** 输出控制在两行以内，空行最多跳过 3 行。",
                     "- **R2** 输出控制在两行以内，空行最多跳过 3 行。\n"
                     "- **R2** 也支持 data/old.xlsx 的回填。"),
         {"E2b"}),
        # ---- R6-gate1 回归：不可见字符 / 全角字母造成的假红 ---------------------
        # 从网页、Word、聊天记录里粘进标题的零宽字符肉眼看不见，却让别名、编号、判定词
        # 全部匹配失败：用户看着那行明明是对的，闸门却报 E1/E2/E3/E4/E5（审计 R6-gate1-03）。
        ("零宽空格不破坏段落标题识别",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "## 4. 需\u200b求（R#）"),
         set()),
        ("行首零宽字符 / BOM 不破坏段落标题识别",
         lambda: sub(from_clean(), "## 12. 汇报格式", "\u200b\ufeff## 12. 汇报格式"),
         set()),
        ("零宽空格不破坏 R# 编号识别",
         lambda: sub(from_clean(), "- **R3**", "- **R\u200b3**"),
         set()),
        ("零宽空格不破坏验收引用",
         lambda: sub(from_clean(), "A3（对应 R3）", "A3（对应 R\u200b3）"),
         set()),
        ("全角字母的 R# 编号不误报 E2",
         lambda: sub(from_clean(), "- **R3**", "- **Ｒ3**"),
         set()),
        ("全角字母的 A# 编号不误报 E3",
         lambda: sub(from_clean(), "A3（对应 R3）", "Ａ3（对应 R3）"),
         set()),
        ("NBSP 不破坏段落标题识别",
         lambda: sub(from_clean(), "## 4. 需求（R#）", "## 4. 需\u00a0求（R#）"),
         set()),
        ("零宽字符不破坏判定取值",
         lambda: sub(from_clean(), "判定：人工 看 main 分支。", "判定：人\u200b工 看 main 分支。"),
         set()),
        # ---- R6-gate1 回归：判定缩进写在子条目里也算这条 A# 的判定 ------------
        # 常见排版：`- **A3** …` 下一行缩进写 `- 判定：人工`。旧版把子条目一律当「别人的
        # 判定」排除，于是照报 E5（审计 R6-gate1-04 的假红侧）。
        ("判定写法：缩进子条目里单独一行写判定",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。\n  - 判定：人工 看 main 分支。"),
         set()),
        ("判定写法：缩进子条目里加粗写判定",
         lambda: sub(from_clean(), a3_line,
                     "- **A3（对应 R3）** 入口与退出码符合要求。\n  - **判定：** 人工 看 main 分支。"),
         set()),
        # ---- R6-gate1 回归：缩进代码块里的 pf-ignore 是示例，不是真豁免 ---------
        # 与围栏里的示例同口径（校验清单「豁免注释」）：示例不能关掉正文里真该报的 W2。
        # 旧版只跳过围栏与行内代码，4 空格缩进代码块里的写法照样被当成真豁免（审计 R6-gate1-05）。
        ("缩进代码块里的豁免注释不生效",
         lambda: sub(with_plain_r3(), plain_r3_new,
                     plain_r3_new + "\n\n    <!-- pf-ignore: W2 这版先不量化 -->"),
         set(), {"W2"}, "", set()),
        # ---- 第 8 轮审计：W6 按「条数」数，不再按「行数」数 ------------------
        ("非目标只写 1 条：带一行续行也报 W6（第 8 轮 R8-03）",
         lambda: sub(from_clean(), s5_all, s5_one_cont),
         set(), {"W6"}, "非目标只写了 1 条"),
        ("非目标只有单列表格 1 条也报 W6（第 8 轮 R8-03）",
         lambda: sub(from_clean(), s5_all, s5_one_table),
         set(), {"W6"}, "非目标只写了 1 条"),
        # W6 带行号后，豁免注释回到「只对同段生效」：写在 §5 里生效，写在别的段不生效。
        ("豁免注释：同段的 W6 被吃掉（第 8 轮 R8-03）",
         lambda: sub(from_clean(), s5_all,
                     s5_one_cont + "\n<!-- pf-ignore: W6 这版先不补第二条 -->"),
         set(), set(), "", {"W6"}),
        ("豁免注释：写在 §4 的 W6 不算数（跨段不生效，第 8 轮 R8-03）",
         lambda: sub(sub(from_clean(), s5_all, s5_one_cont),
                     "## 4. 需求（R#）",
                     "## 4. 需求（R#）\n\n<!-- pf-ignore: W6 这版先不补第二条 -->"),
         set(), {"W6"}, "", set()),
    ]
    for case in inline:
        name, build, want = case[0], case[1], case[2]
        # 第四个元素是「期望的 WARN 码集合」；不写就只比对 ERROR（历史用例都是这个口径）。
        want_warns = case[3] if len(case) > 3 else None
        # 第五个元素是「报错文案里必须出现的一句话」：码集合对了但话说反了，同样算不通过。
        want_msg = case[4] if len(case) > 4 else ""
        # 第六个元素是「期望被 `pf-ignore` 豁免掉的 WARN 码集合」（不写就不看豁免）。
        want_ignored = case[5] if len(case) > 5 else None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.prompt.md"
            try:
                text = build()
                # 用例必须真的改到了夹具（`sub()` 已经逐个断言过替换目标，这里再兜一层）。
                if text == SELFTEST_FIXTURE:
                    raise AssertionError("没有改动夹具，用例是空跑的")
            except AssertionError as exc:
                # 夹具漂移 = 用例失效，必须让 `--selftest` 非零退出，否则「全绿」是假的。
                print(f"FAIL  [闸门] {name}: 用例构造失败")
                print(f"      ERROR: 自测用例 [{name}] 不可用：{exc}")
                failed += 1
                continue
            path.write_text(text, encoding="utf-8")
            suppressed: list[tuple[Finding, str]] = []
            findings, _ = check_file(path, suppressed=suppressed)
        got = {f.code for f in findings if f.level == "ERROR"}
        got_warns = {f.code for f in findings if f.level == "WARN"}
        got_ignored = {f.code for f, _why in suppressed}
        ok = got == want and (want_warns is None or got_warns == want_warns)
        if ok and want_msg:
            ok = any(want_msg in f.message for f in findings)
        if ok and want_ignored is not None:
            ok = got_ignored == want_ignored
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] {name}: ERROR 码 {sorted(got)}（期望 {sorted(want)}）"
              + ("" if want_warns is None else f" WARN 码 {sorted(got_warns)}（期望 {sorted(want_warns)}）")
              + ("" if not want_msg else f" 文案须含「{want_msg}」")
              + ("" if want_ignored is None else f" 豁免 {sorted(got_ignored)}（期望 {sorted(want_ignored)}）"))
        if not ok:
            failed += 1
            for f in findings:
                if f.level == "ERROR" or (want_warns is not None and f.level == "WARN"):
                    print(f"      {f.render()}")
            for f, why in suppressed:
                print(f"      豁免: {f.render()} —— 理由：{why}")

    # `--no-ignore` 必须让豁免失效：看不见的 WARN 没法在评审时复查。
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "no-ignore.prompt.md"
        path.write_text(
            sub(with_plain_r3(), plain_r3_new, plain_r3_new + "\n<!-- pf-ignore: W2 这版先不做量化 -->"),
            encoding="utf-8",
        )
        findings, _ = check_file(path, use_ignore=False)
        got_warns = {f.code for f in findings if f.level == "WARN"}
        ok = got_warns == {"W2"}
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] --no-ignore 时豁免注释失效: "
              f"WARN 码 {sorted(got_warns)}（期望 ['W2']）")
        if not ok:
            failed += 1
            for f in findings:
                print(f"      {f.render()}")

    # 读不了文件（拿目录当输入）：必须打印一行 ERROR 并退出码 1，不能抛 traceback（审计 F2）。
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = report(Path(tmp))
        out = buf.getvalue()
        ok = rc == 1 and "ERROR: 读不了文件" in out and "Traceback" not in out
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 读不了文件时打印 ERROR 而不抛 traceback：退出码 {rc}")
        if not ok:
            failed += 1
            print("      " + " / ".join(out.strip().splitlines()[-3:]))

    # U+2028 不是换行符：报错行号必须和编辑器一致（审计 F8）。
    noisy = sub(SELFTEST_FIXTURE, "## 1. 角色与目标\n", "## 1. 角色与目标\u2028\n")
    want_line = noisy.split("\n").index("- **R2** 输出界面要美观，空行最多跳过 3 行。") + 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "u2028.prompt.md"
        path.write_text(noisy, encoding="utf-8")
        noisy_findings, _ = check_file(path)
    got_lines = sorted({f.line for f in noisy_findings if f.code == "E9" and "美观" in f.message})
    ok = got_lines == [want_line]
    print(f"{'PASS' if ok else 'FAIL'}  [闸门] U+2028 时 E9 行号不漂：{got_lines}（期望 [{want_line}]）")
    if not ok:
        failed += 1
        for f in noisy_findings:
            if f.code == "E9":
                print("      " + f.render())

    # UTF-16（记事本另存）按 BOM 解码，不能落一个莫名其妙的 E0（审计 F17）。
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "utf16.prompt.md"
        path.write_bytes(from_clean().encode("utf-16"))
        utf16_findings, _ = check_file(path)
    utf16_errors = [f for f in utf16_findings if f.level == "ERROR"]
    ok = not utf16_errors
    print(f"{'PASS' if ok else 'FAIL'}  [闸门] UTF-16 文件按 BOM 解码：{len(utf16_errors)} ERROR（期望 0）")
    if not ok:
        failed += 1
        for f in utf16_errors[:5]:
            print("      " + f.render())

    # 围栏内容不能把后面的行号整体前移：E9 报的行号必须和编辑器里看到的一致（审计 F9）。
    shifted = sub(
        SELFTEST_FIXTURE,
        "- **R3**",
        "```text\n示例\n\n示例二\n```\n\n- **R4** 界面要美观。\n\n- **R3**",
    )
    shifted_line = shifted.split("\n").index("- **R4** 界面要美观。") + 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fence-shift.prompt.md"
        path.write_text(shifted, encoding="utf-8")
        shifted_findings, _ = check_file(path)
    shifted_lines = sorted({f.line for f in shifted_findings if f.code == "E9" and "美观" in f.message})
    ok = shifted_line in shifted_lines
    print(f"{'PASS' if ok else 'FAIL'}  [闸门] 代码围栏不引起 E9 行号漂移：{shifted_lines}（须含 [{shifted_line}]）")
    if not ok:
        failed += 1
        for f in shifted_findings:
            if f.code == "E9":
                print("      " + f.render())

    # E4/E5/E6 必须带行号：没有行号的报错等于让人重新全文找一遍（审计 F14）。
    unlined = sub(
        from_clean(),
        "- **A2（对应 R2）** 遇到空行不中断。判定：阈值 跳过行数 ≤ 3。",
        "- **A2** 遇到空行不中断。",
    )
    a2_line = unlined.split("\n").index("- **A2** 遇到空行不中断。") + 1
    r2_line = unlined.split("\n").index("- **R2** 输出控制在两行以内，空行最多跳过 3 行。") + 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "line-numbers.prompt.md"
        path.write_text(unlined, encoding="utf-8")
        unlined_findings, _ = check_file(path)
    def _lines(code: str) -> list[int]:
        return sorted({f.line for f in unlined_findings if f.code == code})
    ok = _lines("E4") == [a2_line] and _lines("E5") == [a2_line] and _lines("E6") == [r2_line]
    print(
        f"{'PASS' if ok else 'FAIL'}  [闸门] E4/E5/E6 带行号：E4 {_lines('E4')}、E5 {_lines('E5')}、"
        f"E6 {_lines('E6')}（期望 E4/E5 [{a2_line}]、E6 [{r2_line}]）"
    )
    if not ok:
        failed += 1
        for f in unlined_findings:
            if f.code in ("E4", "E5", "E6"):
                print("      " + f.render())

    # ---- R5-gate1 回归：病态输入的性能与编码边界 --------------------------
    # E10 曾对每一行跑 `URL_RE.sub`：无空白的字母数字行会触发 O(n²) 回溯，1MB 单行 >10 分钟
    # （审计 R5-gate1-01：160KB 62.6s，faulthandler 卡在 `check_prompt.py` 的 URL_RE.sub）。
    # 交付物段的 PATH_RE 同根因（只对交付段落生效，同样能卡死 E8）。这里用定长串做回归，
    # 阈值故意放宽到 5s / 10s：测的是「线性不退化」，不是机器跑分。
    def _run_timed(text: str) -> tuple[set[str], float]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "perf.prompt.md"
            path.write_text(text, encoding="utf-8")
            started = time.perf_counter()
            findings, _ = check_file(path)
            elapsed = time.perf_counter() - started
        return {f.code for f in findings if f.level == "ERROR"}, elapsed

    perf_cases = [
        ("无空白超长单行（131072 字符）",
         sub(from_clean(), "每周五花 15 分钟手工抄一遍。[AI 推断]",
             "每周五花 15 分钟手工抄一遍。[AI 推断]\n" + "a" * 131072),
         set(), 5.0),
        ("超长单行里带真 URL",
         sub(from_clean(), "每周五花 15 分钟手工抄一遍。[AI 推断]",
             "每周五花 15 分钟手工抄一遍。[AI 推断]\n" + "b" * 65536 + " 见 https://example.com/x"),
         set(), 3.0),
        # 交付物段被一行 524288 字母替换（唯一的 out/borrow.csv 没了）：E8 必须照报且不卡。
        # 长度取到 512KB 是为了让「删掉 has_path_token 的改点」真的超时：PATH_RE 的 255 段量词
        # 在 512KB 上约 7s（修复前 1MB 更久），修复后走「没有点」快速通道 < 1s。
        ("交付物段超长单行仍要报 E8",
         sub(from_clean(), "| out/borrow.csv | UTF-8，只输出本周新增行 | 1 个 |",
             "交付到 " + "c" * 524288),
         {"E8"}, 3.0),
        ("约 560KB 多行正常文档不退化",
         from_clean() + "".join(
             f"\n第 {i} 行附注：把借阅登记的输出控制在两行以内。" for i in range(1, 20001)),
         set(), 10.0),
        # R6-gate1-01：E10 的 `FILE_TOKEN_RE.sub` 在「长串标点」行上逐起点回溯，是 O(n²)：
        # 32768 个点号 11.6s、1MB 单行卡死。修复改成按「词字符 run」线性扫描后 <0.1s。
        ("标点长行（65536 个点号）",
         sub(from_clean(), "每周五花 15 分钟手工抄一遍。[AI 推断]",
             "每周五花 15 分钟手工抄一遍。[AI 推断]\n" + "." * 65536),
         set(), 3.0),
        # R6-gate1-02：`strip_comments` 的 `<!--.*?-->` 在大量未闭合 `<!--` 上同样是 O(n²)：
        # 65536 字符要 7.9s、1MB 卡死。修复改成 `str.find` 线性推进后 <0.1s。
        ("大量未闭合 HTML 注释（65536 字符）",
         sub(from_clean(), "每周五花 15 分钟手工抄一遍。[AI 推断]",
             "每周五花 15 分钟手工抄一遍。[AI 推断]\n" + "<!--" * 16384),
         set(), 3.0),
    ]
    for perf_name, perf_text, want_perf, limit in perf_cases:
        got_perf, elapsed = _run_timed(perf_text)
        ok = got_perf == want_perf and elapsed < limit
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 病态输入：{perf_name}："
              f"ERROR 码 {sorted(got_perf)}、{elapsed:.2f}s（期望 {sorted(want_perf)} 且 < {limit:g}s）")
        if not ok:
            failed += 1

    # 输入编码边界（审计 R5-gate1-05 补测；修复前后行为都已实跑确认）：
    # GBK / CRLF / BOM / 正文 NUL 必须照常检查，非法 UTF-8 字节必须确定性地落 E0。
    encoding_cases = [
        ("CRLF", from_clean().replace("\n", "\r\n").encode("utf-8"), set()),
        ("UTF-8 BOM", b"\xef\xbb\xbf" + from_clean().encode("utf-8"), set()),
        ("正文含 NUL", from_clean().replace("[AI 推断]", "[AI 推断]\x00", 1).encode("utf-8"), set()),
        ("GBK 编码", from_clean().encode("gbk"), set()),
        ("非法 UTF-8 字节", b"\x80\x81\x82\x83\x84\x85\x86", {"E0"}),
    ]
    for enc_name, data, want_enc in encoding_cases:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "encoding.prompt.md"
            path.write_bytes(data)
            findings, _ = check_file(path)
        got_enc = {f.code for f in findings if f.level == "ERROR"}
        ok = got_enc == want_enc
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 输入编码边界：{enc_name}："
              f"ERROR 码 {sorted(got_enc)}（期望 {sorted(want_enc)}）")
        if not ok:
            failed += 1

    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查长提示词是否符合 prompt-forge 的 12 段契约")
    parser.add_argument("files", nargs="*", help="要检查的 prompt.md 路径")
    parser.add_argument("--selftest", action="store_true", help="用 examples/ 下的正反例自测")
    parser.add_argument("--no-ignore", action="store_true",
                        help="忽略 `<!-- pf-ignore: W码 理由 -->` 豁免注释，把所有 WARN 都报出来")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.files:
        parser.print_help()
        return 2

    code = 0
    for name in args.files:
        path = Path(name)
        if path.is_dir():
            # 目录是存在的，只是不是文件：说「文件不存在」会把人带去查路径拼写。
            print(f"ERROR: 这是目录，不是文件：{path}")
            code = 1
            continue
        if not path.is_file():
            print(f"ERROR: 文件不存在：{path}")
            code = 1
            continue
        code |= report(path, use_ignore=not args.no_ignore)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
