#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的第二道机器闸门：检查这个技能**自己的定义**能不能被宿主自动识别。

用法：
    python scripts/check_skill.py <SKILL.md 路径> [<SKILL2.md> ...]
    python scripts/check_skill.py --selftest    # 自测：仓库里的 SKILL.md 必须 0 ERROR，反例必须命中预期 ERROR 码

为什么需要它：技能不是靠"固定咒语"触发的。宿主把 name + description 交给模型，
模型按语义决定要不要用这个技能（机制与出处见 references/方法论出处.md 第 4.5 节）。
后果是写错 frontmatter 会很安静：有的宿主只打一条 warning，模型侧完全看不到诊断。
所以把规则钉成闸门：名字对不对、描述能不能被认出"何时该用/何时不该用"、正文有没有把"该不该接"
与"菜单默认档"讲清楚（**后两项只在正文声称了自己会做这件事时才查** —— 别人的自足技能不该被
prompt-forge 的家规拦下）。另外，正文引用了**同目录**文件（`AGENTS.md` / `references/` / 闸门脚本）
却只拷了一个 `SKILL.md`，也在这里拦。

三条实现口径（都被真实审计撞过，改代码时别丢）：
1. frontmatter 按 YAML 的常见写法解析：折行/缩进续行要折叠、行内 `#` 注释要剥掉、
   **闭合标记 `---` 必须真的存在**（未闭合时宿主会整个忽略这个技能，闸门不能报"通过"）。
2. 行号按编辑器口径切行（只认 `\n`）：用 `str.splitlines()` 会把 U+2028 之类当换行，
   行号与编辑器对不上，还会把一个合法的 YAML 值劈成两半。
3. 只有**同目录**引用才算依赖：`../AGENTS.md`、绝对路径都不该被 E7 拦（那不是"同目录"）。

退出码：有 ERROR 时非零退出。发现问题就打印 `ERROR: ...` 行，不静默通过。
检查项清单见 references/校验清单.md。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
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

# ---------------------------------------------------------------- 规则表（权威）

#: name 必须是 kebab-case：小写字母/数字，用单个连字符连接，不能首尾或连续出现连字符。
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: agentskills 规范：name 最长 64 字符。
NAME_MAX = 64
#: agentskills 规范：description 最长 1024 字符。
DESC_MAX = 1024
#: 有的宿主只把 description 的前 500 字渲染进目录（DSH 的 catalogDescriptionMaxLength 默认就是 500），
#: 所以"何时该用 / 何时不该用"必须落在前 500 字里。
CATALOG_RENDER_MAX = 500

#: 允许出现在 name 里的非 ASCII 会被 NAME_RE 拦掉，这里只放人话解释。
NAME_RULE = "kebab-case（小写字母/数字，用单个连字符连接，不能首尾或连续出现连字符），最长 64 字符"

#: 「何时该用」的标记：官方要求把 description 写成给模型的指令（"Use this skill when…"），
#: 而不是"这个技能做 X"的自我介绍。
#: 注意：这里**不能放裸字「当」**。`当前/当时/相当/应当/当然` 在中文技术文里到处都是，
#: 一个裸「当」会让 E5 空转（实测：description 里只有「当前只支持 Windows」也能过）。
#: `当…时` 那种句式交给下面的 WHEN_PATTERNS 用正则认。
#: `re:` 前缀的标记走正则（在折叠空白后的文本上匹配）——「不适用」里也有「适用」，
#: 不做否定环视就会把边界句当成触发句。
WHEN_MARKERS = [
    "如果", "若用户", "用户说", "用户给出", "用户抛", "你需要",
    "使用本技能", "使用此技能", "调用本技能", "请用", "本技能用于",
    "Use this skill", "use this skill", "when the user", "When the user",
    r"re:(?<!不)(?:适用|触发)",
]

#: 「当…时」句式：当 + 至多 40 个非句末标点字符 + 时/时候。
#: 只认 10 个字会漏掉正常长度的触发句（审计 P28）；`当(?!前|相)` 挡住「当前/相当」。
#: 逗号/分号/句号/叹号/问号仍算句界，不允许跨过去 —— 否则整段话都被当成一个「当…时」。
WHEN_PATTERNS = [
    re.compile(r"当(?!前|相)[^，。；！？\n]{0,40}?(?:时|时候)"),
]

#: 「何时不该用」的标记：官方口径是"描述过宽 = 在不该触发的时候触发"，
#: 要求显式写明边界（what the skill does *not* do）。
#: 注意：这里只收**排除式**说法。`只在…时触发` / `仅当…` 是"何时该用"的限定，不是边界 ——
#: 旧版把 `只在` 当边界，实测「只在需求模糊时触发。」就能让 E6 假绿。
BOUNDARY_MARKERS = [
    "不接", "不适用", "不处理", "不要用", "不用于", "不用来", "不负责", "别用", "不该用",
    "禁止用于", "不属于本技能", "不覆盖",
    "Do NOT use", "do not use", "don't use", "Not for", "not for", "NOT for",
]

#: 祈使句标记（官方指南：frame the description as an instruction to the agent）。
IMPERATIVE_MARKERS = [
    "使用本技能", "使用此技能", "调用本技能", "用本技能", "请用", "Use this skill", "use this skill",
]

#: 第三人称自我介绍（官方指南明确不推荐）。
THIRD_PERSON_MARKERS = [
    "本技能做", "本技能是", "这个技能做", "这个技能是", "该技能做", "该技能是", "此技能做", "此技能是",
    "This skill does", "This skill is", "This skill helps",
]

#: 三态分诊：接 / 先问一句 / 不接（对齐 spec-kit assessment 的 go/needs-clarification/kill）。
#: 注意：**不是所有技能都要写分诊**。只有正文声称了自己会做"该不该接"的判断时，才要求它写全三态 ——
#: 否则就是拿 prompt-forge 的家规去要求别人的技能（这条曾经误报过一大片）。
TRIAGE_CLAIM_MARKERS = ["分诊", "该不该接", "接不接"]
#: 「不接（kill）」这一态。`kill` 必须带三态语境，裸词不算：很多技能正文会写「kill 进程」
#: （审计 P34：把裸 ASCII 标记改成有语境的形状）。
TRIAGE_STATE_MARKERS = [
    "不接",
    r"re:`kill`",
    r"re:不接[（(]?kill",
    r"re:(?:/|、)kill",
    r"re:kill[）)]",
    r"re:kill态",
    r"re:(?<![A-Za-z0-9])KILL(?![A-Za-z0-9])",
]
#: 「接（go）」这一态。`(?<!不)接[（(]`：`不接（kill）` 里的那半截「接（」**不算**写过接住态。
#: 裸 `go` 不算：正文里的「用 Go 写」「Go 版本 1.22」会把三态检查骗绿（审计 P34）；
#: 只有反引号包住、括号里、`接 go`、`go /` 或在全大写时才认。
TRIAGE_ACCEPT_MARKERS = [
    r"re:(?<!不)接[（(]",
    r"re:`go`",
    r"re:接go",
    r"re:(?:[（(/、])go(?=[）)/、,，。]|$)",
    r"re:go[（(]",
    r"re:go[/）)]",
    r"re:go态",
    r"re:(?<![A-Za-z0-9])GO(?![A-Za-z0-9])",
    "needs-clarification",
]

#: 「先问一句（needs-clarification）」这一态。W3 的报错文案要求三态齐，旧版只查了接/不接两态，
#: 正文声称分诊却漏掉中间态的假绿就漏过去了（第 6 轮审计 R6-PF-G2-03）。
TRIAGE_MID_MARKERS = [
    "先问一句", "先问清楚", "需要澄清", "需澄清", "问清楚", "clarify", "clarification",
]

