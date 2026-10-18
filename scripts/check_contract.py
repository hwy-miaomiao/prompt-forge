#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的机器闸门③：跨段一致性。

三道闸门各管一段距离：
  ① scripts/check_prompt.py   长提示词**自己**合不合规（12 段齐不齐、编号、判定三态、模糊词、占位符）
  ② scripts/check_skill.py    技能定义**自己**合不合规（name / description / 边界 / 薄包装依赖）
  ③ 本脚本                   段与段**之间**对不对得上（见下面的码表）

为什么要有第三道：前两道都是「一段一段单独看」，于是下面这种文档能一路绿灯 ——
第 4 段写了 R1~R5，第 9 段只做了 R1~R3（另外两条没人干）；第 10 段写了三条「未确认」，
第 11 段写「无」（用户答不上来时不知道该不该继续）；第 7 段写着 `判定：命令` 却没给命令
（验收人只能自己猜怎么跑）。这些都不是「某一段写错了」，而是「两段之间对不上」。

码表（口径见 references/校验清单.md）：
  C1 ERROR  引用了不存在的编号（`对应 R9` 而第 4 段里没有 R9）
  C2 WARN   某条需求没被第 9 段任何步骤触及（第 9 段一条 R# 都没声明时合并成一条）
  C3 ERROR  第 10 段有「未确认」，第 11 段却空着或只写「无」
  C4 WARN   那条「未确认」在第 11 段找不到对应（两边没有一个共同关键词）
  C5 WARN   `判定：命令` 却没给出可直接粘贴的命令

实现口径（四条，写下来免得以后自己猜）：
  * **解析器只有一份**：全部 import 闸门①（`check_prompt`），不复制正则与拆段逻辑。
    两份解析器迟早会漂，然后同一个文件被两个闸门报出不同的行号。
  * **认段按段号优先**（`## 9. 执行步骤`），没有段号时退回别名匹配。闸门① 那套
    「抢绑 / 同义标题歧义」的算法不在这里重复：歧义由闸门① 的 W8 报，两边都报只会让
    用户看到两条一样的提示。
  * **C4 是关键词重叠启发式**（中文二元组 + 英文词），所以只报 WARN：它说的是
    「这条未确认可能没有落脚点」，不是判决。看不出重叠才报，看得出就闭嘴。
  * 行号口径与闸门① 一致（`check_prompt.split_lines()`：只按 `\n` 切，U+2028 不当换行，
    所以两个闸门对同一个文件必须报同一个行号，审计 P35）。
  * 用法错误（路径是目录 / 文件不存在）沿用闸门① 的写法：直接 `ERROR: …`，不带码 ——
    那不是在评文档，是在说命令行写错了。

用法：
    python scripts/check_contract.py <输出目录>/prompt.md [<prompt2.md> ...]
    python scripts/check_contract.py --selftest
    python scripts/check_contract.py --no-ignore <prompt.md>

退出码：有 ERROR 时非零退出。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_prompt as cp  # noqa: E402  （同目录，故意复用闸门① 的解析器）

#: 本闸门自己的码（C1/C3 是 ERROR，其余是 WARN）。
C_CODES = ("C1", "C2", "C3", "C4", "C5")
#: 本闸门的码里哪些是 ERROR：`pf-ignore` 不能豁免它们（写进注释会报 E11，审计 P32）。
C_ERROR_CODES = ("C1", "C3")
#: 能被 `pf-ignore` 豁免的 C 码（WARN）。
C_WARN_CODES = ("C2", "C4", "C5")
#: 纸张规格后缀：`A4 纸` / `A3 幅面` / `A0/A1 幅面` / `A2 出图` 里的编号不是验收编号（审计 P25）。
_PAPER_SPEC_TAIL_RE = re.compile(r"^\s*(?:的\s*)?(?:/\s*[A-Za-z]?\d+\s*)*(?:纸|图纸|幅面|幅|开本|出)")

