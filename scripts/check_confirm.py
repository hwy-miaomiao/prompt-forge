#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的机器闸门④：检查需求确认.md 的机器可读字段。

前三道闸门管「写给 AI 的长提示词」；这一道管「问询过程本身」：
问询是不是真的发生过、三态判定有没有证据、每道题的答案与方法来源有没有落盘、
「未确认」有没有写进假设段、kill 是不是一句「不做了」就带过。

为什么需要它：需求确认.md 是**给人看的过程记录**，不落进 prompt.md 的信息全靠它保存。
但它一直是纯散文，写得再空也「看着像回事」。本闸门只校验有机器可读锚点的那些字段，
不评价文字好坏（那是人工复核的事）。

码表（口径见 references/校验清单.md）：
  D0 ERROR  找不到任何 Markdown 标题，无法定位字段
  D1 ERROR  缺少「问询模式」，或取值不是「菜单式 / 一次一问」
  D2 ERROR  缺少「分诊结论」，或不是 go / needs-clarification / kill
  D3 ERROR  缺少「一句理由」，或理由短到没有信息量
  D4 ERROR  缺少证据链段，或缺 intake / research / define / shape / decide 某一步
  D5 ERROR  证据链某一步为空，或没写「证据 / 理由 / 依据」
  D6 ERROR  三态=kill，但 decide 没写「为什么现在不做 / 什么条件下再做 / 替代做法」
  D7 ERROR  缺少问询表，或某题既没有用户选择、也没标「未确认」
  D8 ERROR  问询表某题的方法来源不在方法库（M01~M16 或方法名，见 references/需求问询-方法映射.md）
  D9 ERROR  问询表某题的「落到 prompt.md」追溯无法解析（必须是第 N 段 / §N / R# / A#）
  D10 ERROR 第 3 节标了「未确认」，第 4 节假设与开放问题里没有逐条对应的条目
  D11 WARN  问询题目超过 9 题（硬上限，见 AGENTS.md 阶段 2）
  D12 WARN  三态=needs-clarification，却没写「还缺什么才能接」

注意：`templates/需求确认.md` 是**占位模板**，原样提交会被本闸门拦下（D1/D2/D3/D5/D7）——
占位符不是答案，这是有意的；复制后必须逐项替换 `<…>` 才算真实的需求确认。

用法：
    python scripts/check_confirm.py <需求确认.md> [...]
    python scripts/check_confirm.py --selftest
    python scripts/check_confirm.py --no-ignore <需求确认.md>

退出码：有 ERROR 时非零退出。检查项清单见 references/校验清单.md。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_prompt as cp  # noqa: E402  （同目录，复用闸门① 的解析器与 Finding）
import pick_methods as pm  # noqa: E402  （同目录，D8 用方法库判定 M 编号 / 方法名是否合法）

#: 本闸门自己的码（D0~D10 是 ERROR，D11/D12 是 WARN）。
D_CODES = ("D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12")
D_ERROR_CODES = ("D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10")
D_WARN_CODES = ("D11", "D12")

#: 证据链五步（spec-kit assess 的 intake → research → define → shape → decide 本地化）。
EVIDENCE_STEPS = ("intake", "research", "define", "shape", "decide")
#: 某一步算「写了证据」的词。只认这些词不认长度：一句空泛的「已确认」不是证据。
EVIDENCE_WORDS = ("证据", "为什么", "理由", "原因", "依据", "来源", "实测", "原话")
#: kill 的 decide 必须回答「为什么现在不做」；缺了它就只是一个「不」字。
KILL_REASON_RE = re.compile(r"(为什么|现在不做|暂不|不值得|替代|条件|等.*再|不做|放弃)")
#: needs-clarification 的 decide 必须写清「还缺什么才能接」。
NEEDS_REASON_RE = re.compile(r"(还缺|缺什么|待补|才能接|补什么|差什么)")
#: 问询表的表头列名（容忍几种常见写法）。
HEAD_QUESTION = ("题目", "问题", "问询题")
HEAD_METHOD = ("方法来源", "方法", "来源")
HEAD_ANSWER = ("用户的选择", "用户选择", "用户回答", "答案", "选择")
HEAD_TRACE = ("落到", "追溯", "写入", "对应段落")
_HEAD_CELL_CLEAN_RE = re.compile(r"[`*\s]")

#: 证据内容的否定式：`证据：无` / `证据：没有` 不算证据（独立验证 F3）。
EVIDENCE_NEGATION_RE = re.compile(
    r"^(?:无|没有|暂无|不适用|不知道|待补|没|未|空|n/?a|none)[\s。．.;；,，、]*$",
    re.IGNORECASE,
)
#: 「落到 prompt.md」必须能解析成段 / 需求 / 验收编号，否则「随便」也能过（独立验证 F2）。
TRACE_RE = re.compile(r"第\s*\d+\s*段|§\s*\d+|R\d+|A\d+")
#: 方法来源里的 M 编号形状；合法性还要对方法库（见 `_valid_method`）。
METHOD_ID_RE = re.compile(r"M\d{2}")


def _clean(value: str) -> str:
    """去掉反引号与多余空白：`` `go` `` 归一成取值。

    尖括号**不剥**：模板里的 `<选项原文>` 是占位符，剥掉就变成有效值，
    空白模板能「空跑」过闸门（独立验证 F1）。
    """
    return value.strip().strip("`").strip()


def _is_placeholder(value: str) -> bool:
    """模板填了一半的占位符不算有效字段。

    `<…>` 一律算占位：模板原样提交必须被拦（独立验证 F1），
    所以这里在清洗之前看原始值，且不再把尖括号当成格式装饰。
    """
    text = value.strip()
    if not text:
        return True
    if text in ("…", "…", "...", "待补充", "待填", "待定"):
        return True
    return bool(re.search(r"<[^<>\n]+>", text))


