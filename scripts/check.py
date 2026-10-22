#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 单入口：一条命令跑完这个文件该跑的所有零依赖闸门。

为什么要有它（竞品调研 README §3.3 第③条）：spec-kit 用 `/specify /plan /tasks /implement`
把流程串成命令，OpenSpec 用 `openspec validate` 一条命令验规格，Amagine3D 用 `doctor --vision`
一条命令跑体检。它们卖的是「一条命令跑完整流程」，我们卖的应该是同一个入口体验，但内容换成
**确定性、离线、零依赖的静态闸门**：不调模型、不联网、不写文件，同样输入永远同样输出。

用法：
    python scripts/check.py <文件或目录> [更多...]
    python scripts/check.py --json <文件>       # 机器可读（stdout 只有 JSON）
    python scripts/check.py --strict <文件>     # 只有 WARN 也算失败（CI 想更严时用）
    python scripts/check.py --selftest          # 自测：合格示例 0 ERROR、反例必须被拦

自动识别（看文件名 + 正文特征，判断永远可解释）：
    `SKILL.md` / 含 SKILL.md 的目录      → 闸门② check_skill
    文件名含「需求确认」或正文有「目标体检 / 问询与拍板 / 分诊结论」 → 闸门④ check_confirm
    `.prompt.md` / 其余 `.md` 提示词文档 → 闸门① check_prompt + 闸门③ check_contract
    目录（不含 SKILL.md）               → 递归找 .prompt.md / 需求确认 / SKILL.md 逐个查
    认不出的文件（例如 `.txt` / `.py`）  → E0 ERROR（不能 0 闸门静默「通过」）

退出码：有 ERROR → 1；只有 WARN → 0（--strict 时 1）；路径不存在/用法错误 → 2。
路径不存在时 --json 仍只往 stdout 写一份 JSON（`missing` 字段列出缺的路径、exit_code 为 2）。
既有四个闸门脚本的独立 CLI 行为不变，本脚本只做编排。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_confirm as cf  # noqa: E402  （同目录，闸门④）
import check_contract as cc  # noqa: E402  （同目录，闸门③）
import check_prompt as cp  # noqa: E402  （同目录，闸门①；也提供 Finding / read_text）
import check_skill as cs  # noqa: E402  （同目录，闸门②）

#: 目标类型 → 要依次跑哪些闸门（顺序即输出顺序，先契约后一致性/诊断）。
GATE_PLANS: dict[str, tuple[tuple[str, str], ...]] = {
    "prompt": (("闸门① 提示词契约", "prompt"), ("闸门③ 跨段一致性", "contract")),
    "confirm": (("闸门④ 需求确认", "confirm"),),
    "skill": (("闸门② 技能定义", "skill"),),
}
#: 没有对应闸门的目标（例如目录里找不到任何可检查文件）。
GATE_PLANS["unknown"] = ()
_CONFIRM_MARKERS = ("目标体检", "问询与拍板", "分诊结论", "证据链（intake")
_SKILL_NAME = "SKILL.md"


def _setup_stdout() -> None:
    """管道/重定向时强制 UTF-8（避免 Windows 上按 GBK 输出被读成乱码）。"""
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        else:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass


@dataclass
class GateResult:
    """一个目标过一道闸门的结果。findings 是各脚本自己的 Finding（字段一致：level/code/message/line）。"""

    label: str
    findings: list = field(default_factory=list)
    size: int = 0
    note: str = ""

    @property
    def errors(self) -> int:
        return sum(1 for finding in self.findings if finding.level == "ERROR")

    @property
    def warns(self) -> int:
        return sum(1 for finding in self.findings if finding.level == "WARN")


@dataclass
class TargetResult:
    path: Path
    kind: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(gate.errors for gate in self.gates)

    @property
    def warns(self) -> int:
        return sum(gate.warns for gate in self.gates)


def detect_kind(path: Path) -> str:
    """根据文件名（+ 正文特征）判断该走哪套闸门；判断口径见模块 docstring。"""
    if path.is_dir():
        return "skill" if (path / _SKILL_NAME).is_file() else "unknown"
    name = path.name
    if name == _SKILL_NAME:
        return "skill"
    if "需求确认" in name:
        return "confirm"
    if name.endswith(".prompt.md"):
        return "prompt"
    if name.endswith(".md"):
        try:
            text = cp.normalize_invisible(cp.read_text(path))
        except (cp.FileReadError, OSError):
            return "prompt"
        if any(marker in text for marker in _CONFIRM_MARKERS):
            return "confirm"
        return "prompt"
    return "unknown"


