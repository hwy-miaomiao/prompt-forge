#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的唯一机器闸门：检查长提示词是否符合 12 段契约。

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


def normalize_heading(raw: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,4}\s*", "", raw).strip()
    text = strip_emphasis(text)
    text = re.sub(r"^[\d]+[\s\.、\)）:：]*", "", text)  # 去掉 "5. " / "5、" 这类编号
    return text.replace(" ", "").replace("\u3000", "").replace("：", "").replace(":", "")


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
    """段落是否为空或只写了「无」（容忍「无。」「（无）」这类标点/括号写法）。"""
    cleaned = re.sub(r"[\s。，,、；;：:！!？?\-—~～*•·]+", "", strip_emphasis(text))
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


def split_sections(lines: list[str]) -> tuple[list[tuple[str, int, int]], list[str]]:
    """返回 ([(归一化标题, 起始行号, 结束行号)], 无标题前的前言行)。"""
    headings: list[tuple[str, int]] = []
    fenced = fence_lines(lines)
    for idx, line in enumerate(lines):
        if fenced[idx]:
            continue
        if HEADING_RE.match(line):
            headings.append((normalize_heading(line), idx))
    sections: list[tuple[str, int, int]] = []
    for i, (title, start) in enumerate(headings):
        end = headings[i + 1][1] if i + 1 < len(headings) else len(lines)
        sections.append((title, start, end))
    preamble = lines[: headings[0][1]] if headings else lines
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


def check_file(path: Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    raw = read_text(path)
    lines = raw.splitlines()

    sections, _preamble = split_sections(lines)
    if not sections:
        findings.append(Finding("ERROR", "E0", "找不到任何 Markdown 标题；长提示词必须按 12 段分节"))
        return findings, len(raw)

    # ---- E1 必需段落 -------------------------------------------------------
    used: set[int] = set()
    matched: dict[str, tuple[int, int]] = {}
    for label, aliases in REQUIRED_SECTIONS:
        hit = None
        for i, (title, start, end) in enumerate(sections):
            if i in used:
                continue
            if label.startswith("4 ") and ("非目标" in title or "验收" in title):
                continue
            if alias_hit(title, aliases):
                hit = i
                break
        if hit is None:
            findings.append(Finding("ERROR", "E1", f"缺少必需段落：{label}"))
        else:
            used.add(hit)
            matched[label] = (sections[hit][1], sections[hit][2])

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
        refs = {int(m) for m in re.findall(r"R(\d+)(?!\d)", text)}
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
    for label in ("4 需求（R#）", "7 验收标准（A#）"):
        if label in matched:
            start, end = matched[label]
            checked.add((start, end))
            _flag_vague(findings, label.split(" ", 1)[1], body_of(lines, start, end), start, "ERROR")

    for i, (title, start, end) in enumerate(sections):
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
    if len(raw) < 600:
        findings.append(Finding("WARN", "W1", f"全文只有 {len(raw)} 字符，信息量可能不足以让陌生 AI 开工"))
    if len(acceptances) > 25:
        findings.append(Finding("WARN", "W4", f"验收条目 {len(acceptances)} 条偏多，考虑合并"))
    if len(requirements) >= 15:
        findings.append(Finding("WARN", "W5", f"需求条目 {len(requirements)} 条偏多，考虑分层（必须有 / 可选）"))

    for num, block in sorted(requirements.items()):
        text = "\n".join(block)
        # 只看标记（`- **R9**`）之后的正文：标记里的编号是数字，会骗过下面的 NUMBER_RE。
        body = item_body(text)
        # 代码段（`openpyxl`）本身就是「具体对象」，所以有代码段时照样豁免。
        joined = CODE_SPAN_RE.sub("", body)
        if not NUMBER_RE.search(joined) and not PATH_RE.search(joined) and "`" not in body:
            findings.append(
                Finding(
                    "WARN",
                    "W2",
                    f"需求 R{num} 没有任何数字/路径/具体对象，可能不可验证"
                    "（量化写在配对的 A# 里时可以忽略本条）",
                )
            )
        # 只抓「A，并且 B」这种并列句式；「三类：…、以及 …」是列举，「42 人同时提交」是场景，都不算。
        for word in ("并且", "以及", "同时"):
            if re.search(r"[，,]\s*" + word, joined):
                findings.append(Finding("WARN", "W7", f"需求 R{num} 里出现「，{word}」，可能是一句多需求，考虑拆成两条"))
                break

    assume_label = "10 假设与开放问题"
    if assume_label in matched:
        start, end = matched[assume_label]
        if is_empty_claim(body_of(lines, start, end)):
            findings.append(Finding("WARN", "W3", "假设段为空；一条假设都没有，通常说明替用户拍板了"))

    return findings, len(raw)


def _flag_vague(findings: list[Finding], label: str, body: str, start: int, level: str) -> None:
    clean = CODE_SPAN_RE.sub("", body)
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


def report(path: Path) -> int:
    findings, size = check_file(path)
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]
    print(f"\n=== 检查 {path} （{size} 字符） ===")
    for f in sorted(findings, key=lambda x: (x.level != "ERROR", x.line)):
        print(f.render())
    if errors:
        print(f"结果：{len(errors)} ERROR, {len(warns)} WARN —— 未通过")
    else:
        print(f"结果：0 ERROR, {len(warns)} WARN —— 通过" + ("（WARN 需逐条说明为什么不影响执行）" if warns else ""))
    return 1 if errors else 0


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
    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查长提示词是否符合 prompt-forge 的 12 段契约")
    parser.add_argument("files", nargs="*", help="要检查的 prompt.md 路径")
    parser.add_argument("--selftest", action="store_true", help="用 examples/ 下的正反例自测")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.files:
        parser.print_help()
        return 2

    code = 0
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"ERROR: 文件不存在：{path}")
            code = 1
            continue
        code |= report(path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