def _split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _find_section(
    sections: list[tuple[str, int, int, int, bool]],
    keywords: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> tuple[str, int, int, int, bool] | None:
    for section in sections:
        title = section[0]
        if any(keyword in title for keyword in keywords) and not any(word in title for word in exclude):
            return section
    return None


def _body(lines: list[str], fenced: list[bool], indented: list[bool],
          start: int, end: int) -> str:
    return "\n".join(
        cp.strip_comments(lines[i]) for i in range(start + 1, min(end, len(lines)))
        if not fenced[i] and not indented[i]
    )


def _table_rows(lines: list[str], fenced: list[bool], indented: list[bool],
                start: int, end: int) -> list[tuple[int, list[str]]]:
    """段内的表格行：`（行号, 单元格）`，围栏 / 缩进代码块不算。"""
    rows: list[tuple[int, list[str]]] = []
    for idx in range(start + 1, min(end, len(lines))):
        if fenced[idx] or indented[idx]:
            continue
        line = lines[idx].strip()
        if not line.startswith("|"):
            continue
        cells = _split_cells(line)
        if not any(cells):
            continue
        rows.append((idx + 1, cells))
    return rows


def _row_value(rows: list[tuple[int, list[str]]], keywords: tuple[str, ...],
               min_cells: int = 2) -> tuple[str, int] | None:
    """在「项 | 结论」两列表里按第一列关键词取值。"""
    for line_no, cells in rows:
        if len(cells) < min_cells:
            continue
        if any(keyword in cells[0] for keyword in keywords):
            return cells[1], line_no
    return None


def _classify_triage(value: str) -> str | None:
    """三态判定：kill 的判断必须在 go 之前 —— 「不接（kill）」里也有「接」字。"""
    text = _clean(value)
    if not text:
        return None
    low = text.lower()
    if "kill" in low or "不接" in text:
        return "kill"
    if "needs-clarification" in low or "先问" in text or "需要澄清" in text or "待澄清" in text:
        return "needs-clarification"
    if re.search(r"(?:^|[（(\s])go(?:[）)\s]|$)", low) or text.startswith("接") or "接（" in text:
        return "go"
    return None


def _classify_mode(value: str) -> str | None:
    text = _clean(value)
    if "一次一问" in text:
        return "一次一问"
    if "菜单式" in text or text == "菜单":
        return "菜单式"
    return None


def _parse_evidence(lines: list[str], fenced: list[bool], indented: list[bool],
                    start: int, end: int) -> tuple[dict[str, tuple[str, int]], list[int]]:
    """解析证据链五步：返回 ({步骤: (内容, 行号)}, 五步中缺失的列表)。"""
    found: dict[str, tuple[str, int]] = {}
    lines_of: dict[str, int] = {}
    for idx in range(start + 1, min(end, len(lines))):
        if fenced[idx] or indented[idx]:
            continue
        line = lines[idx]
        stripped = line.strip()
        step = None
        content = ""
        if stripped.startswith("|"):
            cells = _split_cells(stripped)
            if cells and cells[0].strip().lower() in EVIDENCE_STEPS:
                step = cells[0].strip().lower()
                content = " ".join(cells[1:]).strip()
        else:
            match = re.match(
                r"^\s*(?:[-*+]\s*|\d+[.)]\s*)?(intake|research|define|shape|decide)\s*[:：]\s*(.*)$",
                stripped, re.IGNORECASE,
            )
            if match:
                step = match.group(1).lower()
                content = match.group(2).strip()
        if step is not None and step not in found:
            found[step] = (content, idx + 1)
            lines_of[step] = idx + 1
    missing = [step for step in EVIDENCE_STEPS if step not in found]
    return found, missing


def _looks_like_header(cells: list[str]) -> bool:
    clean = [_HEAD_CELL_CLEAN_RE.sub("", cell) for cell in cells]
    return any(any(keyword in cell for keyword in HEAD_QUESTION) for cell in clean)


def _find_header(rows: list[tuple[int, list[str]]]) -> int | None:
    for index, (_line_no, cells) in enumerate(rows):
        if _looks_like_header(cells):
            return index
    return None


def _column_index(header: list[str], keywords: tuple[str, ...]) -> int | None:
    clean = [_HEAD_CELL_CLEAN_RE.sub("", cell) for cell in header]
    for index, cell in enumerate(clean):
        if any(keyword in cell for keyword in keywords):
            return index
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return _clean(cells[index])


def _valid_method(value: str) -> bool:
    """方法来源必须落在方法库：M01~M16，或方法库里的方法名（独立验证 F2）。

    旧版只查非空，于是 `M99` / `瞎编方法` / `随便` 都能过 —— 那等于奖励任何非空字符串。
    """
    text = _clean(value)
    if not text or _is_placeholder(text):
        return False
    ids = METHOD_ID_RE.findall(text)
    known_ids = {method["id"] for method in pm.METHODS}
    if ids:
        return all(method_id in known_ids for method_id in ids)
    return any(method["name"] in text for method in pm.METHODS)


def _valid_trace(value: str) -> bool:
    """「落到 prompt.md」必须能解析：第 N 段 / §N / R# / A#（独立验证 F2）。"""
    text = _clean(value)
    if not text or _is_placeholder(text):
        return False
    return bool(TRACE_RE.search(text))


#: 证据正文去掉「否定 / 推迟 / 空话」后的残留：空 = 没写实质内容（第二轮 R2-F1，第三轮 R3-F1）。
#: 不能只靠整段正则：`无（不适用）`、`没有证据，先按默认` 加字加括号就能绕过。
#: 词表按长度从长到短排：`暂无结论` 必须先于 `暂无` 被剥，否则会漏下「结论」两个字冒充实质内容。
_EVIDENCE_FILLER_WORDS = tuple(sorted({
    "先按默认", "按默认", "默认", "暂无结论", "暂无数据", "暂无", "尚无结论", "尚无", "尚未",
    "尚不明确", "尚不清楚", "尚不", "不明确", "不清楚", "不确定", "不肯定", "不好说", "说不好",
    "说不上", "无法确定", "无法判断", "无法确认", "无法核实", "无可奉告", "没有结论", "没有数据",
    "还没有结论", "没有实质内容", "无任何有效信息",
    "没有", "不存在", "不适用", "不涉及", "不知道", "不明白", "不理解", "未定", "未知", "待定",
    "待补", "待确认", "待核实", "待观察", "待查", "再看看", "再看", "再定", "之后再", "进一步",
    "确认", "核实", "补充", "后续", "暂时", "目前", "现在", "当前", "看情况", "视情况", "再说",
    "大概率", "应该", "可能是", "也许是", "大概是", "估计", "猜测", "看起来", "听起来", "似乎",
    "好像", "恐怕", "说不定", "结论", "情况", "是", "无", "没", "未", "空", "暂", "先", "再", "等", "看",
}, key=lambda word: (-len(word), word)))


def _evidence_substance(text: str) -> str:
    """把证据正文剥到只剩「实质内容」：去括号补充、证据标记词、标点、开头否定/推迟词。

    判 D5 时只要残留为空，就说明这段证据只是在说「没有 / 不适用 / 待补 / 按默认」，
    哪怕加括号或逗号后半句也不该算证据。
    """
    body = re.sub(r"[（(][^（）()]*[）)]", "", text)
    body = re.sub(r"(?:证据|依据|理由|原因|来源|为什么|实测|原话)", "", body)
    body = re.sub(r"[\s。．.;；,，、:：…!！?？\-—_/\\|（）()\[\]【】「」『』\"'']", "", body)
    changed = True
    while changed:
        changed = False
        for word in _EVIDENCE_FILLER_WORDS:
            if body.startswith(word):
                body = body[len(word):]
                changed = True
                break
    return body


def _evidence_ok(content: str) -> bool:
    """证据链一步算不算「写了证据」：不能是占位、不能是否定式、不能是空话（独立验证 F3）。"""
    text = _clean(content)
    if not text or _is_placeholder(text):
        return False
    body = re.sub(r"^\s*(?:证据|理由|依据|原因|来源|为什么|实测|原话)\s*[:：]\s*", "", text)
    if len(re.sub(r"\s", "", body)) < 4:
        return False
    if EVIDENCE_NEGATION_RE.match(body.strip()) or not _evidence_substance(body):
        return False
    return any(word in text for word in EVIDENCE_WORDS)


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _content_tokens(text: str) -> set[str]:
    """逐条对账用的词：英文单词 + 中文二元组（与闸门③ 的 keywords() 同口径）。"""
    tokens = {word.lower() for word in _ASCII_WORD_RE.findall(text)}
    for run in _CJK_RUN_RE.findall(text):
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _unmatched_pending(pending: list[tuple[int, str]], body4: str) -> list[tuple[int, str]]:
    """第 3 节每条「未确认」都要在第 4 节有对得上的条目（独立验证 F9）。

    旧版只要求第 4 节出现「未确认」四个字：一条 pending 配任意一条无关条目就能过。
    """
    if not pending:
        return []
    body_tokens = _content_tokens(body4)
    unmatched: list[tuple[int, str]] = []
    for line_no, question in pending:
        tokens = _content_tokens(question)
        if tokens and tokens & body_tokens:
            continue
        unmatched.append((line_no, question))
    return unmatched


def check_confirm_file(
    path: Path, use_ignore: bool = True,
    suppressed: list[tuple[cp.Finding, str]] | None = None,
) -> tuple[list[cp.Finding], int]:
    raw = cp.normalize_invisible(cp.read_text(path))
    text_lines = cp.split_lines(raw)
    lines = cp.split_lines(cp.strip_comments(raw))
    fenced = cp.fence_lines(lines)
    indented = cp.indented_code_lines(lines, fenced)
    sections, _preamble = cp.split_sections(lines)
    findings: list[cp.Finding] = []

    if not sections:
        findings.append(cp.Finding("ERROR", "D0", "找不到任何 Markdown 标题；需求确认必须按 0~7 节分节"))
        return findings, len(raw)

    # ---- D1/D2/D3 第 0 节：问询模式 / 三态判定 / 一句理由 ----------------
    sec0 = _find_section(sections, ("目标体检",))
    triage = ""
    mode = ""
    reason = ""
    if sec0 is None:
        findings.append(cp.Finding("ERROR", "D2", "缺少「目标体检」段（阶段 0），分诊结论无处可查"))
        findings.append(cp.Finding("ERROR", "D1", "缺少「问询模式」字段：菜单式 / 一次一问"))
        findings.append(cp.Finding("ERROR", "D3", "缺少「一句理由」：为什么接、为什么先问、为什么不做"))
    else:
        rows0 = _table_rows(lines, fenced, indented, sec0[2], sec0[3])
        triage_row = _row_value(rows0, ("分诊",))
        mode_row = _row_value(rows0, ("问询模式",))
        reason_row = _row_value(rows0, ("理由",))
        if triage_row is None:
            findings.append(cp.Finding("ERROR", "D2", "第 0 节缺少「分诊结论（阶段 -1）」一行"))
        else:
            triage = "" if _is_placeholder(triage_row[0]) else (_classify_triage(triage_row[0]) or "")
            if not triage:
                findings.append(cp.Finding(
                    "ERROR", "D2",
                    "分诊结论必须落在三态之一：接（go）/ 先问一句（needs-clarification）/ 不接（kill）",
                    triage_row[1],
                ))
        if mode_row is None:
            findings.append(cp.Finding("ERROR", "D1", "第 0 节缺少「问询模式」一行"))
        else:
            mode = "" if _is_placeholder(mode_row[0]) else (_classify_mode(mode_row[0]) or "")
            if not mode:
                findings.append(cp.Finding(
                    "ERROR", "D1",
                    "问询模式只能是「菜单式」或「一次一问」（阶段 2 的两套模式，见 AGENTS.md）",
                    mode_row[1],
                ))
        if reason_row is None:
            findings.append(cp.Finding("ERROR", "D3", "第 0 节缺少「一句理由」一行"))
        else:
            reason = _clean(reason_row[0])
            if _is_placeholder(reason) or len(reason) < 8:
                findings.append(cp.Finding(
                    "ERROR", "D3",
                    f"一句理由太短（{len(reason)} 字）：至少说清交付物有没有、是不是任务级、缺的信息能不能自查到",
                    reason_row[1],
                ))

    # ---- D4/D5/D6 证据链（intake → research → define → shape → decide）----
    sec_evidence = _find_section(sections, ("证据链",))
    decide_text = ""
    if sec_evidence is None:
        findings.append(cp.Finding(
            "ERROR", "D4",
            "缺少证据链段（intake → research → define → shape → decide）：分诊结论必须有据可查，"
            "kill 不能只写一句「不做」",
        ))
    else:
        evidence, missing = _parse_evidence(
            lines, fenced, indented, sec_evidence[2], sec_evidence[3])
        if missing:
            findings.append(cp.Finding(
                "ERROR", "D4",
                "证据链缺步骤：" + "、".join(missing) + "（五步：intake → research → define → shape → decide）",
                sec_evidence[2] + 1,
            ))
        for step in EVIDENCE_STEPS:
            if step not in evidence:
                continue
            content, line_no = evidence[step]
            clean = _clean(content)
            if step == "decide":
                decide_text = clean
            if not clean or _is_placeholder(clean):
                findings.append(cp.Finding(
                    "ERROR", "D5", f"证据链 {step} 这一条是空的：要写证据（查到了什么），不是占位",
                    line_no,
                ))
            elif not _evidence_ok(clean):
                findings.append(cp.Finding(
                    "ERROR", "D5",
                    f"证据链 {step} 没写有效证据：「证据：无」这类否定式、或只写结论，都不算证据",
                    line_no,
                ))
        if triage == "kill" and decide_text and not KILL_REASON_RE.search(decide_text):
            findings.append(cp.Finding(
                "ERROR", "D6",
                "分诊=kill，但 decide 没写「为什么现在不做 / 什么条件下再做 / 替代做法」——"
                "kill 不是一句「不做了」，停掉也要有据",
                evidence.get("decide", ("", sec_evidence[2] + 1))[1],
            ))

    # ---- D7/D8/D9 问询与拍板表 ------------------------------------------
    # 先找「问询与拍板」；找不到再退到任何含「问询」的段，但要排除「问询记录」——
    # 问询记录是可选产物，不能拿它冒充拍板表。
    sec_interview = _find_section(sections, ("问询与拍板",))
    if sec_interview is None:
        sec_interview = _find_section(sections, ("问询",), exclude=("记录",))
    if sec_interview is None:
        findings.append(cp.Finding("ERROR", "D7", "缺少「问询与拍板」段：每道题的答案必须落盘"))
    else:
        rows = _table_rows(lines, fenced, indented, sec_interview[2], sec_interview[3])
        header_index = _find_header(rows)
        body_text = _body(lines, fenced, indented, sec_interview[2], sec_interview[3])
        if header_index is None:
            findings.append(cp.Finding(
                "ERROR", "D7",
                "问询表缺少表头（至少要有：题目 / 用户的选择），无法定位每道题的答案",
                sec_interview[2] + 1,
            ))
        else:
            header_line, header = rows[header_index]
            idx_question = _column_index(header, HEAD_QUESTION)
            idx_method = _column_index(header, HEAD_METHOD)
            idx_answer = _column_index(header, HEAD_ANSWER)
            idx_trace = _column_index(header, HEAD_TRACE)
            if idx_answer is None:
                findings.append(cp.Finding(
                    "ERROR", "D7", "问询表缺少「用户的选择」列：每题必须有答案或标「未确认」", header_line))
            if idx_method is None:
                findings.append(cp.Finding(
                    "ERROR", "D8", "问询表缺少「方法来源」列：用哪个方法问的（见 references/需求问询-方法映射.md）",
                    header_line))
            if idx_trace is None:
                findings.append(cp.Finding(
                    "ERROR", "D9", "问询表缺少「落到 prompt.md」列：答案进了第几段 / 哪条 R#", header_line))

            data_rows: list[tuple[int, list[str]]] = []
            for line_no, cells in rows[header_index + 1:]:
                if all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
                    continue   # 分隔行
                if idx_answer is not None and _cell(cells, idx_question) == "" and _cell(cells, idx_answer) == "":
                    continue   # 空行 / 只有编号
                data_rows.append((line_no, cells))

            if not data_rows:
                if not any(word in body_text for word in ("无需问询", "不需要问询", "没有需要问")):
                    findings.append(cp.Finding(
                        "ERROR", "D7",
                        "问询表里没有任何题目：要么补上问过的问题，要么明确写「本次无需问询」并说明原因",
                        header_line,
                    ))
            else:
                pending: list[tuple[int, str]] = []
                for line_no, cells in data_rows:
                    answer = _cell(cells, idx_answer) if idx_answer is not None else ""
                    if idx_answer is not None and (not answer or _is_placeholder(answer)):
                        findings.append(cp.Finding(
                            "ERROR", "D7",
                            "这道题既没有用户的选择，也没标「未确认」：空白不等于默认值",
                            line_no,
                        ))
                    elif "未确认" in answer:
                        question = _cell(cells, idx_question) if idx_question is not None else ""
                        pending.append((line_no, question))
                    if idx_method is not None:
                        method = _cell(cells, idx_method)
                        if not _valid_method(method):
                            findings.append(cp.Finding(
                                "ERROR", "D8",
                                "这道题的方法来源不在方法库（M01~M16 或方法名，见 references/需求问询-方法映射.md）："
                                "问法也要留档，不能随便写",
                                line_no,
                            ))
                    if idx_trace is not None:
                        trace = _cell(cells, idx_trace)
                        if not _valid_trace(trace):
                            findings.append(cp.Finding(
                                "ERROR", "D9",
                                "这道题缺可解析的「落到 prompt.md」追溯：写清第几段 / 哪条 R# / 哪条 A#，"
                                "否则答案会丢",
                                line_no,
                            ))
                if len(data_rows) > 9:
                    findings.append(cp.Finding(
                        "WARN", "D11",
                        f"问询题目 {len(data_rows)} 道，超过 9 道硬上限（优先 ≤5；超了就改默认值 + 未确认）",
                        data_rows[0][0],
                    ))
                # D10：第 3 节标了「未确认」，第 4 节必须有**逐条**对应的条目。
                if pending:
                    sec4 = _find_section(sections, ("假设与开放问题", "假设"))
                    body4 = ""
                    if sec4 is not None:
                        body4 = _body(lines, fenced, indented, sec4[2], sec4[3])
                    if sec4 is None or "未确认" not in body4:
                        findings.append(cp.Finding(
                            "ERROR", "D10",
                            "问询表里有「未确认」的题，但第 4 节假设与开放问题里没有「未确认」条目 ——"
                            "答案没落盘就等于没问",
                            pending[0][0],
                        ))
                    else:
                        for line_no, question in _unmatched_pending(pending, body4):
                            snippet = re.sub(r"\s+", " ", question)[:40]
                            findings.append(cp.Finding(
                                "ERROR", "D10",
                                f"问询表第 {line_no} 行这道题标了「未确认」，但第 4 节找不到说同一件事的"
                                f"「未确认」条目：{snippet}",
                                line_no,
                            ))

    # ---- D12：needs-clarification 要写清还缺什么才能接 ---------------------
    if triage == "needs-clarification" and not NEEDS_REASON_RE.search(decide_text):
        findings.append(cp.Finding(
            "WARN", "D12",
            "三态=needs-clarification，但没写「还缺什么才能接」：先说清缺哪条信息、谁能补",
        ))

    # ---- 豁免注释 ---------------------------------------------------------
    ignores, problems = cp.parse_ignores(
        text_lines,
        codes=cp.IGNORE_CODES + D_WARN_CODES,
        error_codes=cp.ERROR_CODES + D_ERROR_CODES,
    )
    findings.extend(problems)
    if use_ignore and ignores:
        findings, dropped = cp.apply_ignores(
            findings, ignores, cp.section_index(lines, sections))
        if suppressed is not None:
            suppressed.extend(dropped)

    return findings, len(raw)


def report(path: Path, use_ignore: bool = True) -> int:
    suppressed: list[tuple[cp.Finding, str]] = []
    try:
        findings, size = check_confirm_file(path, use_ignore=use_ignore, suppressed=suppressed)
    except cp.FileReadError as exc:
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
    if errors:
        print(f"结果：{len(errors)} ERROR, {len(warns)} WARN —— 未通过")
        return 1
    tail = "（WARN 需逐条说明为什么不影响执行）" if warns else ""
    if suppressed:
        tail = f"（另有 {len(suppressed)} 条 WARN 按 `pf-ignore` 注释豁免，`--no-ignore` 可看全）"
    print(f"结果：0 ERROR, {len(warns)} WARN —— 通过" + tail)
    return 0


VALID_CONFIRM = """# 需求确认 · download-tidy

> 这份文件是给人看的过程记录，给 AI 执行的是 prompt.md。
> 机器闸门：`python scripts/check_confirm.py 需求确认.md`

## 0. 目标体检（阶段 0）

| 项 | 结论 |
| --- | --- |
| 分诊结论（阶段 -1） | 接（go） |
| 一句理由 | 交付物明确是单个脚本，属于任务级，缺的信息能从下载目录实测补齐 |
| 问询模式 | 菜单式 |
| 谁执行这条提示词 | Claude Code |
| 产物落在哪里 | 输出目录 |
| 手上已有的上下文 | 下载目录实测 1200 个文件、8 类扩展名 |
| 输出目录 | `输出/download-tidy/` |

## 0.5 证据链（intake → research → define → shape → decide）

- intake：证据：用户原话「帮我把下载文件夹整理一下」；要的是能跑的脚本，不是一次性手工整理。
- research：证据：实测 D:\\\\Downloads 共 1200 个文件、8 类扩展名；用户截图 2 张。
- define：证据：范围收窄为「按扩展名分 4 类 + 同名追加 -1」，不做去重与云同步。
- shape：证据：选单个 Python 脚本、零依赖，排除需要安装第三方库的方案。
- decide：证据：go —— 交付物、边界、验收都能当场确定；移动文件不可逆，已写进第 11 段。

## 1. 用户原话

> 帮我把下载文件夹整理一下，太乱了。

## 2. 预调研：已知 / 未知（阶段 1）

**已知**

- [用户原话] 只整理下载文件夹。
- [仓库实测] 该目录 1200 个文件，8 类扩展名。

**未知**

- 同名文件怎么处理。

## 3. 问询与拍板（阶段 2）

| # | 题目 | 方法来源 | 用户的选择 | 落到 prompt.md |
| --- | --- | --- | --- | --- |
| Q1 | 交付物做成什么形态？ | M01 五问定位 | 单个 Python 脚本 run.py | 第 3 段 / 交付物 |
| Q2 | 同名文件怎么处理？ | M13 验收口径 | 追加 -1、-2，不覆盖 | R2 + A2 |
| Q3 | 其他盘要不要一起整理？ | M02 反向问（非目标） | 未确认：默认只整理 D:\\\\Downloads | 第 10 段假设 |

## 4. 假设与开放问题（未确认，已写进 prompt.md 第 10 段）

- 未确认：是否只整理 D:\\\\Downloads（默认是；影响第 4 段；改起来一个参数）

## 5. 迭代记录

| 轮次 | 用户意见（原话） | 影响范围 | 处理 |
| --- | --- | --- | --- |
| 1 | 先这样 | 第 3 段 | 重跑闸门①③④ |

## 6. 问询记录（可选产物 `问询记录.md`）

| # | 题目 | 结果 |
| --- | --- | --- |
| Q4 | 要不要保留整理日志？ | 跳过：能自己查到（脚本默认写日志） |

## 7. 确认

以上若有偏差请指出；确认无误请回 **准确，开始生成**。
"""


def selftest() -> int:
    """自测：每个 case 固定期望的码集合；期望值都先用真实文件跑过一遍。"""
    base = VALID_CONFIRM
    triage_row = "| 分诊结论（阶段 -1） | 接（go） |"
    mode_row = "| 问询模式 | 菜单式 |"
    reason_row = "| 一句理由 | 交付物明确是单个脚本，属于任务级，缺的信息能从下载目录实测补齐 |"
    evidence_head = "## 0.5 证据链（intake → research → define → shape → decide）"
    intake_line = "- intake：证据：用户原话「帮我把下载文件夹整理一下」；要的是能跑的脚本，不是一次性手工整理。"
    decide_line = "- decide：证据：go —— 交付物、边界、验收都能当场确定；移动文件不可逆，已写进第 11 段。"
    q1_line = "| Q1 | 交付物做成什么形态？ | M01 五问定位 | 单个 Python 脚本 run.py | 第 3 段 / 交付物 |"
    q2_line = "| Q2 | 同名文件怎么处理？ | M13 验收口径 | 追加 -1、-2，不覆盖 | R2 + A2 |"
    q3_line = "| Q3 | 其他盘要不要一起整理？ | M02 反向问（非目标） | 未确认：默认只整理 D:\\\\Downloads | 第 10 段假设 |"
    assumption_line = "- 未确认：是否只整理 D:\\\\Downloads（默认是；影响第 4 段；改起来一个参数）"
    interview_head = "## 3. 问询与拍板（阶段 2）"

    def sub(text: str, old: str, new: str) -> str:
        if old not in text:
            raise AssertionError(f"替换目标不存在（夹具漂移了？）：{old!r}")
        return text.replace(old, new, 1)

    def row_removed(text: str, row: str) -> str:
        return sub(text, row + "\n", "")

    def extra_questions() -> str:
        rows = "\n".join(
            f"| Q{i} | 第 {i} 个补充问题？ | M01 五问定位 | 按默认 | 第 10 段假设 |"
            for i in range(4, 11)
        )
        return sub(base, q3_line, q3_line + "\n" + rows)

    cases: list[tuple[str, object, set[str], set[str], str]] = [
        ("基准件：完整的需求确认", lambda: base, set(), set(), ""),
        ("D1 问询模式为空", lambda: sub(base, mode_row, "| 问询模式 |  |"), {"D1"}, set(), ""),
        ("D1 问询模式取值非法",
         lambda: sub(base, mode_row, "| 问询模式 | 随便问问 |"), {"D1"}, set(), ""),
        ("D1 缺问询模式行", lambda: row_removed(base, mode_row), {"D1"}, set(), ""),
        ("D2 缺分诊结论行", lambda: row_removed(base, triage_row), {"D2"}, set(), ""),
        ("D2 分诊结论不合法",
         lambda: sub(base, triage_row, "| 分诊结论（阶段 -1） | 再看看 |"), {"D2"}, set(), ""),
        ("D3 一句理由太短", lambda: sub(base, reason_row, "| 一句理由 | 行 |"), {"D3"}, set(), ""),
        ("D4 缺证据链段",
         lambda: sub(base, evidence_head, "## 0.5 补充说明"), {"D4"}, set(), ""),
        ("D4 证据链缺一步",
         lambda: sub(base,
                     "- shape：证据：选单个 Python 脚本、零依赖，排除需要安装第三方库的方案。\n", ""),
         {"D4"}, set(), ""),
        ("D5 证据链某步为空",
         lambda: sub(base, intake_line, "- intake："), {"D5"}, set(), ""),
        ("D5 证据链某步只写结论不写证据",
         lambda: sub(base, intake_line, "- intake：用户说要整理下载文件夹，已确认"), {"D5"}, set(), ""),
        ("D6 kill 但 decide 没写为什么",
         lambda: sub(sub(base, triage_row, "| 分诊结论（阶段 -1） | 不接（kill） |"),
                     decide_line, "- decide：证据：这个方向先放着。"), {"D6"}, set(), ""),
        ("D6 kill 写了为什么就通过",
         lambda: sub(sub(base, triage_row, "| 分诊结论（阶段 -1） | 不接（kill） |"),
                     decide_line,
                     "- decide：证据：现在不做，因为已有同类工具；等用户真的要迁移时再做。"),
         set(), set(), ""),
        ("D7 某题答案空白", lambda: sub(base, q2_line,
                                         "| Q2 | 同名文件怎么处理？ | M13 验收口径 |  | R2 + A2 |"),
         {"D7"}, set(), ""),
        ("D7 缺问询表", lambda: sub(base, interview_head, "## 3. 补充说明"), {"D7"}, set(), ""),
        ("D7 问询表没有任何题目",
         lambda: sub(sub(sub(base, q1_line, ""), q2_line, ""), q3_line, ""),
         {"D7"}, set(), ""),
        ("D7 明确写无需问询则通过",
         lambda: sub(sub(sub(sub(base, q1_line, ""), q2_line, ""), q3_line, ""),
                     interview_head, interview_head + "\n\n本次无需问询：用户原话已经足够。"),
         set(), set(), ""),
        ("D8 某题缺方法来源",
         lambda: sub(base, q2_line, "| Q2 | 同名文件怎么处理？ |  | 追加 -1、-2，不覆盖 | R2 + A2 |"),
         {"D8"}, set(), ""),
        ("D9 某题缺落到 prompt.md",
         lambda: sub(base, q3_line,
                     "| Q3 | 其他盘要不要一起整理？ | M02 反向问（非目标） | 未确认：默认只整理 D:\\\\Downloads |  |"),
         {"D9"}, set(), ""),
        ("D10 标了未确认但第 4 节没有对应条目",
         lambda: sub(base, assumption_line + "\n", ""), {"D10"}, set(), ""),
        # F1（独立验证）：模板占位符原样提交必须被拦，不能剥掉尖括号就当有效值。
        ("F1 占位：分诊结论还是 <…>",
         lambda: sub(base, triage_row,
                     "| 分诊结论（阶段 -1） | <接（go）/ 先问一句（needs-clarification）/ 不接（kill）> |"),
         {"D2"}, set(), ""),
        ("F1 占位：问询模式还是 <…>",
         lambda: sub(base, mode_row, "| 问询模式 | <菜单式 / 一次一问> |"), {"D1"}, set(), ""),
        ("F1 占位：一句理由还是 <…>",
         lambda: sub(base, reason_row,
                     "| 一句理由 | <说清交付物有没有、是不是任务级、缺的信息能不能自查> |"),
         {"D3"}, set(), ""),
        ("F1 占位：某题答案还是 <…>",
         lambda: sub(base, q2_line,
                     "| Q2 | 同名文件怎么处理？ | M13 验收口径 | <替换成用户的选择> | R2 + A2 |"),
         {"D7"}, set(), ""),
        ("F1 占位：证据链某步还是 <…>",
         lambda: sub(base, intake_line, "- intake：证据：<写一句证据>"), {"D5"}, set(), ""),
        # F2（独立验证）：方法来源必须在方法库，追溯必须能解析。
        ("F2 方法来源 M99 不存在",
         lambda: sub(base, q2_line,
                     "| Q2 | 同名文件怎么处理？ | M99 自创方法 | 追加 -1、-2，不覆盖 | R2 + A2 |"),
         {"D8"}, set(), ""),
        ("F2 方法来源是瞎编的方法名",
         lambda: sub(base, q2_line,
                     "| Q2 | 同名文件怎么处理？ | 瞎编方法 | 追加 -1、-2，不覆盖 | R2 + A2 |"),
         {"D8"}, set(), ""),
        ("F2 追溯只写「随便」",
         lambda: sub(base, q2_line,
                     "| Q2 | 同名文件怎么处理？ | M13 验收口径 | 追加 -1、-2，不覆盖 | 随便 |"),
         {"D9"}, set(), ""),
        ("F2 追溯「第 3 段 + R2 + A2」能解析",
         lambda: sub(base, q2_line,
                     "| Q2 | 同名文件怎么处理？ | M13 验收口径 | 追加 -1、-2，不覆盖 | 第 3 段 + R2 + A2 |"),
         set(), set(), ""),
        # F3（独立验证）：「证据：无」这种否定式不算证据。
        ("F3 证据写成「证据：无」",
         lambda: sub(base, intake_line, "- intake：证据：无"), {"D5"}, set(), ""),
        # R2-F1（第二轮独立验证）：加括号 / 加逗号后半句不能绕过否定识别。
        ("R2-F1 证据写成「无（不适用）」",
         lambda: sub(base, intake_line, "- intake：证据：无（不适用）"), {"D5"}, set(), ""),
        ("R2-F1 证据写成「没有证据，先按默认」",
         lambda: sub(base, intake_line, "- intake：证据：没有证据，先按默认"), {"D5"}, set(), ""),
        ("R2-F1 证据写成「不存在，待补」",
         lambda: sub(base, intake_line, "- intake：证据：不存在，待补"), {"D5"}, set(), ""),
        # 对照：否定词开头但后面有实词/实测依据的长证据，不算空话。
        ("R2-F1 长证据以否定开头仍算证据",
         lambda: sub(base, intake_line,
                     "- intake：证据：没有现成的接口文档，实测 requests 自带示例可用，依据是官方 README。"),
         set(), set(), ""),
        # R3-F1（第三轮独立验证）：暂时没结论的空话不能当证据，加逗号/加括号也不行。
        ("R3-F1 证据写成「暂无结论」",
         lambda: sub(base, intake_line, "- intake：证据：暂无结论"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「暂无结论，先按默认」",
         lambda: sub(base, intake_line, "- intake：证据：暂无结论，先按默认"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「尚不明确」",
         lambda: sub(base, intake_line, "- intake：证据：尚不明确"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「无法确定」",
         lambda: sub(base, intake_line, "- intake：证据：无法确定"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「无可奉告」",
         lambda: sub(base, intake_line, "- intake：证据：无可奉告"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「不清楚」",
         lambda: sub(base, intake_line, "- intake：证据：不清楚"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「不知道」",
         lambda: sub(base, intake_line, "- intake：证据：不知道"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「说不好」",
         lambda: sub(base, intake_line, "- intake：证据：说不好"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「未定」",
         lambda: sub(base, intake_line, "- intake：证据：未定"), {"D5"}, set(), ""),
        ("R3-F1 证据写成「应该是，待核实」",
         lambda: sub(base, intake_line, "- intake：证据：应该是，待核实"), {"D5"}, set(), ""),
        # 对照：空话/猜测开头，但后面带实测依据或可核验事实的长证据，照常算证据。
        ("R3-F1 空话开头但带实测的长证据照样算证据",
         lambda: sub(base, intake_line,
                     "- intake：证据：暂无结论时先按默认，但实测 3 次均通过，日志见 out/run.log。"),
         set(), set(), ""),
        ("R3-F1 否定开头但带可核验事实的长证据照样算证据",
         lambda: sub(base, intake_line,
                     "- intake：证据：无报错，实测 3 次均通过，日志见 out/log.txt。"),
         set(), set(), ""),
        ("R3-F1 猜测开头但带报错日志的长证据照样算证据",
         lambda: sub(base, intake_line,
                     "- intake：证据：看起来要 Python 3.9，实测 pip install 报 requires-python>=3.9（依据官方 README）。"),
         set(), set(), ""),
        # R4-F1（第四轮独立验证）：换一种说法说空话，仍然不能当证据。
        ("R4-F1 证据写成「还没有结论」",
         lambda: sub(base, intake_line, "- intake：证据：还没有结论"), {"D5"}, set(), ""),
        ("R4-F1 证据写成「没有实质内容」",
         lambda: sub(base, intake_line, "- intake：证据：没有实质内容"), {"D5"}, set(), ""),
        ("R4-F1 证据写成「无任何有效信息」",
         lambda: sub(base, intake_line, "- intake：证据：无任何有效信息"), {"D5"}, set(), ""),
        # F9（独立验证）：未确认必须逐条对应，不是第 4 节出现「未确认」四个字就算。
        ("F9 未确认与第 4 节说的不是同一件事",
         lambda: sub(base, q3_line,
                     "| Q3 | 界面颜色用什么？ | M02 反向问（非目标） | 未确认：还没定 | 第 10 段假设 |"),
         {"D10"}, set(), ""),
        ("F9 两条未确认只有一条对得上",
         lambda: sub(base, q3_line, q3_line + "\n"
                     "| Q4 | 界面颜色用什么？ | M10 九维非功能扫描 | 未确认：还没定 | 第 10 段假设 |"),
         {"D10"}, set(), ""),
        ("D11 超过 9 题", extra_questions, set(), {"D11"}, ""),
        ("D12 needs-clarification 没写还缺什么",
         lambda: sub(sub(base, triage_row, "| 分诊结论（阶段 -1） | 先问一句（needs-clarification） |"),
                     decide_line, "- decide：证据：先问用户几个问题。"),
         set(), {"D12"}, ""),
        ("D12 needs-clarification 写了还缺什么就通过",
         lambda: sub(sub(base, triage_row, "| 分诊结论（阶段 -1） | 先问一句（needs-clarification） |"),
                     decide_line, "- decide：证据：还缺同名冲突的处理规则，问清后才能接。"),
         set(), set(), ""),
        ("D0 没有 Markdown 标题", lambda: "随便写点什么。\n", {"D0"}, set(), ""),
    ]

    failed = 0
    with tempfile.TemporaryDirectory(prefix="pf-confirm-") as tmp:
        for name, build, want_errors, want_warns, want_msg in cases:
            try:
                text = build()
            except AssertionError as exc:
                print(f"FAIL  [闸门④] {name}: {exc}")
                failed += 1
                continue
            if text == base and name != "基准件：完整的需求确认":
                print(f"FAIL  [闸门④] {name}: 用例没有改动夹具（空跑）")
                failed += 1
                continue
            path = Path(tmp) / "需求确认.md"
            path.write_text(text, encoding="utf-8")
            findings, _size = check_confirm_file(path)
            errors = {f.code for f in findings if f.level == "ERROR"}
            warns = {f.code for f in findings if f.level == "WARN"}
            ok = errors == want_errors and warns == want_warns
            if want_msg:
                ok = ok and any(want_msg in f.message for f in findings)
            print(f"{'PASS' if ok else 'FAIL'}  [闸门④] {name}: ERROR {sorted(errors)}"
                  f"（期望 {sorted(want_errors)}） WARN {sorted(warns)}（期望 {sorted(want_warns)}）")
            if not ok:
                failed += 1
                for finding in findings:
                    print("      " + finding.render())

        # --no-ignore：豁免注释默认生效，--no-ignore 时 D11 必须重新出现。
        text = extra_questions()
        text = sub(text, q3_line, q3_line + "\n<!-- pf-ignore: D11 本次问询按两轮合并 -->")
        path = Path(tmp) / "ignore.confirm.md"
        path.write_text(text, encoding="utf-8")
        findings, _size = check_confirm_file(path)
        warns = {f.code for f in findings if f.level == "WARN"}
        ok = "D11" not in warns
        print(f"{'PASS' if ok else 'FAIL'}  [闸门④] pf-ignore 能豁免 D11：WARN {sorted(warns)}")
        if not ok:
            failed += 1
        findings, _size = check_confirm_file(path, use_ignore=False)
        warns = {f.code for f in findings if f.level == "WARN"}
        ok = "D11" in warns
        print(f"{'PASS' if ok else 'FAIL'}  [闸门④] --no-ignore 时 D11 重新出现：WARN {sorted(warns)}")
        if not ok:
            failed += 1

        # F1（独立验证）：占位模板原样提交必须被拦，否则「先空跑过闸门再填」会漏。
        template = Path(__file__).resolve().parent.parent / "templates" / "需求确认.md"
        findings, _size = check_confirm_file(template)
        errors = sorted(f.code for f in findings if f.level == "ERROR")
        ok = bool(errors)
        print(f"{'PASS' if ok else 'FAIL'}  [闸门④] 占位模板原样提交必须被拦：ERROR {errors}")
        if not ok:
            failed += 1

    print(f"\n闸门④ 自测：{len(cases)} 个用例 + 2 个豁免用例 + 1 个模板负例")
    print(f"自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检查需求确认.md 的机器可读字段（问询模式 / 三态证据 / 追溯）")
    parser.add_argument("files", nargs="*", help="要检查的需求确认.md 路径")
    parser.add_argument("--selftest", action="store_true", help="跑本脚本内置用例")
    parser.add_argument("--no-ignore", action="store_true",
                        help="忽略 `<!-- pf-ignore: D码 理由 -->` 豁免注释，把所有 WARN 都报出来")
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
    sys.exit(main())