LABEL_REQ = "4 需求（R#）"
LABEL_ACC = "7 验收标准（A#）"

#: 「判定：命令」时必须看得出跑什么：反引号里的命令、脚本路径，或者一个命令名。
COMMAND_HINT_RE = re.compile(
    r"(?<![\w.-])(?:python|py|node|npm|pnpm|npx|git|bash|zsh|sh|pwsh|powershell|make|cargo|go|dotnet|"
    r"java|pytest|ruff|mypy|deno|bun|curl|sqlite3)\b",
    re.IGNORECASE,
)
#: 「判定：命令」里认的可执行文件 / 脚本路径；数据文件（.csv/.json/.xlsx）不算命令（审计 P33）。
SCRIPT_RE = re.compile(
    r"[\w./\\-]+\.(?:py|sh|ps1|bat|cmd|js|mjs|cjs|ts|exe)\b",
    re.IGNORECASE,
)

#: C4 比对时要丢掉的词：这些词在「停下来问人」和「未确认」两边本来就会出现，不算对应关系。
STOP_WORDS = {
    "未确", "确认", "这个", "那个", "什么", "怎么", "如果", "一旦", "出现", "需要", "可以", "就是",
    "还是", "以及", "或者", "因为", "所以", "目前", "现在", "默认", "假设", "问题", "情况", "时候",
    "可能", "应该", "不能", "不会", "没有", "一条", "两条", "同时", "而且", "然后", "下来", "停下",
    "问人", "继续", "再继", "一条",
}
ASCII_STOP = {
    "the", "and", "for", "with", "this", "that", "not", "you", "are", "when", "then", "than",
    "http", "https", "www", "from", "into", "only", "must", "can", "should",
}


def find_section(sections: list[tuple[str, int, int, int, bool]], number: int) -> tuple[int, int] | None:
    """第 `number` 段在哪：标题里带这个段号的优先，没有段号时退回别名匹配。"""
    aliases = next((al for label, al in cp.REQUIRED_SECTIONS if label.startswith(f"{number} ")), None)
    if aliases is None:
        return None
    for title, _level, start, end, _numbered in sections:
        if cp.heading_number(title) == number and cp.alias_hit(title, aliases):
            return start, end
    for title, _level, start, end, _numbered in sections:
        if cp.alias_hit(title, aliases):
            return start, end
    return None


def item_line(lines: list[str], fenced: list[bool], letter: str) -> dict[int, int]:
    """编号 -> 定义它的那一行（1 基）。报错要指向条目自己那一行，不是段标题。"""
    out: dict[int, int] = {}
    for idx, line in enumerate(lines):
        if fenced[idx]:
            continue
        num = cp.item_start(line, letter)
        if num is not None:
            out.setdefault(num, idx + 1)
    return out


def _definition_end(line: str, letter: str) -> int:
    """`- **R3** …` 里那个**定义**标记的结束位置；这一行不是 `R#` 开头的条目就返回 0。

    定义不等于引用：`- **R3** 入口是 scripts/borrow.py` 说的是「R3 在这」，不要求 R3 先存在。
    但同一行里**后面**出现的编号仍然是引用（`- **R5** 复用 R2 的做法` 要求 R2 存在）。
    """
    stripped = line.strip()
    probe = cp.ITEM_STRIP_RE.sub("", stripped).lstrip(">*`").lstrip()
    match = re.match(rf"{letter}(\d+)(?!\d)", probe)
    if not match:
        return 0
    return (len(line) - len(stripped)) + (len(stripped) - len(probe)) + match.end()