#: 薄包装正文第一步必须指向同目录的规范本体，并写清"先把它读完"。
#: SPEC_FILE_MARKERS（哪些文件算规范本体）在下面的 SIBLING_PATTERNS 旁边定义，两处共用一份。
SPEC_READ_MARKERS = ["读一遍", "从头到尾", "先读", "先把它", "读文件工具"]

#: 菜单必须自带默认档，且用户能不选（官方口径：Provide defaults, not menus）。
#: 同样只在正文真的提供了"菜单/选项"这种机制时才检查。
MENU_CLAIM_MARKERS = ["菜单", "选项"]
DEFAULT_MARKERS = ["默认"]
OPT_OUT_MARKERS = ["都可以", "你决定", "全按默认", "按默认走"]

#: 同目录依赖的识别标记：正文里出现这些字符串，就等于宣称"我需要同目录的某个文件"。
#: 只有**真的引用了**才检查它在不在 —— 自足技能（把规范写在自己正文里）不该被这条拦。
#:
#: 前面那个否定环视就是"同目录"三个字的实现：`../AGENTS.md`、`F:/x/AGENTS.md`、
#: `/usr/lib/AGENTS.md` 前面的字符是 `/`、`\`、`:`，一律不算同目录引用（文件明明存在却被 E7
#: 判红，是真实审计里撞到过的假红）。`scripts/check_prompt.py` 这种**技能目录内的相对路径**
#: 仍要检查，所以第一分支允许带 `scripts/` 前缀。
SIBLING_PATTERNS: dict[str, re.Pattern[str]] = {
    "AGENTS.md": re.compile(r"(?<![\w./\\:-])AGENTS\.md"),
    "references": re.compile(r"(?<![\w./\\:-])(?:references[/\\]|`references`)"),
    "scripts/check_prompt.py": re.compile(r"(?<![\w./\\:-])(?:scripts[/\\])?check_prompt\.py"),
}

#: 薄包装形态的判据：正文引用了同目录的规范本体（SPEC_FILE_MARKERS 里的名字），
#: 才要求它写"动手前先读完规范"。自足技能不引用，就不查 W4。
SPEC_FILE_MARKERS = ("AGENTS.md",)

FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")


class Finding:
    """一条检查结果。level 只有 ERROR / WARN 两种。"""

    def __init__(self, level: str, code: str, message: str, line: int = 0) -> None:
        self.level = level
        self.code = code
        self.message = message
        self.line = line

    def render(self) -> str:
        where = f"（第 {self.line} 行）" if self.line else ""
        return f"{self.level}: [{self.code}] {self.message}{where}"


def read_text(path: Path) -> str:
    """读文件：UTF-8（含 BOM）优先，退回系统默认编码（Windows 上的中文文件可能是 GBK）。

    用 utf-8-sig 而不是 utf-8：带 BOM 的 SKILL.md 会被前者解码成干净文本，
    否则 BOM 留在开头，`---` 认不出来，报一个假的 E1（审计 P27）。
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def write_text_lf(path: Path, text: str) -> None:
    """按 LF 写文件（本仓库约定全部文本都是 LF）。

    不用 `Path.write_text(..., newline="\\n")`：那个 `newline` 参数是 **Python 3.10+** 才有的，
    3.9 上会 `TypeError: write_text() got an unexpected keyword argument 'newline'`
    —— 本仓库承诺 ≥3.9，CI 的 3.9 那一格真的抓到过（踩坑记录 #42），所以这里显式 `open`。
    """
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def split_lines(text: str) -> list[str]:
    """按编辑器口径切行：只认 `\\n`（顺带去掉 `\\r`）。

    不用 `str.splitlines()`：它还会在 U+2028 / U+2029 / \\v / \\f 处断行，
    于是行号与编辑器对不上，还会把一个合法的 YAML 值（description 里就可能有）劈成两半。
    """
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def fold_ws(text: str) -> str:
    """去掉所有空白，**只用于找标记词**。

    description 为了排版会折行，也可能被写成 `不 接`；找标记时不该因此漏判。
    长度与取值仍然用原文，只有找标记这一步折叠。
    """
    return re.sub(r"\s+", "", text)


#: 纯 ASCII 的短标记（`go`、`kill`、`needs-clarification`）必须按词边界匹配：
#: 折叠空白后的子串匹配会让 `good` 命中 `go`、`skilled` 命中 `kill` ——
#: 实测里写过「这个技能 good 用」的正文就这样把 W3 骗成了通过。
ASCII_MARKER_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")


def marker_hit(text: str, marker: str) -> bool:
    """单个标记是否命中。

    - `re:` 前缀 → 正则（在折叠空白后的文本上匹配，用来表达「不适用」这类否定边界）
    - 纯 ASCII 短词 → 词边界匹配（`go` 不该被 `good` 命中）
    - 其余（中文短语 / 带空格的英文短句）→ 折叠空白后的子串匹配（`不 接` 也算命中）

    子串匹配大小写不敏感：YAML 里写 `Don't use`（句首大写）与词表里的 `don't use` 是同一个边界，
    旧版区分大小写，句首大写的合法写法被漏认成 E6（第 5 轮审计 R5-PF-G3-10）。
    """
    if marker.startswith("re:"):
        return re.search(marker[3:], fold_ws(text)) is not None
    if ASCII_MARKER_RE.fullmatch(marker):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(marker) + r"(?![A-Za-z0-9])"
        return re.search(pattern, text, re.IGNORECASE if marker.islower() else 0) is not None
    return fold_ws(marker).casefold() in fold_ws(text).casefold()


def has_marker(text: str, markers: list[str]) -> bool:
    """在文本里找标记词。

    中文标记先折叠空白（`不 接` 也算命中），ASCII 短词按词边界在原文里找
    （折叠会把英文单词粘在一起，边界就没法判了）。
    """
    return any(marker_hit(text, m) for m in markers)


def marker_line(lines: list[str], markers: list[str], start: int, default: int) -> int:
    """第一个含指定标记词的行号（1-based）；找不到就返回 default。"""
    for idx in range(start, len(lines)):
        if has_marker(lines[idx], markers):
            return idx + 1
    return default


def pattern_line(lines: list[str], pattern: re.Pattern[str], start: int, default: int) -> int:
    """第一个命中正则的行号（1-based）；找不到就返回 default。"""
    for idx in range(start, len(lines)):
        if pattern.search(lines[idx]):
            return idx + 1
    return default


def unquote(value: str) -> str:
    """去掉 YAML 值两端成对的引号，并还原单引号标量里的 `''` 转义（YAML：`''` → `'`）。

    旧版只剥引号、不还原转义：合法写法 `'When the user … Don''t use …'` 读出来带着两个单引号，
    边界词认不出，于是报 E6 + W2 —— 宿主按 YAML 解析后明明是合规的描述（第 5 轮审计 R5-PF-G3-06）。
    双引号标量不做转义还原：description 里极少出现 `\"` / `\n` 这类转义，保持原样风险更低。
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].strip()
    return value


def strip_inline_comment(value: str) -> str:
    """剥掉 YAML 行内注释：`#` 在行首或前面是空白、且不在引号里，才算注释。

    不剥的话 `name: cmt # 说明` 会读成 `cmt # 说明`（假的 E2 + E3）；
    反过来，把唯一一句边界写在注释里也会假绿 —— 宿主读到的值里没有那句注释。
    """
    quote = ""
    for idx, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (idx == 0 or value[idx - 1] in " \t"):
            return value[:idx]
    return value


