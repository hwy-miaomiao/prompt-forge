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
import re
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

#: 豁免注释：`<!-- pf-ignore: W2 这版先不做量化 -->`。
#: 只允许豁免 WARN —— ERROR 是「不改就不能交付」，给它开后门等于闸门失效。
IGNORE_RE = re.compile(r"<!--\s*pf-ignore\s*[:：]\s*([^>\n]*?)\s*-->")
IGNORE_CODES = tuple(f"W{i}" for i in range(1, 10))
ERROR_CODES = ("E0", "E1", "E2", "E2b", "E3", "E3b", "E4", "E5", "E6", "E7", "E8", "E9", "E10")
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

#: 交付物/需求里的文件路径。白名单列常见产物，白名单外的扩展名走兜底（go / gif / xml / db…），
#: 免得仅仅因为扩展名不在表里就误报 E8。
PATH_RE = re.compile(
    r"[A-Za-z0-9_\-.\\/]+\.(?:md|py|js|mjs|cjs|ts|tsx|jsx|json|jsonc|txt|csv|tsv|html|css|scss|"
    r"yml|yaml|toml|ini|sh|ps1|cmd|bat|sql|xlsx|docx|pptx|pdf|png|jpg|jpeg|svg|webp|exe|zip|log|env|"
    r"c|h|r|m|o|a|7z|"
    r"[A-Za-z][A-Za-z0-9]{1,7})",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\d")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
HEADING_RE = re.compile(r"^\s{0,3}#{1,4}\s+\S")

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


def strip_comments(text: str) -> str:
    """把 HTML 注释换成等量空行：注释是给人的元信息，不是正文。

    为什么必须剥：`<!-- pf-ignore: W2 … -->` 里的 `W2` **带着数字**。注释紧跟在条目后面时
    它就是这条需求的续行，`NUMBER_RE` 一看到那个 `2` 就认为「这条需求有数字、可验证」，
    W2 于是静默消失 —— 豁免注释自己把要豁免的检查弄成了假绿（实测：`- **R3** 界面要顺手。`
    加一行 `<!-- pf-ignore: W2 … -->` 之后 W2 既不报也不豁免，直接不见了）。
    换成等量空行是为了保住行号：E9/W9 的报错行号要指向编辑器里那一行。
    """
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def normalize_heading(raw: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,4}\s*", "", raw).strip()
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
    head = re.sub(r"^\s{0,3}#{1,4}\s*", "", raw).strip()
    return bool(SECTION_NUM_RE.match(strip_emphasis(head)))


def heading_number(raw: str) -> int | None:
    """标题编号里的数字（`## 4. 需求（R#）` → 4；`## （4）需求` → 4；`## 3D 素材` → None）。

    绑定「第 N 段」时，号数与 N 一致的那条标题优先 —— 只看「带不带编号」不够：
    `# 3 用户需求说明` 也是一条带编号的同义标题，位置还常常更靠前，光按文档顺序
    它就会把 `## 4 需求（R#）` 的绑定抢走（审计用例 C2-h1num-above）。
    """
    head = re.sub(r"^\s{0,3}#{1,4}\s*", "", raw).strip()
    match = SECTION_NUM_RE.match(strip_emphasis(head))
    if not match:
        return None
    digits = re.search(r"\d+", match.group(0))
    return int(digits.group(0)) if digits else None


#: 别名前面紧跟「非 / 不」时不算命中：「非目标」是第 5 段，不是第 1 段里的「目标」。
NEGATION_CHARS = "非不无"

#: `判定：` 的三种取值（AGENTS.md 第 4 节）：命令 / 阈值 / 人工，三选一。
JUDGE_KINDS = ("命令", "阈值", "人工")

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
    """返回条目里第一处「判定」之后写的三态取值；一种都没写就返回 None。"""
    at = text.find("判定")
    if at == -1:
        return None
    tail = text[at:]
    return next((kind for kind in JUDGE_KINDS if kind in tail), None)


def is_empty_claim(text: str) -> bool:
    """段落是否为空或只写了「无」（容忍「无。」「（无）」这类标点/括号写法）。

    注释不算内容：`## 10 假设与开放问题` 下面只挂一行 `<!-- 等用户补 -->` 仍然是空段。
    """
    cleaned = re.sub(r"[\s。，,、；;：:！!？?\-—~～*•·]+", "",
                     strip_emphasis(strip_comments(text)))
    return cleaned.strip("（）()【】[]<>「」").lower() in EMPTY_CLAIMS


def fence_lines(lines: list[str]) -> list[bool]:
    """标记每行是否在 ``` / ~~~ 围栏内 —— 围栏里的 `#` 是注释，不是标题。"""
    flags: list[bool] = []
    fence: str | None = None
    for line in lines:
        stripped = line.strip()
        if fence is None and stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
        elif fence is not None and stripped.startswith(fence):
            fence = None
            flags.append(False)
            continue
        flags.append(fence is not None)
    return flags


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def split_sections(lines: list[str]) -> tuple[list[tuple[str, int, int, int, bool]], list[str]]:
    """返回 ([(归一化标题, 标题级别, 起始行号, 结束行号, 有没有编号)], 无标题前的前言行)。

    级别和编号都要留着：文档标题（`# 任务：…`）和正文段落一样会被当成候选段落，
    而它只是一句话的名字，不该抢走任何一段的绑定（见 `check_file` 里的 E1 绑定）。
    """
    headings: list[tuple[str, int, int, bool]] = []
    fenced = fence_lines(lines)
    for idx, line in enumerate(lines):
        if fenced[idx]:
            continue
        if HEADING_RE.match(line):
            depth = line.lstrip()
            headings.append((normalize_heading(line), len(depth) - len(depth.lstrip("#")), idx,
                             heading_has_number(line)))
    sections: list[tuple[str, int, int, int, bool]] = []
    for i, (title, level, start, numbered) in enumerate(headings):
        end = headings[i + 1][2] if i + 1 < len(headings) else len(lines)
        sections.append((title, level, start, end, numbered))
    preamble = lines[: headings[0][2]] if headings else lines
    return sections, preamble


def body_of(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start + 1 : end])