def check_refs(lines: list[str], fenced: list[bool], known: dict[str, set[int]]) -> list[cp.Finding]:
    """C1：全文里出现的编号引用，必须在对应段落里真的定义过。"""
    findings: list[cp.Finding] = []
    for idx, line in enumerate(lines):
        if fenced[idx]:
            continue
        clean = cp.strip_comments(line)
        if not clean.strip():
            continue
        # 行内代码里的是示例，不是引用（审计 P25）：等长替换成空格，偏移量不变。
        clean = cp.CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), clean)
        for letter, label in (("R", LABEL_REQ), ("A", LABEL_ACC)):
            marker_end = _definition_end(clean, letter)
            for match in re.finditer(rf"(?<![A-Za-z0-9_]){letter}(\d+)(?!\d)", clean):
                if match.start() < marker_end:
                    continue  # 这一处是条目自己的编号，不是引用
                if _PAPER_SPEC_TAIL_RE.match(clean[match.end():]):
                    continue  # `A4 纸` / `A0/A1 幅面` / `A2 出图` 是纸张规格，不是验收编号（审计 P25）
                num = int(match.group(1))
                if num not in known[letter]:
                    findings.append(cp.Finding(
                        "ERROR", "C1",
                        f"引用了不存在的 {letter}{num}（第 {label} 段里没有这条编号）——"
                        "执行的人会卡在这里猜，编号要么补上、要么改掉",
                        idx + 1,
                    ))
    return findings


def keywords(text: str) -> set[str]:
    """比对「两边是不是在说同一件事」用的词：中文二元组 + 英文单词，去掉两边都会出现的口水词。"""
    words: set[str] = set()
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_.\-]{2,}", text):
        word = match.group(0).lower()
        if word not in ASCII_STOP:
            words.add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(run) - 1):
            words.add(run[i:i + 2])
    return words - STOP_WORDS


def check_step_coverage(
    lines: list[str], fenced: list[bool], sections: list[tuple[str, int, int, int, bool]], requirements: set[int]
) -> list[cp.Finding]:
    """C2：每条需求都要在第 9 段里有人干（`（对应 R#）`）。

    报错行指向**第 9 段的标题**而不是那条需求：要改的地方是步骤，不是需求本身
    （豁免注释因此也写在第 9 段里 —— 作用域判据见 `apply_ignores`）。
    """
    if not requirements:
        return []
    sec = find_section(sections, 9)
    if sec is None:
        return []  # 第 9 段整个没有是闸门① 的 E1，不在这里重复报
    start, end = sec
    seen: set[int] = set()
    for idx in range(start + 1, min(end, len(lines))):
        if fenced[idx]:
            continue
        seen |= {int(m) for m in re.findall(r"R(\d+)(?!\d)", cp.strip_comments(lines[idx]))}
    seen &= requirements
    if not seen:
        return [cp.Finding(
            "WARN", "C2",
            "第 9 段没有声明任何「（对应 R#）」，「每条需求都有人干」这件事无法检查 ——"
            "给步骤补上标注（例：`1. 读表 → 产物 out/borrow.csv（对应 R1）`）",
            start + 1,
        )]
    return [cp.Finding(
        "WARN", "C2",
        f"需求 R{num} 没被第 9 段任何步骤触及：要么补一步，"
        "要么把它挪进第 5 段非目标说清这版不做",
        start + 1,
    ) for num in sorted(requirements - seen)]


#: 「否定句」里的未确认不算未确认项：`无未确认项`、`没有未确认的地方`（审计 P26）。
_DENY_UNCONFIRMED_RE = re.compile(
    r"(?:无|没有|暂无|并未|不存在|无需|不需|不要|不必)[^。；\n]{0,6}未确认"
)


def _mentions_unconfirmed(text: str) -> bool:
    """这一行是不是真的在说「有未确认的东西」——否定句不算（审计 P26）。"""
    return "未确认" in _DENY_UNCONFIRMED_RE.sub("", text)


def _assumption_block(lines: list[str], idx: int, lo: int, hi: int) -> str:
    """取「未确认」所在的那一整块：上下连续的非空行，遇到标题/空行就停。

    多行假设、或把状态写在续行时，只比对含「未确认」那一行会漏掉关键词（审计 P36）。
    """
    start = idx
    while start > lo and lines[start - 1].strip() and not cp.HEADING_RE.match(lines[start - 1]):
        start -= 1
    end = idx + 1
    while end < hi and lines[end].strip() and not cp.HEADING_RE.match(lines[end]):
        end += 1
    return "\n".join(lines[start:end])