def _fold_plain_parts(parts: list[str]) -> str:
    """按 YAML 纯标量的折叠规则拼多行值：单个换行折成空格，空行折成换行（段落分隔）。

    实测 PyYAML 口径：`["a", "b"]` → `a b`；`["a", "", "b"]` → `a\\nb`。旧版遇到空行就整个
    结束标量，边界写在第二段的合法写法被漏读成 E6（第 5 轮审计 R5-PF-G3-05）。
    """
    out = ""
    blank = 0
    for part in parts:
        if not part:
            blank += 1
            continue
        if out:
            out += "\n" * blank if blank else " "
        out += part
        blank = 0
    return out


def parse_frontmatter(text: str) -> tuple[dict[str, str], int, str, dict[str, int]]:
    """解析最简 YAML frontmatter。

    返回 `(字段表, 闭合行号, 状态, 每个字段所在行号)`；状态是 `"ok"`（有 frontmatter 且闭合）/
    `"missing"`（开头没有 `---`）/ `"unclosed"`（找不到第二个 `---`，宿主会整个忽略这个技能）/
    `"badtab"`（用 tab 做缩进，真 YAML 解析器会报错，宿主同样会整个忽略这个技能）。

    只认 `key: value`，另外照顾三种真实写法：
    - `|` / `>` 块标量（description 常这么写）；
    - 值写在下方的**缩进续行**里，或折行续写（YAML 纯标量折行，按空格折叠；空行是段落分隔）；
    - 行内注释、锚点 / 别名（`&d` / `*d`）。
    """
    lines = split_lines(text)
    if not lines or lines[0].strip() != "---":
        return {}, 0, "missing", {}
    fields: dict[str, str] = {}
    key_lines: dict[str, int] = {}
    anchors: dict[str, str] = {}
    i = 1
    while i < len(lines):
        raw = lines[i]
        # YAML 不允许用 tab 做缩进：宿主（PyYAML）会直接报 ScannerError 并整个忽略技能，
        # 旧版把它当合法续行 → 闸门 0 findings（第 6 轮审计 R6-PF-G2-01）。
        if raw[:1] == "\t":
            return fields, i + 1, "badtab", key_lines
        if raw.strip() == "---":
            return fields, i + 1, "ok", key_lines   # i + 1：1-based，指向闭合的那一行
        m = FRONTMATTER_KEY_RE.match(raw)
        if not m:
            i += 1
            continue
        key = m.group(1)
        value = strip_inline_comment(m.group(2))
        key_lines[key] = i + 1
        # YAML 锚点 / 别名：`x-desc: &d …` 写在前面，`description: *d` 引用它。
        # 旧版把 `*d` 当字面量，合法写法被报 E5+E6+W2（第 6 轮审计 R6-PF-G2-02）。
        anchor = None
        anchor_match = re.match(r"&([A-Za-z0-9_-]+)\s*(.*)$", value)
        if anchor_match:
            anchor = anchor_match.group(1)
            value = anchor_match.group(2)
        if value.strip().startswith("*"):
            alias = value.strip()[1:].strip()
            fields[key] = anchors.get(alias, value.strip())
            i += 1
            continue
        if value.strip() in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            j = i + 1
            # 只认空格缩进：tab 开头的行留给外层循环报 badtab（第 6 轮审计 R6-PF-G2-01）。
            while j < len(lines) and (lines[j].strip() == "" or lines[j][:1] == " "):
                block.append(lines[j].strip())
                j += 1
            fields[key] = "\n".join(part for part in block if part)
            if anchor:
                anchors[anchor] = fields[key]
            i = j
            continue
        parts = [value.strip()]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                # YAML 纯标量里的空行是段落分隔（折叠后是一个换行），不是标量结束：
                # 只要下一个非空行仍是缩进续行，值就还没完（第 5 轮审计 R5-PF-G3-05）。
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if (k < len(lines) and lines[k][:1] == " "
                        and not FRONTMATTER_KEY_RE.match(lines[k])):
                    parts.append("")
                    j = k
                    continue
                break
            if nxt[:1] != " ":
                break
            if nxt.lstrip().startswith("#"):
                j += 1
                continue
            parts.append(strip_inline_comment(nxt).strip())
            j += 1
        fields[key] = unquote(_fold_plain_parts(parts))
        if anchor:
            anchors[anchor] = fields[key]
        i = j
    return fields, 0, "unclosed", key_lines


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def strip_fences(text: str) -> str:
    """去掉围栏代码块里的内容。

    闸门看的是"这个技能自己声称了什么"，而正文里的代码块大多是**引用的样例**：
    样例里出现的「先分诊」「不接」「AGENTS.md」是别人的文字，不该被当成这个技能的声明。

    围栏按 CommonMark 配对：同一种字符、闭合 run 不短于开栏 run、闭合行后面只能有空白。
    旧版只看前三个字符，```` 开的样例里嵌一个 ``` 就被提前关掉，样例文字漏进正文 ——
    只拷贝了 SKILL.md 的薄包装会被样例里的 `AGENTS.md` 误报 E7（第 5 轮审计 R5-PF-G3-07）。
    口径与 check_prompt.py 的 fence_lines() 一致。
    """
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in split_lines(text):
        stripped = line.lstrip()
        match = _FENCE_RE.match(stripped)
        if fence is None:
            if match is not None:
                fence = (match.group(1)[0], len(match.group(1)))
                continue
            kept.append(line)
        elif (match is not None and match.group(1)[0] == fence[0]
              and len(match.group(1)) >= fence[1]
              and not stripped[len(match.group(1)):].strip()):
            fence = None
    return "\n".join(kept)


_INDENT_CODE_RE = re.compile(r"^(?: {4}|\t)")


def _strip_indented_code(text: str) -> str:
    """去掉缩进代码块（CommonMark：空行后、4 空格/Tab 起头，直到非空且不缩进行）。

    正文里的代码样例是「别人的文字」：样例里出现的 `AGENTS.md` 不是这个技能的声明。
    围栏代码块已经由 `strip_fences` 剥掉，缩进代码块之前没剥 → 样例里的 `AGENTS.md`
    被当成同目录依赖误报 E7（第 6 轮审计 R6-PF-G2-04）。口径与 check_prompt.py 的
    `indented_code_lines()` 一致：只有「上一行是空行（或文件开头）」的缩进行才是代码块。
    """
    lines = split_lines(text)
    kept: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _INDENT_CODE_RE.match(line) and (idx == 0 or not lines[idx - 1].strip()):
            idx += 1
            while idx < len(lines) and (not lines[idx].strip() or _INDENT_CODE_RE.match(lines[idx])):
                idx += 1
            continue
        kept.append(line)
        idx += 1
    return "\n".join(kept)


def _sibling_prose(prose: str) -> str:
    """归一化同目录前缀 `./` / `.\\` 再匹配依赖：`./AGENTS.md` 与 `AGENTS.md` 是同一个同目录文件。

    `../` 与绝对路径仍不算同目录（前面的字符是 `/`、`\\`、`:`，会被 SIBLING_PATTERNS 的环视挡住）。
    旧版漏认 `./AGENTS.md`，只拷贝 SKILL.md 时依赖明明缺了却不报 E7（第 5 轮审计 R5-PF-G3-08）；
    第 6 轮审计 R6-PF-G2-05：Windows 同目录写法 `.\\AGENTS.md` 同样要认。
    """
    return re.sub(r"(?<!\.)\.[/\\]", "", prose)