def _run_gate(kind: str, path: Path) -> GateResult:
    """真的调用对应闸门的检查函数（不打印，只有 findings 返回值）。"""
    try:
        if kind == "prompt":
            findings, size = cp.check_file(path)
            return GateResult("闸门① 提示词契约", list(findings), size)
        if kind == "contract":
            findings, size, note = cc.check_contract(path)
            return GateResult("闸门③ 跨段一致性", list(findings), size, note)
        if kind == "confirm":
            findings, size = cf.check_confirm_file(path)
            return GateResult("闸门④ 需求确认", list(findings), size)
        findings, size = cs.check_skill(path)
        return GateResult("闸门② 技能定义", list(findings), size)
    except cp.FileReadError as exc:
        # check_prompt 的 check_file 会把读不了的文件抛出来（独立 CLI 由 report() 接住）；
        # 单入口里必须变成一条 ERROR，不能整条命令崩掉。
        return GateResult("读文件", [cp.Finding("ERROR", "E0", f"读不了文件：{exc}", 0)])
    except OSError as exc:
        return GateResult("读文件", [cp.Finding("ERROR", "E0", f"读不了文件：{exc}", 0)])


def check_target(path: Path) -> TargetResult:
    kind = detect_kind(path)
    # 「含 SKILL.md 的目录」按目录里的 SKILL.md 查；展示路径仍是调用者给的那个。
    actual = path / _SKILL_NAME if kind == "skill" and path.is_dir() else path
    target = TargetResult(path, kind)
    if kind == "unknown":
        # 第 9 轮独立验证 V9-PF-01：unknown 文件以前 0 闸门 0 ERROR → rc=0「通过」，
        # 等于把 `foo.txt` 当合格资产；目录形态早就报 E0，文件形态必须同口径。
        message = ("目录里没有可检查的文件：需要 `.prompt.md`、文件名含「需求确认」的 `.md`，"
                   "或含 `SKILL.md` 的技能目录" if path.is_dir() else
                   "无法识别的文件类型：需要 `.prompt.md`、文件名含「需求确认」的 `.md`，"
                   "或 `SKILL.md`")
        target.gates.append(GateResult("识别", [cp.Finding("ERROR", "E0", message, 0)]))
        return target
    for label, gate_kind in GATE_PLANS[kind]:
        result = _run_gate(gate_kind, actual)
        result.label = label
        target.gates.append(result)
    return target