def item_start(text: str, letter: str) -> int | None:
    # 引用块写法 `> **R1** …` 与列表写法 `- **R1** …` 都要认出来：条目本身是什么形式
    # 不影响编号的收集，把 `>` 和 `*` 一起剥掉再找编号。
    probe = ITEM_STRIP_RE.sub("", text.strip()).lstrip(">*`").lstrip()
    match = re.match(rf"{letter}(\d+)(?!\d)", probe)
    return int(match.group(1)) if match else None


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


def collect_items(lines: list[str], letter: str) -> tuple[dict[int, list[str]], list[tuple[int, int]]]:
    """扫全文收集 `R1` / `A2` 开头的条目。

    返回 ({编号: 条目正文行}, [(编号, 行号), ...] 重复编号)。条目块在遇到下一条目或下一个标题时结束。
    """
    starts: list[tuple[int, int]] = []
    fenced = fence_lines(lines)
    for idx, line in enumerate(lines):
        # 围栏里的 `- **R9** 这里只是写法示例` 是**引用的样例**，不是作者的条目：与 `split_sections`
        # （会跳过围栏内的标题）保持同一口径。不跳过就有两个方向的错：围栏示例变成「幽灵需求」
        # 逼出 E6，或者整份文档的条目全写在围栏里却一路绿灯（审计 BUG-B）。
        if fenced[idx]:
            continue
        num = item_start(line, letter)
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
        block: list[str] = []
        for j in range(idx, end):
            if j != idx and not fenced[j] and HEADING_RE.match(lines[j]):
                break
            block.append(lines[j])
        items.setdefault(num, []).extend(block)
    return items, duplicates