def check_open_items(
    lines: list[str], fenced: list[bool], sections: list[tuple[str, int, int, int, bool]]
) -> list[cp.Finding]:
    """C3/C4：第 10 段每条「未确认」都要在第 11 段有落脚点。"""
    sec10 = find_section(sections, 10)
    if sec10 is None:
        return []
    pending: list[tuple[int, str]] = []
    for idx in range(sec10[0] + 1, min(sec10[1], len(lines))):
        if fenced[idx]:
            continue
        clean = cp.strip_comments(lines[idx]).strip()
        if _mentions_unconfirmed(clean):
            pending.append((idx + 1, clean))
    if not pending:
        return []

    sec11 = find_section(sections, 11)
    body11 = ""
    if sec11 is not None:
        body11 = "\n".join(
            cp.strip_comments(lines[i]) for i in range(sec11[0] + 1, min(sec11[1], len(lines))) if not fenced[i]
        )
    if sec11 is None or cp.is_empty_claim(body11):
        return [cp.Finding(
            "ERROR", "C3",
            f"第 10 段有 {len(pending)} 条「未确认」，第 11 段却空着或只写「无」："
            "用户答不上来时没人知道该不该继续 —— 每条未确认都要在第 11 段有一条对应的停下条件",
            pending[0][0],
        )]

    known = keywords(body11)
    findings: list[cp.Finding] = []
    for line_no, text in pending:
        block = _assumption_block(lines, line_no - 1, sec10[0] + 1, min(sec10[1], len(lines)))
        if keywords(block) & known:
            continue
        snippet = re.sub(r"\s+", " ", text)[:60]
        findings.append(cp.Finding(
            "WARN", "C4",
            f"这条「未确认」在第 11 段找不到对应（两边没有一个共同的关键词）：{snippet}",
            line_no,
        ))
    return findings


def has_command(text: str) -> bool:
    """条目里有没有能直接粘贴的命令：反引号里的命令、脚本路径，或者一个命令名。"""
    return bool(cp.CODE_SPAN_RE.search(text) or SCRIPT_RE.search(text) or COMMAND_HINT_RE.search(text))


def check_judgements(
    lines: list[str], fenced: list[bool], acceptances: dict[int, list[str]]
) -> list[cp.Finding]:
    """C5：`判定：命令` 得真的给出命令，否则验收人自己猜。"""
    lines_of = item_line(lines, fenced, "A")
    findings: list[cp.Finding] = []
    for num in sorted(acceptances):
        text = cp.strip_comments("\n".join(acceptances[num]))
        if cp.judge_kind(text) != "命令" or has_command(text):
            continue
        findings.append(cp.Finding(
            "WARN", "C5",
            f"验收 A{num} 写着「判定：命令」，条目里却没有一条能直接粘贴的命令"
            "（反引号里的命令 / 脚本路径 / 命令名）—— 验收人只能自己猜怎么跑",
            lines_of.get(num, 0),
        ))
    return findings