#: Windows 目录 junction / 其他重解析点的文件属性位（Python 3.9 没有 `Path.is_junction()`）。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_entry(entry: "os.DirEntry[str]") -> bool:
    """目录项是不是链接（符号链接 / junction / 其他重解析点）：扫描时一律不进入。"""
    try:
        if entry.is_symlink():
            return True
        stat_result = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(stat_result, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _iter_dir(dir_path: Path) -> list[Path]:
    """目录模式：递归找可检查的文件（跳 .git / __pycache__ 之类的噪音目录）。

    只收明确的目标类型（`*.prompt.md` / 文件名含「需求确认」/ `SKILL.md`）；不含这些特征的
    普通 `.md`（README、CHANGELOG…）不参与目录扫描 —— 要看它们就显式传文件路径。

    第 9 轮独立验证 V9-PF-02：旧版用 `dir_path.rglob("*")`，它会跟进目录符号链接 / junction：
    遇到「子目录 junction 指回祖先」的目录树会无限递归（3.12 上跑出 64 个重复目标，3.9 上
    直接 `OSError: [WinError 1921]` 崩掉）。这里改成自己 scandir 遍历，**不进入任何链接**。
    """
    found: list[Path] = []
    noise = {".git", "__pycache__", ".review-tmp", ".audit"}

    def scan(current: Path) -> None:
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in noise or _is_link_entry(entry):
                        continue
                    scan(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if name == _SKILL_NAME or "需求确认" in name or name.endswith(".prompt.md"):
                    found.append(Path(entry.path))
            except OSError:  # pragma: no cover - 权限/被占用等，跳过该项即可
                continue

    scan(dir_path)
    return found


def expand_targets(paths: list[Path]) -> list[TargetResult]:
    results: list[TargetResult] = []
    for path in paths:
        if path.is_dir():
            if (path / _SKILL_NAME).is_file():
                results.append(check_target(path))
                continue
            children = _iter_dir(path)
            if not children:
                results.append(check_target(path))
                continue
            for child in children:
                results.append(check_target(child))
        else:
            results.append(check_target(path))
    return results


def exit_code(errors: int, warns: int, strict: bool = False) -> int:
    """统一退出码：有 ERROR → 1；只有 WARN → 0（--strict 时 1）。"""
    if errors:
        return 1
    if strict and warns:
        return 1
    return 0


def _finding_dict(finding) -> dict:
    return {
        "level": finding.level,
        "code": finding.code,
        "message": finding.message,
        "line": finding.line,
    }


def build_payload(results: list[TargetResult], strict: bool,
                  missing: list[str] | None = None) -> dict:
    errors = sum(target.errors for target in results)
    warns = sum(target.warns for target in results)
    missing = list(missing or [])
    return {
        "tool": "prompt-forge check",
        "version": 1,
        "strict": strict,
        # 路径不存在属于用法错误：即使其它目标全过，也按 rc=2 退出（与文本模式同口径）。
        "exit_code": 2 if missing else exit_code(errors, warns, strict),
        "totals": {"targets": len(results), "errors": errors, "warns": warns},
        "missing": missing,
        "targets": [
            {
                "path": str(target.path),
                "kind": target.kind,
                "errors": target.errors,
                "warns": target.warns,
                "gates": [
                    {
                        "label": gate.label,
                        "errors": gate.errors,
                        "warns": gate.warns,
                        "size": gate.size,
                        "note": gate.note,
                        "findings": [_finding_dict(finding) for finding in gate.findings],
                    }
                    for gate in target.gates
                ],
            }
            for target in results
        ],
    }


def _shorten(text: str, limit: int = 88) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def print_summary(results: list[TargetResult], strict: bool) -> int:
    """统一汇总表 + 关键条目；返回退出码。"""
    errors = sum(target.errors for target in results)
    warns = sum(target.warns for target in results)
    gate_total = sum(len(target.gates) for target in results)
    print(f"prompt-forge check：{len(results)} 个目标，{gate_total} 道闸门"
          f"（零依赖 / 离线 / 确定性）")
    if results:
        width = min(max(len(str(target.path)) for target in results), 46)
        print(f"\n{'目标':<{width}}  {'闸门':<16} ERROR  WARN")
        print(f"{'-' * width}  {'-' * 16} -----  ----")
        for target in results:
            name = str(target.path)
            if len(name) > width:
                name = "…" + name[-(width - 1):]
            for gate in target.gates:
                print(f"{name:<{width}}  {gate.label:<16} {gate.errors:^5}  {gate.warns:^4}")
    key_lines: list[str] = []
    for target in results:
        for gate in target.gates:
            for finding in gate.findings:
                if finding.level == "ERROR":
                    key_lines.append(f"  {target.path}  [{finding.code}] {_shorten(finding.message)}"
                                     + (f"（第 {finding.line} 行）" if finding.line else ""))
    if key_lines:
        print(f"\n关键条目（{len(key_lines)} 条 ERROR）：")
        for line in key_lines[:40]:
            print(line)
        if len(key_lines) > 40:
            print(f"  …… 还有 {len(key_lines) - 40} 条，用 --json 看全量")
    warn_lines = [f"  {target.path}  [{finding.code}] {_shorten(finding.message)}"
                  for target in results for gate in target.gates for finding in gate.findings
                  if finding.level == "WARN"]
    if warn_lines:
        print(f"\nWARN（{len(warn_lines)} 条，不影响退出码；--strict 时算失败）：")
        for line in warn_lines[:10]:
            print(line)
        if len(warn_lines) > 10:
            print(f"  …… 还有 {len(warn_lines) - 10} 条，用 --json 看全量")
    code = exit_code(errors, warns, strict)
    verdict = "通过" if code == 0 else "未通过"
    print(f"\n结果：{len(results)} 个目标，{errors} ERROR，{warns} WARN —— {verdict}"
          + ("（--strict：WARN 也算失败）" if strict and warns and not errors else ""))
    return code


def selftest() -> int:
    """自测：合格示例 0 ERROR、反例必须被拦、分类与退出码口径正确。"""
    failed = 0
    repo = Path(__file__).resolve().parent.parent
    examples = repo / "examples"
    skills = examples / "技能定义"

    def report_case(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        print(f"{'PASS' if ok else 'FAIL'}  [单入口] {label}" + (f": {detail}" if detail else ""))
        if not ok:
            failed += 1

    # 1) 分类口径：文件名优先，正文特征兜底
    classify_cases = [
        (skills / "good-minimal" / "SKILL.md", "skill"),
        (skills / "good-minimal", "skill"),
        (examples / "需求确认" / "例1-下载文件夹整理.需求确认.md", "confirm"),
        (examples / "例1-下载文件夹整理.prompt.md", "prompt"),
        (repo / "templates" / "提示词骨架.md", "prompt"),
        (repo / "templates" / "需求确认.md", "confirm"),
    ]
    for path, want in classify_cases:
        got = detect_kind(path)
        report_case(f"识别 {path.name} → {want}", got == want, f"实际 {got}")

    # 1b) 正文特征兜底：文件名不叫「需求确认」时也要认得出（变异检验靠这条抓）
    with tempfile.TemporaryDirectory() as tmp:
        neutral = Path(tmp) / "spec-doc.md"
        neutral.write_bytes(
            (examples / "需求确认" / "例1-下载文件夹整理.需求确认.md").read_bytes())
        got = detect_kind(neutral)
        report_case("正文特征识别需求确认（文件名不叫需求确认）", got == "confirm", f"实际 {got}")

    # 1c) 认不出的目标不能 0 闸门静默「通过」（第 9 轮独立验证 V9-PF-01）
    with tempfile.TemporaryDirectory() as tmp:
        stray = Path(tmp) / "notes.txt"
        stray.write_bytes(b"hello\n")
        target = check_target(stray)
        code = exit_code(target.errors, target.warns, False)
        report_case("认不出的文件报 E0 且 rc=1", target.errors > 0 and code == 1,
                    f"{target.errors} ERROR / rc={code}")
        report_case("认不出的文件被标成 unknown", detect_kind(stray) == "unknown")
        empty = Path(tmp) / "empty-dir"
        empty.mkdir()
        report_case("空目录仍报 E0", check_target(empty).errors > 0)

    # 2) 合格资产：0 ERROR（WARN 不阻断）
    clean_cases = [
        examples / "例1-下载文件夹整理.prompt.md",
        examples / "例2-班级报名网站.prompt.md",
        examples / "例3-logsift-json-export.prompt.md",
        examples / "需求确认" / "例1-下载文件夹整理.需求确认.md",
        skills / "good-minimal" / "SKILL.md",
        skills / "good-minimal",                    # 目录形态：自动落到目录里的 SKILL.md
        skills / "evidence-images" / "SKILL.md",
    ]
    for path in clean_cases:
        target = check_target(path)
        report_case(f"合格资产 0 ERROR：{path.name}", target.errors == 0,
                    f"{target.errors} ERROR / {target.warns} WARN")

    # 3) 反例必须被拦：ERROR > 0 且退出码 1
    bad_cases = [
        examples / "反例-模糊需求.prompt.md",
        examples / "需求确认" / "反例-需求确认缺证据链.需求确认.md",
        skills / "evidence-fake" / "SKILL.md",
        skills / "no-frontmatter" / "SKILL.md",
    ]
    for path in bad_cases:
        target = check_target(path)
        code = exit_code(target.errors, target.warns, False)
        report_case(f"反例被拦：{path.name}", target.errors > 0 and code == 1,
                    f"{target.errors} ERROR / {target.warns} WARN / rc={code}")

    # 4) --json 可解析，且 totals 与目标结果一致
    payload = build_payload([check_target(examples / "例1-下载文件夹整理.prompt.md")], False)
    try:
        parsed = json.loads(json.dumps(payload, ensure_ascii=False))
        ok_json = (parsed["version"] == 1 and parsed["totals"]["targets"] == 1
                   and parsed["exit_code"] == 0
                   and all(gate["label"] for target in parsed["targets"] for gate in target["gates"]))
    except (TypeError, ValueError):
        ok_json = False
    report_case("--json 结构可解析且字段齐全", ok_json)

    # 5) 退出码口径：ERROR→1；只有 WARN→0；--strict 时 WARN→1
    report_case("退出码：有 ERROR 必 1", exit_code(1, 0, False) == 1)
    report_case("退出码：只有 WARN 默认 0", exit_code(0, 2, False) == 0)
    report_case("退出码：--strict 时 WARN 也失败", exit_code(0, 2, True) == 1)

    # 6) 目录模式：递归找目标，不把 README 之类的普通 .md 当提示词
    dir_results = expand_targets([examples])
    dir_names = [target.path.name for target in dir_results]
    ok_dir = (len(dir_results) >= 6 and "README.md" not in dir_names
              and any(name.endswith(".prompt.md") for name in dir_names)
              and sum(1 for target in dir_results if target.kind == "skill") >= 9)
    report_case("目录模式递归找目标且跳过 README", ok_dir, f"{len(dir_results)} 个目标")

    # 6b) 目录里有 junction 指回祖先时不能无限递归（第 9 轮独立验证 V9-PF-02）。
    #     Linux 用符号链接；Windows 没有开发者模式建不了符号链接，但 junction 不需要权限。
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
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
        except OSError:
            return False
        return proc.returncode == 0 and link.is_dir()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tree"
        (root / "sub").mkdir(parents=True)
        (root / "a.prompt.md").write_bytes(b"x\n")
        if make_dir_link(root, root / "sub" / "back"):
            loop_results = expand_targets([root])
            names = [target.path.name for target in loop_results]
            report_case("目录里的 junction 循环不会无限递归/重复",
                        names == ["a.prompt.md"], f"{names[:5]}（共 {len(names)} 个目标）")
        else:
            print("SKIP  [单入口] junction 循环用例：当前环境建不了目录链接")

    # 7) 真 CLI：子进程跑一次合格资产（rc 0）与反例（rc 1），并验证 --json 只有 JSON
    script = Path(__file__).resolve()
    for path, want_rc, label in (
        (examples / "例1-下载文件夹整理.prompt.md", 0, "CLI 合格资产 rc=0"),
        (examples / "反例-模糊需求.prompt.md", 1, "CLI 反例 rc=1"),
    ):
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(script), str(path)],
            cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        out = proc.stdout.decode("utf-8", "replace")
        report_case(label, proc.returncode == want_rc,
                    f"rc={proc.returncode}（期望 {want_rc}）")
        if not (proc.returncode == want_rc):
            print("      " + out.strip().replace("\n", "\n      "))
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(script), "--json",
         str(examples / "例1-下载文件夹整理.prompt.md")],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    try:
        json.loads(proc.stdout.decode("utf-8", "replace"))
        ok_cli_json = proc.returncode == 0
    except ValueError:
        ok_cli_json = False
    report_case("CLI --json 的 stdout 是纯 JSON", ok_cli_json)

    # 8) 路径不存在：rc=2；--json 时 stdout 仍必须是纯 JSON（第 9 轮独立验证 V9-PF-03）
    missing_path = str((examples / "不存在的文件.prompt.md").resolve())
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(script), missing_path],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    report_case("CLI 路径不存在 rc=2", proc.returncode == 2,
                f"rc={proc.returncode}（期望 2）")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(script), "--json",
         str(examples / "例1-下载文件夹整理.prompt.md"), missing_path],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    try:
        parsed = json.loads(proc.stdout.decode("utf-8", "replace"))
        ok_missing_json = (proc.returncode == 2 and parsed.get("missing") == [missing_path]
                          and parsed.get("exit_code") == 2
                          and parsed.get("totals", {}).get("targets") == 1)
    except ValueError:
        ok_missing_json = False
    report_case("CLI --json + 缺路径：stdout 纯 JSON 且 exit_code=2", ok_missing_json,
                f"rc={proc.returncode}")

    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    _setup_stdout()
    parser = argparse.ArgumentParser(
        description="prompt-forge 单入口：自动识别目标，依次跑对应的零依赖闸门")
    parser.add_argument("paths", nargs="*", help="要检查的文件或目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（stdout 只有 JSON）")
    parser.add_argument("--strict", action="store_true", help="只有 WARN 时也返回非零")
    parser.add_argument("--selftest", action="store_true", help="用仓库里的合格资产与反例自测")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.paths:
        parser.print_help()
        return 2
    results: list[TargetResult] = []
    missing: list[str] = []
    for name in args.paths:
        path = Path(name)
        if not path.exists():
            missing.append(str(path))
            continue
        results.extend(expand_targets([path]))
    if args.json:
        # 第 9 轮独立验证 V9-PF-03：--json 的 stdout 必须始终是纯 JSON；缺路径的信息进
        # `missing` 字段、用 exit_code=2 表达，不能先打一行人类可读文本再去解析失败。
        payload = build_payload(results, args.strict, missing)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload["exit_code"]
    if missing:
        for name in missing:
            print(f"ERROR: 文件或目录不存在：{name}")
        return 2
    return print_summary(results, args.strict)


if __name__ == "__main__":
    _setup_stdout()
    raise SystemExit(main())