def check_skill(path: Path) -> tuple[list[Finding], int]:
    """检查一个 SKILL.md。返回 (findings, 字符数)。"""
    findings: list[Finding] = []
    if path.is_dir():
        # 与闸门① 的口径一致：传目录时要说清"这不是文件"，别让人以为路径打错字了。
        findings.append(Finding("ERROR", "E1", f"这是目录，不是文件：{path}"
                                             "（要给的是 `<技能目录>/SKILL.md`）"))
        return findings, 0
    if not path.is_file():
        findings.append(Finding("ERROR", "E1", f"文件不存在：{path}"))
        return findings, 0

    text = read_text(path)
    lines = split_lines(text)
    fields, fm_end, fm_state, key_lines = parse_frontmatter(text)
    name_line = key_lines.get("name", 1)
    desc_line = key_lines.get("description", 1)

    if fm_state == "missing":
        findings.append(Finding("ERROR", "E1", "开头没有 YAML frontmatter（必须用 `---` 包住 name 与 description）", 1))
        return findings, len(text)
    if fm_state == "unclosed":
        # 宿主拿到这种文件会直接忽略（missing YAML frontmatter），正文根本不读 ——
        # 闸门必须跟着报错，不能因为"字段都解析到了"就放它过去。
        findings.append(Finding("ERROR", "E1",
                                "frontmatter 没闭合：找不到第二个 `---`；这种文件会被宿主整个忽略"
                                "（连正文都不读），补上收尾的 `---` 再说", 1))
        return findings, len(text)
    if fm_state == "badtab":
        # 真 YAML 解析器（宿主用的就是它）遇到 tab 缩进直接 ScannerError，技能会被整个忽略；
        # 旧版把 tab 当合法续行 → 闸门 0 findings（第 6 轮审计 R6-PF-G2-01）。
        findings.append(Finding("ERROR", "E1",
                                "frontmatter 里有 tab 缩进：YAML 不允许用 tab 做缩进，宿主会整个忽略"
                                "这个技能；续行 / 块标量请用空格缩进",
                                fm_end or 1))
        return findings, len(text)

    name = fields.get("name", "")
    desc = fields.get("description", "")
    if not name:
        findings.append(Finding("ERROR", "E1", "frontmatter 缺 `name`（宿主靠它登记这个技能）", 1))
    if not desc:
        findings.append(Finding("ERROR", "E1", "frontmatter 缺 `description`（模型侧就是靠它决定要不要用这个技能）", desc_line))

    # ---- name
    if name:
        if len(name) > NAME_MAX or not NAME_RE.match(name):
            findings.append(Finding("ERROR", "E2", f"name「{name}」不符合规范：{NAME_RULE}", name_line))
        # 目录名只做**路径层**归一化（abspath + normpath），不解析符号链接 / junction：
        # `path.parent.name` 在 `scripts/..\SKILL.md`、`./SKILL.md` 下是 `..` 或空串（判假 E3）；
        # 而 `path.resolve()` 会把 junction 指到真实目录，拿真实目录名比又判假 E3
        # —— 安装目录名才是宿主登记技能时用的名字（审计 P29）。
        dirname = Path(os.path.normpath(os.path.abspath(str(path)))).parent.name
        if name != dirname:
            findings.append(Finding("ERROR", "E3",
                                    f"name「{name}」与所在目录名「{dirname}」不一致；"
                                    f"agentskills 规范要求 name 必须等于父目录名（目录包形态就是 `<name>/SKILL.md`）", name_line))

    # ---- description
    if desc:
        head = desc[:CATALOG_RENDER_MAX]
        if len(desc) > DESC_MAX:
            findings.append(Finding("ERROR", "E4",
                                    f"description 有 {len(desc)} 字符，超过规范上限 {DESC_MAX}；"
                                    f"宿主只会截断，多出来的部分不会生效", desc_line))
        if not (has_marker(head, WHEN_MARKERS) or any(p.search(head) for p in WHEN_PATTERNS)):
            findings.append(Finding("ERROR", "E5",
                                    "description 前 500 字里没有「何时该用」的标记"
                                    "（当…时/如果/用户说/使用本技能/触发…）；模型侧全靠这句话判断要不要用，"
                                    "写成「这个技能做 X」不够", desc_line))
        if not has_marker(head, BOUNDARY_MARKERS):
            findings.append(Finding("ERROR", "E6",
                                    "description 前 500 字里没有「何时不该用」的边界"
                                    "（不接/不适用/不该用/不要用于…，要排除式说法；"
                                    "「只在…时触发」这类触发条件不算）；"
                                    "官方口径：描述过宽 = 在不该触发的时候触发", desc_line))
        if len(desc) > CATALOG_RENDER_MAX:
            findings.append(Finding("WARN", "W1",
                                    f"description 有 {len(desc)} 字符，超过 {CATALOG_RENDER_MAX}；"
                                    f"有的宿主只渲染前 {CATALOG_RENDER_MAX} 字（DSH 的 catalogDescriptionMaxLength 默认值），"
                                    f"把最强的触发信号放前面", desc_line))
        if not has_marker(desc, IMPERATIVE_MARKERS):
            findings.append(Finding("WARN", "W2",
                                    "description 不是祈使句；官方指南要求写成给模型的指令"
                                    "（「使用本技能：当…」/“Use this skill when…”），不是「这个技能做 X」", desc_line))
        elif has_marker(desc, THIRD_PERSON_MARKERS):
            findings.append(Finding("WARN", "W2",
                                    "description 里混了第三人称自我介绍（「本技能是…」）；"
                                    "官方指南要求祈使句，第三人称在路由上没有帮助", desc_line))

    # ---- 正文
    body_lines = lines[fm_end:] if fm_end else []
    body = "\n".join(body_lines)
    # 声明类的检查只看正文本身，不看正文里引用的样例代码块（围栏 + 缩进两类都剥）。
    prose = _strip_indented_code(strip_fences(body))
    body_start = fm_end + 1  # 1-based 行号，正文第一行（fm_end 已经是闭合 `---` 的行号）

    if prose.strip():
        # 同目录依赖：只有正文真的引用了同目录文件，才要求它在同目录里存在。
        # `../AGENTS.md`、绝对路径不算"同目录"（SIBLING_PATTERNS 的环视负责这件事）。
        referenced = [rel for rel, pattern in SIBLING_PATTERNS.items()
                      if pattern.search(_sibling_prose(prose))]
        # 薄包装形态：正文引用了同目录的规范本体，才要求写"动手前先读完"。
        is_wrapper = any(rel in SPEC_FILE_MARKERS for rel in referenced)

        # 分诊三态：只在正文声称了自己会判断"该不该接"时才要求写全（自足技能可以没有分诊）。
        claims_triage = has_marker(prose, TRIAGE_CLAIM_MARKERS)
        if claims_triage and not (has_marker(prose, TRIAGE_STATE_MARKERS)
                                  and has_marker(prose, TRIAGE_MID_MARKERS)
                                  and has_marker(prose, TRIAGE_ACCEPT_MARKERS)):
            findings.append(Finding("WARN", "W3",
                                    "正文写了分诊，却没有写全三态（接 go / 先问一句 needs-clarification / 不接 kill）；"
                                    "自动接住之后必须先判断该不该接，再决定进不进流程",
                                    marker_line(lines, TRIAGE_CLAIM_MARKERS, fm_end, body_start)))

        # 薄包装专项：指向规范本体，并且要求"先读完"。
        if is_wrapper and not has_marker(prose, SPEC_READ_MARKERS):
            findings.append(Finding("WARN", "W4",
                                    "正文引用了同目录的规范本体 `AGENTS.md`，但没写清「动手前先把它读完」",
                                    pattern_line(lines, SIBLING_PATTERNS["AGENTS.md"], fm_end, body_start)))

        # 菜单默认档：只在正文真的给了用户一份可选清单时才要求。
        if has_marker(prose, MENU_CLAIM_MARKERS) and not (
                has_marker(prose, DEFAULT_MARKERS) and has_marker(prose, OPT_OUT_MARKERS)):
            findings.append(Finding("WARN", "W5",
                                    "正文提供了菜单/选项，却没有写「每项自带默认档、用户可以不选」；"
                                    "官方口径是 Provide defaults, not menus，本仓库的折中全靠这条",
                                    marker_line(lines, MENU_CLAIM_MARKERS, fm_end, body_start)))

    # ---- 正文引用到的同目录文件是否真的在（只拷 SKILL.md 是常见的安装事故）
    missing = [rel for rel in referenced if not (path.parent / rel).exists()] if prose.strip() else []
    if missing:
        findings.append(Finding("ERROR", "E7",
                                "正文引用了同目录的 " + "、".join(f"`{rel}`" for rel in missing)
                                + "，但它不在同目录里（只统计同目录的相对引用；"
                                  "`../` 与绝对路径不算，把 SKILL.md 单独拷走会指向不存在的东西）",
                                marker_line(lines, list(missing), fm_end, body_start)))

    return findings, len(text)


