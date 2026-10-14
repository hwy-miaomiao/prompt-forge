#!/usr/bin/env python3
"""变异检验：证明三道闸门的 ``--selftest`` 真的在看着代码，而不是空跑。

用法::

    python scripts/mutate_checks.py              # 跑全部变异
    python scripts/mutate_checks.py --list       # 只列出变异名，不跑
    python scripts/mutate_checks.py C2 围栏      # 只跑名字里含这些子串的变异

原理（每一步都不能省）：

1. 把仓库复制一份到临时目录，镜像目录名必须是 ``prompt-forge`` —— 闸门② 的 E3 要求
   frontmatter 的 ``name`` 与所在目录名一致，镜像换个名字会让「真实 SKILL.md」那条
   用例先红掉，掩盖我们真正想看的那条用例。
2. 先在未变异的镜像上跑一次 ``--selftest``，必须 rc=0。基线不是绿的，后面「红了」
   什么都说明不了。
3. 对每个变异：把某一行换成**故意写错的一行** → 跑该脚本的 ``--selftest`` →
   **必须 rc≠0**，并打印被抓到的失败用例名（证明是自测抓到的，不是脚本自己崩了）。
4. 全部变异都被抓到 → rc=0；任何一个变异跑完还是 rc=0（自测没抓住）→ rc=1。
   变异点在源码里找不到或不是唯一一处（说明代码改过、变异脚本过期了）→ 也算失败。

为什么不用 ``unittest.mock`` 或改 AST：这是给「改闸门规则的人」用的纪律工具，
要在任何 Python 3.9+ 上跑；而且变异必须是**人读得懂的文本 diff** —— 评审时谁都看得出
我们把哪一行改坏了、哪条用例因此变红。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 镜像目录名不能改：闸门② 的 E3 拿它跟 frontmatter 的 name 比对。
MIRROR_DIR_NAME = "prompt-forge"

#: 三个闸门的 ``--selftest`` 都要用到的仓库资产（少一样就会多出一条与变异无关的 FAIL）。
COPY_ITEMS = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "SKILL.md",
    "examples",
    "references",
    "scripts",
    "templates",
)

IGNORE_COPY = shutil.ignore_patterns("__pycache__", "*.pyc")


class Mutation:
    """一处变异：把 ``old`` 换成 ``new`` 之后，自测必须变红。"""

    def __init__(self, name: str, script: str, old: str, new: str) -> None:
        self.name = name
        self.script = script
        self.old = old
        self.new = new

    def __str__(self) -> str:
        return f"{self.script}: {self.name}"


MUTATIONS: list[Mutation] = [
    # ---------- 闸门①：长提示词 ----------
    Mutation(
        "围栏里的标题不算段落", "check_prompt.py",
        "        if fenced[idx]:\n            continue\n        if HEADING_RE.match(line):",
        "        if False:\n            continue\n        if HEADING_RE.match(line):",
    ),
    Mutation(
        "围栏里的条目不算条目", "check_prompt.py",
        "        if fenced[idx]:\n            continue\n        num = item_start(line, letter)",
        "        if False:\n            continue\n        num = item_start(line, letter)",
    ),
    Mutation(
        "围栏开关恒为假", "check_prompt.py",
        "        flags.append(fence is not None)",
        "        flags.append(False)",
    ),
    Mutation(
        "验收只认条目行上的引用", "check_prompt.py",
        "        refs = declared_refs(block, num)",
        "        refs = declared_refs(block[:1], num)",
    ),
    Mutation(
        "E9 退回只查绑定段", "check_prompt.py",
        "        ranges = list(suspects.get(label, ()))",
        "        ranges = []",
    ),
    Mutation(
        "排序键：段内真有条目退到第二键", "check_prompt.py",
        "            return (not c[5], not c[3], not c[4], c[0], c[2])",
        "            return (not c[3], not c[5], not c[4], c[0], c[2])",
    ),
    Mutation(
        "豁免注释被当成条目正文", "check_prompt.py",
        "        body = strip_comments(item_body(text))",
        "        body = item_body(text)",
    ),
    # ---------- 闸门②：技能定义 ----------
    Mutation(
        "ASCII 标记退回折叠子串（good 命中 go）", "check_skill.py",
        "    if ASCII_MARKER_RE.fullmatch(marker):",
        "    if False:",
    ),
    Mutation(
        "三态接受词退回裸「接（」（不接（kill）也算接）", "check_skill.py",
        'TRIAGE_ACCEPT_MARKERS = [r"re:(?<!不)接[（(]", "go", "needs-clarification"]',
        'TRIAGE_ACCEPT_MARKERS = ["接（", "go", "needs-clarification"]',
    ),
    Mutation(
        "目录名退回词法路径（带 .. 时误报 E3）", "check_skill.py",
        "        dirname = path.resolve().parent.name",
        "        dirname = path.parent.name",
    ),
    Mutation(
        "触发词加回裸字「当」", "check_skill.py",
        'WHEN_MARKERS = [\n    "如果", "若用户", "用户说", "用户给出", "用户抛", "你需要",',
        'WHEN_MARKERS = [\n    "当",\n    "如果", "若用户", "用户说", "用户给出", "用户抛", "你需要",',
    ),
    # ---------- 闸门③：跨段一致性 ----------
    Mutation(
        "C1 不查不存在的编号", "check_contract.py",
        "                if num not in known[letter]:",
        "                if False:",
    ),
    Mutation(
        "C2 不再报没被触及的需求", "check_contract.py",
        "    ) for num in sorted(requirements - seen)]",
        "    ) for num in []]",
    ),
    Mutation(
        "C4 一律认为对得上", "check_contract.py",
        "        if keywords(text) & known:",
        "        if True:",
    ),
    Mutation(
        "C5 认为命令永远给了", "check_contract.py",
        "    return bool(cp.CODE_SPAN_RE.search(text) or SCRIPT_RE.search(text)"
        " or COMMAND_HINT_RE.search(text))",
        "    return True",
    ),
]


def build_mirror(dest: Path) -> Path:
    """把仓库拷成一份可随意改坏的镜像，返回镜像根。"""
    mirror = dest / MIRROR_DIR_NAME
    mirror.mkdir(parents=True)
    for item in COPY_ITEMS:
        src = ROOT / item
        if not src.exists():
            raise SystemExit(f"ERROR: 仓库里找不到 {item}，建不出变异镜像")
        if src.is_dir():
            shutil.copytree(src, mirror / item, ignore=IGNORE_COPY)
        else:
            shutil.copy2(src, mirror / item)
    return mirror


def run_selftest(mirror: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-B", str(mirror / "scripts" / script), "--selftest"],
        cwd=str(mirror),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def failed_case_names(output: str) -> list[str]:
    """从自测输出里摘出 FAIL 用例名，让「被抓到了」这句话有据可依。"""
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAIL"):
            body = stripped[4:].strip()
            names.append(body.split(":", 1)[0])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="变异检验：确认三道闸门的 --selftest 不是空跑")
    parser.add_argument("filters", nargs="*", help="只跑名字里含这些子串的变异")
    parser.add_argument("--list", action="store_true", help="只列出变异名，不跑")
    args = parser.parse_args()

    selected = [
        m for m in MUTATIONS
        if not args.filters or any(f.lower() in m.name.lower() for f in args.filters)
    ]
    if args.list:
        for mutation in selected:
            print(f"{mutation.script}\t{mutation.name}")
        return 0
    if not selected:
        print("没有名字匹配的变异，检查一下过滤词")
        return 1

    scripts = list(dict.fromkeys(m.script for m in selected))
    print(f"变异检验：{len(selected)} 个变异，涉及 {len(scripts)} 个脚本")

    failed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pf-mutate-") as tmp:
        # 1) 基线：未变异的自测必须全绿。
        print("\n--- 基线（未变异）---")
        baseline = build_mirror(Path(tmp) / "baseline")
        for script in scripts:
            proc = run_selftest(baseline, script)
            ok = proc.returncode == 0
            tail = [ln for ln in proc.stdout.splitlines() if ln.startswith("自测结果")]
            print(f"{'PASS' if ok else 'FAIL'}  {script} --selftest: rc={proc.returncode}"
                  f"{'，' + tail[0] if tail else ''}")
            if not ok:
                failed.append(f"基线 {script}")
                print(proc.stdout[-2000:])
                print(proc.stderr[-2000:])
        if failed:
            print("\n基线不是绿的：先把 --selftest 修绿再谈变异检验。")
            return 1

        # 2) 逐个变异：改坏一行 → 自测必须变红。
        print("\n--- 变异 ---")
        for index, mutation in enumerate(selected, 1):
            mirror = build_mirror(Path(tmp) / f"m{index:02d}")
            target = mirror / "scripts" / mutation.script
            text = target.read_text(encoding="utf-8")
            hits = text.count(mutation.old)
            if hits != 1:
                print(f"FAIL  [{index:02d}] {mutation}: 变异点在源码里出现 {hits} 次"
                      f"（要求恰好 1 次）—— 代码改过，变异脚本要跟着更新")
                failed.append(mutation.name)
                continue
            target.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")

            proc = run_selftest(mirror, mutation.script)
            caught = proc.returncode != 0
            names = failed_case_names(proc.stdout)
            if caught:
                shown = "、".join(names[:3]) if names else "（没有 FAIL 行，可能是脚本直接崩了）"
                more = f" 等 {len(names)} 条" if len(names) > 3 else ""
                print(f"PASS  [{index:02d}] {mutation}: rc={proc.returncode}，抓到 {shown}{more}")
            else:
                print(f"FAIL  [{index:02d}] {mutation}: rc=0 —— 自测没抓住这个变异")
                failed.append(mutation.name)
                print("      建议：给这个脚本的 --selftest 补一条会因此变红的用例")

    if failed:
        print(f"\n结果：{len(failed)} 个变异没被抓住或没能生效 —— {', '.join(failed)}")
        return 1
    print(f"\n结果：{len(selected)} 个变异全部被自测抓住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