def check_file(path: Path, use_ignore: bool = True,
               suppressed: list[tuple[Finding, str]] | None = None) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    raw = read_text(path)
    lines = raw.splitlines()
    fenced = fence_lines(lines)

    sections, _preamble = split_sections(lines)
    if not sections:
        findings.append(Finding("ERROR", "E0", "找不到任何 Markdown 标题；长提示词必须按 12 段分节"))
        return findings, len(raw)

    #: 豁免注释：`<!-- pf-ignore: W2 理由 -->`。只对 WARN 生效，写法问题报 E11（见 parse_ignores）。
    ignores, ignore_problems = parse_ignores(lines)
    where = section_index(lines, sections)
    findings.extend(ignore_problems)

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
                not fenced[j] and item_start(lines[j], item_letter) is not None
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

    # ---- R# 需求 -----------------------------------------------------------
    requirements, dup_r = collect_items(lines, "R")
    if len(requirements) < 3:
        findings.append(
            Finding("ERROR", "E2", f"编号需求不足：找到 {len(requirements)} 条 R#，至少 3 条（形如 `R1 ...`）")
        )
    for num, line_no in dup_r:
        findings.append(Finding("ERROR", "E2b", f"需求编号 R{num} 重复出现（编号必须唯一）", line_no))

    # ---- A# 验收 -----------------------------------------------------------
    acceptances, dup_a = collect_items(lines, "A")
    for num, line_no in dup_a:
        findings.append(Finding("ERROR", "E3b", f"验收编号 A{num} 重复出现（编号必须唯一）", line_no))
    if len(acceptances) < 3:
        findings.append(
            Finding("ERROR", "E3", f"编号验收不足：找到 {len(acceptances)} 条 A#，至少 3 条（形如 `A1（对应 R1）...`）")
        )
    referenced: set[int] = set()
    for num, block in sorted(acceptances.items()):
        text = "\n".join(block)
        # E4 / E6 的判据是「这条 A# 自己声明的需求号」—— 见 declared_refs()：条目行、续行里
        # 「对应/覆盖/…」后面的、以及带这些关键词的括号段。正文里顺带提一句别的 R# 不算覆盖。
        refs = declared_refs(block, num)
        if not refs:
            findings.append(Finding("ERROR", "E4", f"验收 A{num} 没有引用任何需求编号（应写「对应 R1」）"))
        referenced |= refs
        if judge_kind(text) is None:
            findings.append(
                Finding("ERROR", "E5", f"验收 A{num} 没有 `判定：`，或没写判定方式（命令 / 阈值 / 人工 三者之一）")
            )
    for num in sorted(set(requirements) - referenced):
        findings.append(Finding("ERROR", "E6", f"需求 R{num} 没有任何验收条目覆盖它"))

    # ---- E7 非目标 ---------------------------------------------------------
    goal_label = "5 非目标"
    if goal_label in matched:
        start, end = matched[goal_label]
        body = body_of(lines, start, end)
        if is_empty_claim(body):
            findings.append(Finding("ERROR", "E7", "非目标段落为空或只写了「无」；至少写 1 条不做的事"))
        elif len([ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("|")]) < 2 \
                and len([ln for ln in body.splitlines() if ln.strip()]) < 2:
            findings.append(Finding("WARN", "W6", "非目标只写了 1 条，建议补到 2 条以上"))

    # ---- E8 交付物路径 -----------------------------------------------------
    deliv_label = "3 交付物"
    if deliv_label in matched:
        start, end = matched[deliv_label]
        body = body_of(lines, start, end)
        if not PATH_RE.search(body):
            findings.append(
                Finding("ERROR", "E8", "交付物段落里没有任何带扩展名的路径（如 `out/report.csv`）")
            )
        _flag_vague(findings, "交付物", body, start, "WARN")

    # ---- E9 模糊词 ---------------------------------------------------------
    # 用段落的实际行号区间做去重键：标题字数由使用者决定（`## 4. 需求（R#）` 或 `## 需求（R#）` 都合法），
    # 拿 label 字符串比对会在不编号的标题上失配，同一段就会被 E9 和 W9 各报一次。
    checked: set[tuple[int, int]] = set()
    for label in ERROR_LEVEL_LABELS:
        ranges = list(suspects.get(label, ()))
        bound = matched.get(label)
        if bound is not None and bound not in ranges:
            ranges.insert(0, bound)
        for start, end in ranges:
            if (start, end) in checked:
                continue  # 同一条标题被两个别名命中时只查一遍
            checked.add((start, end))
            if bound is not None and (start, end) != bound:
                # 这一段只是标题**命中**了别名，不是最后绑定的那一段（比如 `## 需求变更由谁说了算`）。
                # 报出来时点名它自己的标题：否则用户跑到行号那一看是别的标题，会以为闸门指错了地方。
                shown = display_heading(lines[start]) or label.split(" ", 1)[1]
            else:
                shown = label.split(" ", 1)[1]
            _flag_vague(findings, shown, body_of(lines, start, end), start, "ERROR")

    for i, (title, _level, start, end, _numbered) in enumerate(sections):
        if (start, end) in checked:
            continue  # 这两段已由 E9 按 ERROR 查过，不要再报一遍 W9
        _flag_vague(findings, title, body_of(lines, start, end), start, "WARN")

    # ---- E10 占位符 --------------------------------------------------------
    for idx, line in enumerate(lines, start=1):
        for token in PLACEHOLDERS:
            if token in line:
                findings.append(Finding("ERROR", "E10", f"残留占位符 `{token}`", idx))
                break

    # ---- 其余警告 ----------------------------------------------------------
    # 行号不是装饰：`pf-ignore` 的作用域按「注释与被豁免的 WARN 在同一段」判，
    # 所以这几条也得说清自己在哪一段（拿段落标题行当锚点）。
    def _item_line(letter: str, num: int) -> int:
        for i, ln in enumerate(lines):
            if not fenced[i] and item_start(ln, letter) == num:
                return i + 1
        return 0

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
        text = "\n".join(block)
        line_no = _item_line("R", num)
        # 只看标记（`- **R9**`）之后的正文：标记里的编号是数字，会骗过下面的 NUMBER_RE。
        # 注释也剥掉：`<!-- pf-ignore: W2 … -->` 里的 `2` 同样会骗过它（见 strip_comments）。
        body = strip_comments(item_body(text))
        # 代码段（`openpyxl`）本身就是「具体对象」，所以有代码段时照样豁免。
        joined = CODE_SPAN_RE.sub("", body)
        if not NUMBER_RE.search(joined) and not PATH_RE.search(joined) and "`" not in body:
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
    clean = CODE_SPAN_RE.sub("", strip_comments(body))
    for idx, line in enumerate(clean.splitlines()):
        if line.strip().startswith("#"):
            continue  # 标题行是段落名本身，不算内容
        probe = _strip_cited(line)
        if not probe:
            continue  # 空行，或整行都是引文（`> [用户原话]「…」`）：引用原文里的词不算你的锅
        for word in VAGUE_WORDS:
            if word in probe:
                findings.append(
                    Finding(level, "E9" if level == "ERROR" else "W9",
                            f"「{label}」段出现模糊词「{word}」，需换算成数字/路径/判定方式", start + idx + 2)
                )
                break