def check_contract(
    path: Path, use_ignore: bool = True, suppressed: list[tuple[cp.Finding, str]] | None = None
) -> tuple[list[cp.Finding], int, str]:
    """返回 `(findings, 字符数, 说明)`。

    第三个值是「这份文件里本闸门能检查什么」的说明：没有 `R#` / `A#` 条目、也没有第 9 段时
    要说清「跨段一致性没有可检查的对象」，否则一个光秃秃的「0 ERROR, 0 WARN —— 通过」
    会被当成「这份文件跨段一致性没问题」（段落齐不齐是闸门① 的事）。
    """
    raw = cp.read_text(path)
    # 两套行视图，行号一一对应（见 check_prompt.check_file 里同样的分法）：
    #   text_lines —— 原文：pf-ignore 注释本身在这一版；
    #   lines      —— 正文：跨段一致性只看正文，注释里的标题 / R# / A# 不算数（审计 F1）。
    text_lines = cp.split_lines(raw)
    lines = cp.split_lines(cp.strip_comments(raw))
    fenced = cp.fence_lines(lines)
    sections, _preamble = cp.split_sections(lines)
    requirements, _dups_r = cp.collect_items(lines, "R")
    acceptances, _dups_a = cp.collect_items(lines, "A")
    known = {"R": set(requirements), "A": set(acceptances)}

    findings: list[cp.Finding] = []
    findings.extend(check_refs(lines, fenced, known))
    findings.extend(check_step_coverage(lines, fenced, sections, known["R"]))
    findings.extend(check_open_items(lines, fenced, sections))
    findings.extend(check_judgements(lines, fenced, acceptances))

    # 豁免注释无论 --no-ignore 都要解析：写错的豁免（给 ERROR 码、码不认识、理由太短）
    # 是文档缺陷，--no-ignore 也不能把它吞掉（审计 P31/P32）。
    # 豁免码表 = 闸门① 的 W 码 + 本闸门的 WARN 码；ERROR 码（E*/C1/C3）写进注释会报 E11。
    ignores, problems = cp.parse_ignores(
        text_lines,
        codes=cp.IGNORE_CODES + C_WARN_CODES,
        error_codes=cp.ERROR_CODES + C_ERROR_CODES,
    )
    findings.extend(problems)
    if use_ignore and ignores:
        findings, dropped = cp.apply_ignores(findings, ignores, cp.section_index(lines, sections))
        if suppressed is not None:
            suppressed.extend(dropped)

    note = ""
    if find_section(sections, 9) is None:
        if not requirements and not acceptances:
            if not findings:
                note = ("这份文件里没有 R# / A# 条目，也没有可认的第 9 段 —— 跨段一致性没有可检查的对象"
                        "（段落齐不齐是闸门① 的事：python scripts/check_prompt.py <文件>）")
        else:
            # 有 R# 却没第 9 段：C2 静默跳过会让人以为「覆盖关系查过了」，必须说清它没查。
            note = ("这份文件没有可认的第 9 段，C2「每条需求都被步骤触及」无从检查"
                    "（段落缺失本身由闸门① 报 E1：python scripts/check_prompt.py <文件>）")
    return findings, len(raw), note


def report(path: Path, use_ignore: bool = True) -> int:
    suppressed: list[tuple[cp.Finding, str]] = []
    try:
        findings, size, note = check_contract(path, use_ignore=use_ignore, suppressed=suppressed)
    except cp.FileReadError as exc:
        # 与闸门① 同一口径：读不了文件打印一行 ERROR，退出码 1，不抛 traceback（审计 F2）。
        print(f"\n=== 检查 {path} ===")
        print(f"ERROR: {exc}")
        print("结果：1 ERROR, 0 WARN —— 未通过")
        return 1
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]
    print(f"\n=== 检查 {path} （{size} 字符） ===")
    for finding in sorted(findings, key=lambda x: (x.level != "ERROR", x.line)):
        print(finding.render())
    for finding, why in suppressed:
        print(f"豁免: {finding.render()} —— 理由：{why}")
    if note:
        print(f"提示：{note}")
    if errors:
        print(f"结果：{len(errors)} ERROR, {len(warns)} WARN —— 未通过")
        return 1
    tail = "（WARN 需逐条说明为什么不影响执行）" if warns else ""
    if suppressed:
        tail = f"（另有 {len(suppressed)} 条 WARN 按 `pf-ignore` 注释豁免，`--no-ignore` 可看全）"
    print(f"结果：0 ERROR, {len(warns)} WARN —— 通过" + tail)
    return 0