def report(path: Path) -> int:
    findings, size = check_skill(path)
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]
    print(f"\n=== 检查 {path} （{size} 字符） ===")
    for f in sorted(findings, key=lambda x: (x.level != "ERROR", x.line)):
        print(f.render())
    if errors:
        print(f"结果：{len(errors)} ERROR, {len(warns)} WARN —— 未通过")
    else:
        print(f"结果：0 ERROR, {len(warns)} WARN —— 通过" + ("（WARN 需逐条说明）" if warns else ""))
    return 1 if errors else 0


SELFTEST_FIXTURE = """---
name: prompt-forge
description: 当用户给出一句话的模糊需求、而它要交给另一个 AI 去执行时，使用本技能：先分诊，再问询，最后落盘一份长提示词。不接：已经能直接执行的单步任务、纯问答检索。
---

# prompt-forge

开始之前，先把同目录的 `AGENTS.md` 从头到尾读一遍。

## 分诊：接 / 先问一句 / 不接

| 判断 | 什么时候 |
| --- | --- |
| 接（go） | 是可执行的活，信息基本齐 |
| 不接（kill） | 单步任务、纯问答 |

扩充菜单的每一项都自带默认档；用户说「都可以 / 你决定」就照默认走。
"""


def selftest() -> int:
    failed = 0

    # 1) 仓库里真实的 SKILL.md 必须过关（本技能自己就是第一份正例）。
    repo_skill = Path(__file__).resolve().parent.parent / "SKILL.md"
    if repo_skill.is_file():
        findings, _ = check_skill(repo_skill)
        errors = [f for f in findings if f.level == "ERROR"]
        ok = not errors
        print(f"{'PASS' if ok else 'FAIL'}  {repo_skill.name}: {len(errors)} ERROR（期望 0 ERROR）")
        if not ok:
            failed += 1
            for f in errors[:8]:
                print("      " + f.render())
    else:
        print(f"FAIL  找不到仓库里的 SKILL.md：{repo_skill}")
        failed += 1

    # 2) 反例：每个用例固定「期望报出的错误码集合」。
    #    加用例前先用真实文件跑一遍确认，不要照直觉写期望值。
    def sub(text: str, old: str, new: str) -> str:
        """替换前先断言目标真的在文本里：夹具漂移时 str.replace 会静默返回原文，
        用例就变成「空跑也 PASS」。"""
        if old not in text:
            raise AssertionError(f"替换目标不存在（夹具漂移了？）：{old!r}")
        return text.replace(old, new, 1)

    desc_line = [ln for ln in SELFTEST_FIXTURE.splitlines() if ln.startswith("description:")][0]
    name_line = "name: prompt-forge"

    inline = [
        # 基准件：本身必须干净，否则后面的用例分不清是谁报的错
        ("正例夹具", lambda: SELFTEST_FIXTURE, set()),
        ("name 大写", lambda: sub(SELFTEST_FIXTURE, name_line, "name: Prompt-Forge"), {"E2"}),
        ("name 连续连字符", lambda: sub(SELFTEST_FIXTURE, name_line, "name: prompt--forge"), {"E2"}),
        ("name 与目录名不一致", lambda: sub(SELFTEST_FIXTURE, name_line, "name: pf-forge"), {"E3"}),
        ("description 超 1024", lambda: sub(SELFTEST_FIXTURE, desc_line, desc_line + "补" * 1000), {"E4"}),
        ("description 没有触发条件",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 把模糊需求变成可执行的长提示词。不接已经能直接执行的单步任务。"), {"E5"}),
        ("description 没有边界",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。"), {"E6"}),
        # 边界写在 500 字之后：DSH 之类的宿主根本看不到，所以必须当前 500 字里没有边界来报
        ("边界写在 500 字之后",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。" + "补" * 600
                     + "。不接单步任务。"), {"E6"}),
        ("缺 description",
         lambda: sub(SELFTEST_FIXTURE, desc_line + "\n", ""), {"E1"}),
        ("缺 frontmatter",
         lambda: sub(SELFTEST_FIXTURE, "---\n" + name_line + "\n" + desc_line + "\n---\n\n", ""), {"E1"}),
        # ---- 下面这些是审计撞出来的真 bug 的回归用例（每条修法都对应一段实现口径）
        # frontmatter 没闭合：宿主会整个忽略这个技能，闸门不能报"通过"
        ("frontmatter 未闭合",
         lambda: sub(SELFTEST_FIXTURE, "---\n\n# prompt-forge", "\n# prompt-forge"), {"E1"}),
        # YAML 纯标量折行：边界写在续行里也要认
        ("description 折行续写",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。\n"
                     "  触发信号：需求只有一句话、缺交付物路径。\n"
                     "  不接：单步任务、纯问答检索翻译。"), set()),
        # 值写在下方的缩进续行里（旧版会误报「缺 description」）
        ("description 写在缩进续行里",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description:\n  当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。不接单步任务。"), set()),
        # 行内注释：不能把注释吞进 name（假 E2 + 假 E3）
        ("name 行内注释",
         lambda: sub(SELFTEST_FIXTURE, name_line, "name: prompt-forge  # 本机安装的薄包装"), set()),
        # 反过来：边界只写在注释里 = 宿主读到的描述里根本没有边界，必须报 E6
        ("边界只写在注释里",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。  # 不接单步任务"), {"E6"}),
        # `只在…时触发` 是触发条件，不是"何时不该用"（旧版把它当边界 → E6 假绿）
        ("只在…时触发 不算边界",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词，只在需求模糊时触发。"), {"E6"}),
        # 标记词被空格拆开也要认（排版折行、手滑加空格都很常见）
        ("边界词被空格拆开",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。不 接：单步任务。"), set()),
        # description 里带 U+2028：它不是换行，不能把值劈成两半（旧版会假报 E6）
        ("description 里带 U+2028",
         lambda: sub(SELFTEST_FIXTURE, "不接：已经", "不接：\u2028已经"), set()),
        # ---- 第 5 轮审计撞出来的 YAML 写法假红（宿主按 YAML 解析后都是合规描述）
        # 单引号标量里的 `''` 是转义的单引号：`Don''t use` 就是 `Don't use`，不该报 E6
        ("单引号 YAML 里的 '' 转义（第 5 轮 R5-PF-G3-06）",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 'When the user needs a demo. Don''t use for chat.'"), set()),
        # 纯标量的空行是段落分隔（折叠成一个换行），边界写在第二段也要认
        ("description 纯标量里空行后的第二段（第 5 轮 R5-PF-G3-05）",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户需要演示时，使用本技能给出步骤。\n\n  不接：纯聊天。"), set()),
        # 边界词句首大写：Don't use 与词表里的 don't use 是同一个边界（第 5 轮 R5-PF-G3-10）
        ("边界词句首大写 Don't use（第 5 轮 R5-PF-G3-10）",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: \"When the user needs a demo. Don't use for chat.\""), set()),
        # 薄包装只拷 SKILL.md：`./AGENTS.md` 与 `AGENTS.md` 是同一个同目录文件，不能漏报
        ("只拷 SKILL.md 且依赖写成 ./AGENTS.md（第 5 轮 R5-PF-G3-08）",
         lambda: sub(SELFTEST_FIXTURE, "`AGENTS.md`", "`./AGENTS.md`"), {"E7"}),
        # 薄包装只拷一个 SKILL.md：AGENTS.md / references/ / scripts/ 都不在
        ("只拷 SKILL.md（缺同目录文件）", lambda: SELFTEST_FIXTURE, {"E7"}),
        # ---- 第 6 轮审计撞出来的 YAML 假绿/假红与路径漏认 ----
        # frontmatter 用 tab 缩进：真 YAML 解析器直接报错，宿主会整个忽略技能（第 6 轮 R6-PF-G2-01）
        ("frontmatter 用 tab 缩进（YAML 非法）",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "description: 当用户需要演示时，使用本技能给出步骤，不接：纯聊天。\n"
                     "\t触发信号补充：需求只有一句话。"),
         {"E1"}),
        # YAML 锚点/别名：`description: *d` 是合法 YAML，旧版当字面量 → 假 E5+E6+W2（第 6 轮 R6-PF-G2-02）
        ("description 用 YAML 锚点/别名引用",
         lambda: sub(SELFTEST_FIXTURE, desc_line,
                     "x-desc: &d 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词。不接：单步任务。\n"
                     "description: *d"),
         set()),
        # Windows 同目录写法 `.\AGENTS.md` 也是同目录依赖（第 6 轮 R6-PF-G2-05）
        ("只拷 SKILL.md 且依赖写成 .\\AGENTS.md",
         lambda: sub(SELFTEST_FIXTURE, "`AGENTS.md`", "`.\\AGENTS.md`"),
         {"E7"}),
    ]

    # 这两个用例本来就靠"不改夹具"来表达意图：正例是基准件，E7 是"文件没少、只是没拷过来"
    no_edit_cases = ("正例夹具", "只拷 SKILL.md（缺同目录文件）")
    # 非法 name 的两个用例把**目录名也改成同一个值**：否则 E3（name 与目录名不一致）会跟着一起报，
    # 就看不出 E2 是独立生效的。夹具仍写进这个目录，符合"目录包 <name>/SKILL.md"的形态。
    dir_overrides = {"name 大写": "Prompt-Forge", "name 连续连字符": "prompt--forge"}

    for name, build, want in inline:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / dir_overrides.get(name, "prompt-forge")
            skill_dir.mkdir()
            path = skill_dir / "SKILL.md"
            try:
                text = build()
                if name not in no_edit_cases and text == SELFTEST_FIXTURE:
                    raise AssertionError("没有改动夹具，用例是空跑的")
            except AssertionError as exc:
                print(f"FAIL  [闸门] {name}: 用例构造失败")
                print(f"      ERROR: 自测用例 [{name}] 不可用：{exc}")
                failed += 1
                continue
            write_text_lf(path, text)
            # 除了"只拷 SKILL.md"这个用例，其余都补上薄包装依赖的同目录文件
            if want != {"E7"}:
                (skill_dir / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
                (skill_dir / "references").mkdir()
                (skill_dir / "scripts").mkdir()
                (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
            findings, _ = check_skill(path)
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] {name}: ERROR 码 {sorted(got)}（期望 {sorted(want)}）")
        if not ok:
            failed += 1
            for f in findings:
                if f.level == "ERROR":
                    print(f"      {f.render()}")

    # 2.5) 嵌套围栏：```` 包的样例里再写 ```，样例仍是别人的文字。只有 `<name>/SKILL.md`
    #      没有同目录 AGENTS.md 时，样例里那句「先读 AGENTS.md」不能被算成本技能的依赖
    #      （旧版只认前三个反引号，内层 ``` 就把外层样例提前关掉 → 假 E7，第 5 轮 R5-PF-G3-07）。
    nested = (
        "---\n"
        "name: prompt-forge\n"
        "description: 当用户需要演示时，使用本技能给出步骤。不接：纯聊天。\n"
        "---\n\n"
        "# 演示\n\n"
        "下面是别的技能的样例：\n\n"
        "````markdown\n"
        "```\n"
        "开始之前先读同目录的 AGENTS.md。\n"
        "```\n"
        "````\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        skill_dir.mkdir()
        path = skill_dir / "SKILL.md"
        write_text_lf(path, nested)
        findings, _ = check_skill(path)   # 故意不放 AGENTS.md：样例不是本技能的依赖
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = not got
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 嵌套围栏里的样例不算同目录依赖: "
              f"ERROR 码 {sorted(got)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                if f.level == "ERROR":
                    print(f"      {f.render()}")

    # 2.6) 分诊三态：正文声称分诊却只写接/不接两态（漏「先问一句 / needs-clarification」）要报 W3；
    #      三态齐全不报。旧版只查两态，两态正文假绿（第 6 轮审计 R6-PF-G2-03）。
    two_state = SELFTEST_FIXTURE.replace("## 分诊：接 / 先问一句 / 不接", "## 分诊：接 / 不接")
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        skill_dir.mkdir()
        path = skill_dir / "SKILL.md"
        write_text_lf(skill_dir / "AGENTS.md", "# 规范\n")
        (skill_dir / "references").mkdir()
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
        for text, want_w3 in ((two_state, True), (SELFTEST_FIXTURE, False)):
            write_text_lf(path, text)
            findings, _ = check_skill(path)
            got_w3 = [f for f in findings if f.code == "W3"]
            ok = bool(got_w3) == want_w3
            print(f"{'PASS' if ok else 'FAIL'}  [闸门] 分诊{'两态' if want_w3 else '三态'}要"
                  f"{'报' if want_w3 else '不报'} W3: {'有' if got_w3 else '无'}（期望 "
                  f"{'有' if want_w3 else '无'}）")
            if not ok:
                failed += 1
                for f in findings:
                    print(f"      {f.render()}")

    # 2.7) 缩进代码块里的样例也是别人的文字：样例里写 references/ 不能算本技能的依赖
    #      （旧版只剥围栏 → 假 E7，第 6 轮审计 R6-PF-G2-04）。
    indented_sample = SELFTEST_FIXTURE.replace(
        "扩充菜单的每一项都自带默认档；用户说「都可以 / 你决定」就照默认走。",
        "下面是别的技能的样例（不是本技能的依赖）：\n\n"
        "    开始之前先读同目录的 references/ 目录。\n")
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        skill_dir.mkdir()
        path = skill_dir / "SKILL.md"
        write_text_lf(path, indented_sample)
        # 正文真正依赖的 AGENTS.md / scripts 补上，故意不建 references：
        write_text_lf(skill_dir / "AGENTS.md", "# 规范\n")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
        findings, _ = check_skill(path)
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = not got
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 缩进代码块里的样例不算同目录依赖: "
              f"ERROR 码 {sorted(got)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                if f.level == "ERROR":
                    print(f"      {f.render()}")

    # 3) 相对路径调用不能误报 E3。这是真实踩过的坑：
    #    在技能目录里跑 `python check_skill.py SKILL.md` 时 Path("SKILL.md").parent 是 `.`，
    #    目录名取出来是空字符串，于是「name 与目录名不一致」被误报。
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        skill_dir.mkdir()
        write_text_lf(skill_dir / "SKILL.md", SELFTEST_FIXTURE)
        (skill_dir / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
        (skill_dir / "references").mkdir()
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
        try:
            os.chdir(skill_dir)
            findings, _ = check_skill(Path("SKILL.md"))   # 故意用相对路径
        finally:
            # 必须在离开 with 之前切回来：Windows 上 cwd 还钉在临时目录里，
            # TemporaryDirectory 清理会报 WinError 32。
            os.chdir(cwd)
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = not got
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 用相对路径调用（cwd 在技能目录内）: "
              f"ERROR 码 {sorted(got)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                if f.level == "ERROR":
                    print(f"      {f.render()}")

    # 4) examples/技能定义/ 下的每个用例：ERROR 与 WARN 码集合都必须与预期一致。
    #    这张表同时是"覆盖清单"：新增了示例目录却没登记进来，自测会失败。
    expected: dict[str, tuple[set[str], set[str]]] = {
        "good-minimal": (set(), set()),            # 自足技能：只查 frontmatter 与描述
        "missing-siblings": ({"E7"}, set()),        # 引用了同目录文件，但它不在
        "oversize-description": ({"E4"}, {"W1"}),   # 描述 1648 字符
        "Bad-Name": ({"E2"}, set()),                # name 大写
        "dir-mismatch": ({"E3"}, set()),            # name 与目录名不一致
        "no-frontmatter": ({"E1"}, set()),          # 没有 frontmatter
        "no-trigger": ({"E5"}, {"W2"}),             # 描述里没有"何时该用"，且不是祈使句
        "no-boundary": ({"E6"}, set()),             # 描述里没有"何时不该用"
        "boundary-after-500": ({"E6"}, {"W1"}),     # 边界写在第 500 字之后 = 宿主看不到
    }
    examples_root = Path(__file__).resolve().parent.parent / "examples" / "技能定义"
    if not examples_root.is_dir():
        print(f"FAIL  找不到示例目录：{examples_root}")
        failed += 1
    else:
        found = sorted(p.name for p in examples_root.iterdir() if (p / "SKILL.md").is_file())
        for extra in sorted(set(found) - set(expected)):
            print(f"FAIL  [示例] {extra}: 目录里有 SKILL.md，但没有登记进自测的预期表")
            failed += 1
        for case, (want_err, want_warn) in sorted(expected.items()):
            skill = examples_root / case / "SKILL.md"
            if not skill.is_file():
                print(f"FAIL  [示例] {case}: 找不到 {skill}")
                failed += 1
                continue
            findings, _ = check_skill(skill)
            got_err = {f.code for f in findings if f.level == "ERROR"}
            got_warn = {f.code for f in findings if f.level == "WARN"}
            ok = got_err == want_err and got_warn == want_warn
            print(f"{'PASS' if ok else 'FAIL'}  [示例] {case}: ERROR {sorted(got_err)} WARN {sorted(got_warn)}"
                  f"（期望 ERROR {sorted(want_err)} WARN {sorted(want_warn)}）")
            if not ok:
                failed += 1
                for f in findings:
                    print(f"      {f.render()}")

    # 5) "同目录"三个字的语义：`../AGENTS.md` 与绝对路径指的是**别处**的文件，不算同目录依赖，
    #    也就不该拿"它不在同目录"去判红（旧版把二者都算进去，文件明明存在却报 E7）。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skill_dir = root / "prompt-forge"
        skill_dir.mkdir()
        (root / "AGENTS.md").write_text("# 上级目录的规范\n", encoding="utf-8")
        elsewhere = sub(
            SELFTEST_FIXTURE, "开始之前，先把同目录的 `AGENTS.md` 从头到尾读一遍。",
            "开始之前，先把上级目录的 `../AGENTS.md` 读一遍"
            f"（绝对路径写法也可以：`{root.as_posix()}/AGENTS.md`）。")
        path = skill_dir / "SKILL.md"
        write_text_lf(path, elsewhere)
        findings, _ = check_skill(path)
        got = {f.code for f in findings}
        ok = not got
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 上级目录/绝对路径引用不算同目录依赖: 码 {sorted(got)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

        # 反向：真·同目录引用（裸文件名）而文件不在，E7 必须照报
        bare_root = root / "bare-case" / "prompt-forge"
        bare_root.mkdir(parents=True)
        write_text_lf(bare_root / "SKILL.md", SELFTEST_FIXTURE)
        findings, _ = check_skill(bare_root / "SKILL.md")
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = got == {"E7"}
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 裸文件名引用而文件不在: ERROR 码 {sorted(got)}（期望 ['E7']）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

    # 6) 行号要对得上编辑器：description 里带 U+2028（合法 YAML 值）时不能让 W4 指错行。
    #    旧版用 str.splitlines() 把这一行劈成两行，行号整体漂移，报的是一个空行。
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        skill_dir.mkdir()
        (skill_dir / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
        rows = [
            "---",
            "name: prompt-forge",
            "description: 当用户给出模糊需求时，使用本技能把它写成可执行的长提示词\u2028不接：单步任务、纯问答检索。",
            "---",
            "",
            "# prompt-forge",
            "",
            "规范本体在同目录的 `AGENTS.md`。",   # 第 8 行：点了规范本体，但没写"先读完" → W4
            "",
            "## 分诊：接 / 先问一句 / 不接",
            "",
            "| 判断 | 什么时候 |",
            "| --- | --- |",
            "| 接（go） | 可以干 |",
            "| 不接（kill） | 单步任务 |",
            "",
            "扩充菜单每一项都自带默认档；用户说「都可以」照默认走。",
        ]
        path = skill_dir / "SKILL.md"
        write_text_lf(path, "\n".join(rows) + "\n")
        findings, _ = check_skill(path)
        codes = {f.code for f in findings}
        w4 = [f for f in findings if f.code == "W4"]
        ok = codes == {"W4"} and len(w4) == 1 and w4[0].line == 8
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 行号对得上编辑器（描述里带 U+2028）: "
              f"码 {sorted(codes)}，W4 行号 {w4[0].line if w4 else '—'}（期望 ['W4'] / 第 8 行）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

    # 7) 审计撞出来的几个"标记词匹配"缺陷的回归用例。
    def case(label: str, rows_text: str, want_codes: set[str], want_msg: str = "") -> None:
        """写一份 SKILL.md 跑一遍，比对 ERROR/WARN 码集合（以及可选的关键文案）。"""
        nonlocal failed
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "prompt-forge"
            skill_dir.mkdir()
            (skill_dir / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
            (skill_dir / "references").mkdir()
            (skill_dir / "scripts").mkdir()
            (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
            path = skill_dir / "SKILL.md"
            write_text_lf(path, rows_text)
            findings, _ = check_skill(path)
        codes = {f.code for f in findings}
        hits = want_msg and any(want_msg in f.message for f in findings)
        ok = codes == want_codes and (hits or not want_msg)
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] {label}: 码 {sorted(codes)}（期望 {sorted(want_codes)}）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

    # 三态的"接住"必须真的写出来：`不接（kill）` 里的那半截「接（」不算，
    # `good` 里的 `go` 也不算（旧版两条都算 → 只写"不接"也能过 W3）
    case("只写『不接（kill）』不算三态齐全",
         sub(SELFTEST_FIXTURE, "接（go）", "这个技能 good 用"), {"W3"})
    # 反过来：真的写了 go（哪怕带括号）必须认，不能变成假黄
    case("真的写了 go 不算缺三态",
         sub(SELFTEST_FIXTURE, "接（go）", "go（接住）"), set())
    # 裸字「当」曾让 E5 空转：`当前` 里含「当」，于是没有触发条件的描述也能过
    case("「当前」不该被当成触发条件",
         sub(SELFTEST_FIXTURE, desc_line,
             "description: 把下载文件夹按类型整理成分类目录，当前只支持 Windows；不接：与文件整理无关的任务。"),
         {"E5", "W2"})
    # 「不适用」里有「适用」：边界句不能反过来当成触发句
    case("「不适用于」不该被当成触发条件",
         sub(SELFTEST_FIXTURE, desc_line,
             "description: 把下载文件夹按类型整理成分类目录。不适用于与文件整理无关的任务。"),
         {"E5", "W2"})
    # 反过来：`当…时` 是正规写法，必须认
    case("当…时 仍算触发条件",
         sub(SELFTEST_FIXTURE, desc_line,
             "description: 当文件夹很乱时，用本技能把它整理成分类目录。不接：与文件整理无关的任务。"),
         set())
    # `当…时` 句子长一点也要认（审计 P28：旧版限 10 字，正常长度就漏）
    case("长一点的当…时 也算触发条件",
         sub(SELFTEST_FIXTURE, desc_line,
             "description: 当用户提出一个说不清的目标、又必须交给另一个 AI 去执行的模糊需求时，"
             "用本技能把它写成可执行的提示词。不接：与需求澄清无关的任务。"),
         set())
    # 正文里的 Go 语言名不能冒充「接（go）」三态（审计 P34）
    case("正文用 Go 语言写脚本不算写了 go 三态",
         sub(SELFTEST_FIXTURE, "接（go）", "不接（kill），脚本用 Go 写"), {"W3"})
    # `scripts\..\SKILL.md` 这种写法下 path.parent.name 是 `..`：目录名按 normpath 归一化后的词法路径取
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "prompt-forge"
        (skill_dir / "sub").mkdir(parents=True)
        (skill_dir / "AGENTS.md").write_text("# 规范\n", encoding="utf-8")
        (skill_dir / "references").mkdir()
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "check_prompt.py").write_text("", encoding="utf-8")
        write_text_lf(skill_dir / "SKILL.md", SELFTEST_FIXTURE)
        findings, _ = check_skill(skill_dir / "sub" / ".." / "SKILL.md")
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = not got
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 路径里带 .. 时目录名按词法路径取（不解析链接）: ERROR 码 {sorted(got)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

        # 传目录：文案要说清"这是目录，不是文件"，别只报"文件不存在"
        findings, _ = check_skill(skill_dir)
        got = {f.code for f in findings if f.level == "ERROR"}
        ok = got == {"E1"} and any("这是目录，不是文件" in f.message for f in findings)
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 传目录时说明这是目录: ERROR 码 {sorted(got)}（期望 ['E1'] + 文案）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

    # 带 UTF-8 BOM 的 SKILL.md 不能报假 E1（审计 P27：旧版按 utf-8 解码，首行变成 \ufeff---）
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "bom-skill"
        skill_dir.mkdir()
        body = ("---\nname: bom-skill\n"
                "description: 使用本技能：当用户要整理文件时按类型归类。不接：与文件整理无关的任务。\n"
                "---\n\n正文。\n")
        (skill_dir / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        findings, _ = check_skill(skill_dir / "SKILL.md")
        codes = {f.code for f in findings}
        ok = codes == set()
        print(f"{'PASS' if ok else 'FAIL'}  [闸门] 带 BOM 的 SKILL.md 不报假 E1（P27）: 码 {sorted(codes)}（期望 []）")
        if not ok:
            failed += 1
            for f in findings:
                print("      " + f.render())

    # 安装形态：name 必须等于**安装目录**名，不能解析链接（审计 P29）。
    # Linux 上直接建符号链接；Windows 没有开发者模式建不了符号链接，但 junction 不需要权限
    # （mklink /J），所以退回 junction 跑同一组用例。两条用例在本地必须真跑：只在 Linux CI
    # 上才红的夹具错误没人拦得住（本轮就踩过一次，见 v1.0.x 的 check_skill 自测修复）。
    def make_dir_link(real: Path, link: Path) -> bool:
        try:
            os.symlink(real, link, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            pass
        if os.name != "nt":
            return False
        try:
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(real)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError:
            return False
        return proc.returncode == 0 and link.is_dir()

    def link_case(label: str, real_name: str, link_name: str, skill_name: str, want_e3: bool) -> None:
        nonlocal failed
        skill = ("---\nname: " + skill_name + "\n"
                 "description: 使用本技能：当用户要整理文件时按类型归类。不接：与文件整理无关的任务。\n"
                 "---\n\n正文。\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / real_name
            real.mkdir()
            write_text_lf(real / "SKILL.md", skill)
            link = root / link_name
            if not make_dir_link(real, link):
                print(f"SKIP  [闸门] {label}: 当前环境建不了目录链接（符号链接与 junction 都失败），"
                      "该场景用本地 junction 夹具人工验证")
                return
            findings, _ = check_skill(link / "SKILL.md")
            got = {f.code for f in findings if f.level == "ERROR"}
            want = {"E3"} if want_e3 else set()
            ok = got == want
            print(f"{'PASS' if ok else 'FAIL'}  [闸门] {label}: ERROR 码 {sorted(got)}（期望 {sorted(want)}）")
            if not ok:
                failed += 1
                for f in findings:
                    print("      " + f.render())

    # ① 真实目录 real-skill、安装名 installed-as，frontmatter 仍是 real-skill：
    #    该报 E3（旧版 resolve() 会漏报）。② 真实目录 target-dir、安装名 linked-name，
    #    frontmatter 也是 linked-name：不该报 E3（旧版 resolve() 会误报）。
    link_case("安装目录与真实目录名不同（不解析链接）", "real-skill", "installed-as", "real-skill", True)
    link_case("链接名就是安装名时不能假报 E3", "target-dir", "linked-name", "linked-name", False)

    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Agent Skill 的 SKILL.md 能不能被宿主自动识别")
    parser.add_argument("files", nargs="*", help="要检查的 SKILL.md 路径")
    parser.add_argument("--selftest", action="store_true", help="用仓库里的 SKILL.md 与内置反例自测")
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
            # 与闸门① 的文案对齐：传目录时别只说"文件不存在"，否则像是在怪用户打错字。
            print(f"ERROR: 这是目录，不是文件：{path}（要给的是 `<技能目录>/SKILL.md`）")
            code = 1
            continue
        if not path.is_file():
            print(f"ERROR: 文件不存在：{path}")
            code = 1
            continue
        code |= report(path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