def parse_ignores(lines: list[str], codes: tuple[str, ...] = IGNORE_CODES) -> tuple[list[tuple[int, str, str]], list[Finding]]:
    """收集 `<!-- pf-ignore: W2 为什么这条可以忽略 -->`。

    返回 `([(注释所在行的 0 基下标, 码, 理由)], [写法有问题的 Finding])`。
    写法问题一律 ERROR（E11）：豁免是「我知道这条 WARN 在说什么、为什么它不影响执行」的声明，
    不是关闸门的开关。所以缺码、缺理由、码不认识、或者想豁免 ERROR，都在这里被拦下。

    `codes` 是「本闸门能豁免的码」：闸门① 只认自己的 `W1…W9`；闸门③ 传 `W1…W9 + C1…C5`，
    这样 `<!-- pf-ignore: C2 … -->` 在闸门③ 里算数、在闸门① 里会老老实实报「不认识这个码」。
    """
    ignores: list[tuple[int, str, str]] = []
    problems: list[Finding] = []
    for idx, line in enumerate(lines):
        for m in IGNORE_RE.finditer(line):
            body = m.group(1).strip()
            hit = re.match(r"^([A-Za-z]\d+[a-z]?)\s*(.*)$", body)
            if not hit:
                problems.append(
                    Finding("ERROR", "E11",
                            "`pf-ignore` 没写码（写法：`<!-- pf-ignore: W2 为什么这条可以忽略 -->`）",
                            idx + 1)
                )
                continue
            code = hit.group(1).upper()
            reason = hit.group(2).strip()
            if code in ERROR_CODES:
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


def section_index(lines: list[str], sections: list[tuple[str, int, int, int, bool]]) -> dict[int, str]:
    """行号（0 基）-> 段落名。豁免注释的作用域判据：注释与被豁免的 WARN 得在同一段。"""
    where: dict[int, str] = {}
    for title, _level, start, end, _numbered in sections:
        for idx in range(max(start, 0), min(end, len(lines))):
            where.setdefault(idx, title)
    return where


def apply_ignores(
    findings: list[Finding],
    ignores: list[tuple[int, str, str]],
    where: dict[int, str],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """按注释豁免 WARN，返回 `(留下的 findings, [(被豁免的 finding, 理由)])`。

    作用域：注释与它要豁免的 WARN 在**同一段**里才算数（把 `pf-ignore: W2` 写在别处，
    下一个人看到那条 WARN 时根本不知道有人处理过）。不带行号的检查（W1 全文长度）是文件级，
    写在哪儿都算 —— 它说的就是整份文件。
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
                if here is not None and here == where.get(idx):
                    reason = why
                    break
        if reason is None:
            kept.append(f)
        else:
            dropped.append((f, reason))
    return kept, dropped


def report(path: Path, use_ignore: bool = True) -> int:
    suppressed: list[tuple[Finding, str]] = []
    findings, size = check_file(path, use_ignore=use_ignore, suppressed=suppressed)
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
        ("数字开头的段名 4K（段内也有条目）",
         lambda: sub(SELFTEST_FIXTURE, "## 4. 需求（R#）",
                     "## 4K 需求素材说明\n\n- **R9** 素材必须真 4K，不缩放。判定：人工 看素材尺寸。\n\n"
                     "## 4. 需求（R#）"),
         {"E9", "E6"}),
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