def selftest() -> int:
    """自测：拿闸门① 的自测夹具当基准件 —— 它本来就是一份合规的长提示词，本闸门只负责把它改坏。

    每个 case 固定「期望报出的码集合」。期望值都是先用真实文件跑一遍确认过的，
    不要照着直觉写：C2 的一级判据、C3/C4 的分工、C5 的命令识别都在这里被钉住。
    """
    base = cp.SELFTEST_FIXTURE
    step1 = "1. 读表并写出 out/borrow.csv（对应 R1）"
    step2 = "2. 末尾统一跳过空行（对应 R2）"
    step3 = "3. 入口收在 scripts/borrow.py，退出码固定 0/1（对应 R3）"
    section11 = ("- 工作表数量超过 1 个时，停下来问人再继续。\n"
                 "- 需要新增第三方依赖时，停下来问人。\n"
                 "- 表头不在第 1 行时，停下来问人再确认列名。")

    def sub(text: str, old: str, new: str) -> str:
        """替换前先断言目标真的在文本里：夹具漂移时 `str.replace` 会静默返回原文，
        用例就变成「空跑也 PASS」，`--selftest` 照样打印「全部通过」。"""
        if old not in text:
            raise AssertionError(f"替换目标不存在（夹具漂移了？）：{old!r}")
        return text.replace(old, new, 1)

    def no_step_refs() -> str:
        text = sub(base, step1, step1.replace("（对应 R1）", ""))
        text = sub(text, step2, step2.replace("（对应 R2）", ""))
        return sub(text, step3, step3.replace("（对应 R3）", ""))

    def c2_uncovered() -> str:
        """把 R2 的步骤标注改成 R1：R2 就没人干了。"""
        return sub(base, step2, step2.replace("（对应 R2）", "（对应 R1）"))

    def c2_exempt() -> str:
        """R2 没人干 + 第 9 段里挂一条豁免注释（注释与被豁免的 WARN 必须同段）。"""
        text = sub(base, step2, step2.replace("（对应 R2）", "（对应 R1）"))
        return sub(text, step3, step3 + "\n<!-- pf-ignore: C2 这条需求这版先不做 -->")

    cases = [
        ("基准件：闸门① 的自测夹具跨段也对得上", lambda: base, set(), set(), set(), ""),
        ("C1 引用了不存在的需求号",
         lambda: sub(base, "（对应 R3）** 入口与退出码符合要求",
                     "（对应 R9、R3）** 入口与退出码符合要求"),
         {"C1"}, set(), set(), ""),
        ("C1 第 9 段引用的编号不存在",
         lambda: sub(base, step2, step2.replace("（对应 R2）", "（对应 R2、R8）")),
         {"C1"}, set(), set(), ""),
        ("C2 某条需求没被第 9 段触及", c2_uncovered, set(), {"C2"}, set(), ""),
        ("C2 第 9 段一条标注都没有（合并成一条）",
         no_step_refs, set(), {"C2"}, set(), "没有声明任何"),
        ("C3 第 11 段写「无」", lambda: sub(base, section11, "无"), {"C3"}, set(), set(), ""),
        ("C4 第 11 段跟未确认对不上",
         lambda: sub(base, section11, "- 结果文件和预期差得太多时，停下来问人。"),
         set(), {"C4"}, set(), ""),
        ("C5 判定：命令 却没给命令",
         lambda: sub(base, "判定：人工 看 main 分支。", "判定：命令 看 main 分支。"),
         set(), {"C5"}, set(), ""),
        ("C5 给了命令就不报",
         lambda: sub(base, "判定：人工 看 main 分支。", "判定：命令 `python scripts/borrow.py`。"),
         set(), set(), set(), ""),
        # —— 以下用例来自第 4 轮审计 P25/P26/P30/P32/P33/P36，每条都先跑真实夹具确认过 ——
        ("C1 不把「A4 纸」当验收编号（行内代码里的编号也不算）",
         lambda: sub(base, "## 4. 需求（R#）",
                     "## 4. 需求（R#）\n\n- 打印名单按 A4 纸纵向排版，页面规格写 `A4`。\n"
                     "- 交付 A0/A1 幅面图纸，以及图纸按 A2 出图。"),
         set(), set(), set(), ""),
        ("C3/C4「无未确认项」是否定句，不算未确认（第 11 段写「无」也不报 C3）",
         lambda: sub(sub(base,
                         "- 假设表头在第 1 行（未确认：若表头在第 2 行要改 header 参数）。",
                         "- 无未确认项：全部假设已在阶段 2 与用户拍板确认。"),
                     section11, "无。"),
         set(), set(), set(), ""),
        ("C4 多行假设按整块取关键词（未确认写在续行）",
         lambda: sub(base,
                     "- 假设表头在第 1 行（未确认：若表头在第 2 行要改 header 参数）。",
                     "- 假设 A1：表头在第 1 行，直接按表头解析。\n"
                     "- 状态：未确认（默认取第 1 行；改起来要改 header 参数）。"),
         set(), set(), set(), ""),
        ("C5 只有数据文件路径（.csv）不算命令",
         lambda: sub(base, "判定：命令 `python scripts/borrow.py`。",
                     "判定：命令 用 Excel 打开 out/borrow.csv 数一遍列。"),
         set(), {"C5"}, set(), ""),
        ("C5 判定：人工 里出现「命令」两个字不算判定：命令",
         lambda: sub(base, "判定：人工 看 main 分支。",
                     "判定：人工 由产品经理看截图确认，这条不用跑命令。"),
         set(), set(), set(), ""),
        ("豁免注释：给 ERROR 码（C1）写 pf-ignore 报 E11",
         lambda: sub(base, "## 5. 非目标",
                     "## 5. 非目标\n\n<!-- pf-ignore: C1 这条引用先不管 -->"),
         {"E11"}, set(), set(), ""),
        ("豁免注释：同段的 C2 被吃掉", c2_exempt, set(), set(), {"C2"}, ""),
    ]

    failed = 0
    print(f"闸门③ 自测：{len(cases)} 个用例 + 2 个 --no-ignore 用例 + 1 个行号用例 + 2 个范围说明用例")
    with tempfile.TemporaryDirectory() as tmp:
        for idx, (name, build, want_errors, want_warns, want_ignored, want_msg) in enumerate(cases):
            try:
                text = build()
            except AssertionError as exc:
                print(f"FAIL  闸门③ {name}: {exc}")
                failed += 1
                continue
            path = Path(tmp) / f"case{idx}.prompt.md"
            path.write_text(text, encoding="utf-8")
            suppressed: list[tuple[cp.Finding, str]] = []
            findings, _size, _note = check_contract(path, suppressed=suppressed)
            got_errors = {f.code for f in findings if f.level == "ERROR"}
            got_warns = {f.code for f in findings if f.level == "WARN"}
            got_ignored = {f.code for f, _why in suppressed}
            msgs = " ".join(f.message for f in findings)
            ok = (got_errors == want_errors and got_warns == want_warns
                  and got_ignored == want_ignored and (not want_msg or want_msg in msgs))
            detail = (f"ERROR 码 {sorted(got_errors)}（期望 {sorted(want_errors)}） "
                      f"WARN 码 {sorted(got_warns)}（期望 {sorted(want_warns)}）")
            if want_ignored:
                detail += f" 豁免 {sorted(got_ignored)}（期望 {sorted(want_ignored)}）"
            if want_msg:
                detail += f" 文案须含「{want_msg}」"
            print(f"{'PASS' if ok else 'FAIL'}  [闸门③] {name}: {detail}")
            if not ok:
                failed += 1
                for finding in findings:
                    print("      " + finding.render())

        # `--no-ignore` 必须让豁免失效：看不见的 WARN 没法在评审时复查。
        path = Path(tmp) / "no-ignore.prompt.md"
        path.write_text(c2_exempt(), encoding="utf-8")
        findings, _size, _note = check_contract(path, use_ignore=False)
        got_warns = {f.code for f in findings if f.level == "WARN"}
        ok = got_warns == {"C2"}
        print(f"{'PASS' if ok else 'FAIL'}  [闸门③] --no-ignore 时豁免注释失效: "
              f"WARN 码 {sorted(got_warns)}（期望 ['C2']）")
        if not ok:
            failed += 1
            for finding in findings:
                print("      " + finding.render())

        # `--no-ignore` 也不能把写错的豁免吞掉（审计 P31/P32）：E11 与真 WARN 都要露出来。
        path = Path(tmp) / "no-ignore-e11.prompt.md"
        path.write_text(
            sub(c2_exempt(), "## 5. 非目标", "## 5. 非目标\n\n<!-- pf-ignore: C1 想盖掉一个 ERROR 码 -->"),
            encoding="utf-8",
        )
        findings, _size, _note = check_contract(path, use_ignore=False)
        got_codes = {f.code for f in findings}
        ok = {"C2", "E11"} <= got_codes
        print(f"{'PASS' if ok else 'FAIL'}  [闸门③] --no-ignore 时写错的豁免照样报 E11: "
              f"码 {sorted(got_codes)}（期望含 ['C2', 'E11']）")
        if not ok:
            failed += 1
            for finding in findings:
                print("      " + finding.render())

        # U+2028 不是换行：行号必须按真实 `\n` 计，不能漂（审计 P35）。
        noisy = sub(base, "（对应 R3）** 入口与退出码符合要求",
                    "（对应 R9、R3）** 入口与退出码符合要求")
        noisy = sub(noisy, "## 2. 背景与已知事实", "## 2. 背景与已知事实\n\n第一行\u2028第二行")
        want_line = next(
            i for i, line in enumerate(noisy.split("\n"), 1) if "R9、R3" in line
        )
        path = Path(tmp) / "u2028.prompt.md"
        path.write_text(noisy, encoding="utf-8")
        findings, _size, _note = check_contract(path)
        c1_findings = [f for f in findings if f.code == "C1"]
        ok = bool(c1_findings) and all(f.line == want_line for f in c1_findings)
        got_lines = sorted({f.line for f in c1_findings})
        print(f"{'PASS' if ok else 'FAIL'}  [闸门③] U+2028 不算换行：C1 行号 {got_lines}"
              f"（期望 [{want_line}]）")
        if not ok:
            failed += 1
            for finding in findings:
                print("      " + finding.render())

        # 没有可检查对象的文件要明说自己没查什么：否则一个光秃秃的「通过」会被当成
        # 「这份文件跨段一致性没问题」，而它其实连 R# / 第 9 段都没有。
        scope_cases = [
            ("没东西可查时要出范围说明",
             "# 随便写点什么\n\n今天天气不错，先这样。\n",
             "没有可检查的对象"),
            ("有需求但没有第 9 段要说清 C2 没查",
             "# 帮我做个东西\n\n## 需求\n\n- R1 能报名\n",
             "无从检查"),
        ]
        for name, text, want in scope_cases:
            path = Path(tmp) / "scope.prompt.md"
            path.write_text(text, encoding="utf-8")
            findings, _size, note = check_contract(path)
            ok = not findings and want in note
            print(f"{'PASS' if ok else 'FAIL'}  [闸门③] {name}: 说明 = {note or '（没有说明）'}")
            if not ok:
                failed += 1
                for finding in findings:
                    print("      " + finding.render())

    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查长提示词的段落之间对不对得上（跨段一致性）")
    parser.add_argument("files", nargs="*", help="要检查的 prompt.md 路径")
    parser.add_argument("--selftest", action="store_true", help="用闸门① 的自测夹具跑一遍本闸门")
    parser.add_argument("--no-ignore", action="store_true",
                        help="忽略 `<!-- pf-ignore: C码 理由 -->` 豁免注释，把所有 WARN 都报出来")
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
