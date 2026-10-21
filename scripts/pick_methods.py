#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-forge 的问询方法推荐器：按需求特征挑这次问询该用哪些方法。

阶段 2 的问题不是「问几个」，而是「用哪种问法问」：
同样是「交付物说不清」，Web 类需求适合给参照物，重构类需求适合先画边界；
同样是缺验收口径，脚本类给命令、文档类给样例。这份映射就是把这层判断从
「每次凭感觉」变成「可复用的方法清单」。

数据与判据的唯一权威是 references/需求问询-方法映射.md（方法名 / 适用信号 /
一次问几个 / 失败退路 / 默认档都在那边有对照表）。改这里要同步改那边，
反之亦然 —— 文件头也写了这句话。

用法：
    python scripts/pick_methods.py --shape web --gaps output,acceptance
    python scripts/pick_methods.py --shape refactor --risk data-loss
    echo "web output acceptance" | python scripts/pick_methods.py -
    python scripts/pick_methods.py --json --shape cli --gaps constraints
    python scripts/pick_methods.py --list
    python scripts/pick_methods.py --selftest

输入维度（--gaps）取「静默九维扫描」里缺的那几维，或阶段 2 的补充维：
    output input constraints context audience memory success examples
    acceptance non-goal reference style data risk performance scale
    security interop workflow scope recency model
没写 --gaps 时按 --shape 的默认方法给一套起步清单。

退出码：参数不合法（未知 shape、完全无输入）非零退出；正常查询 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------- 方法库（权威）

#: 每条方法：id / 名称 / 适用信号（缺口维度）/ 一次问几个 / 失败退路 / 默认档。
#: 默认档三档：必问（不问就会写出没法开工的提示词）/ 建议（问不到就按默认值写进第 10 段）
#: / 可选（用户愿意深聊时才展开）。档位不是「重要性排序」，是「要不要打断用户」。
METHODS: list[dict] = [
    {
        "id": "M01",
        "name": "五问定位",
        "signals": ["output", "audience", "success", "examples", "base"],
        "asks": 3,
        "fallback": "用户答不上来时给 2~3 个具体选项，并给一个默认值写进第 10 段",
        "tier": "必问",
        "why": "所有需求的地基：交付物形态、谁用、完成度、参照物、绝对不做什么",
    },
    {
        "id": "M02",
        "name": "反向问（非目标）",
        "signals": ["non-goal", "scope"],
        "asks": 1,
        "fallback": "给 3 条常见反向选项让用户勾（不做界面 / 不接数据库 / 不做权限）",
        "tier": "必问",
        "why": "用户只说了要什么时，第 5 段最容易写成「无」—— 反向问比正面追问好回答",
    },
    {
        "id": "M03",
        "name": "参照物锚定",
        "signals": ["reference", "style", "examples"],
        "asks": 1,
        "fallback": "拿同类产品 / 同类文件的 URL 当参考，问「像它哪一点」",
        "tier": "建议",
        "why": "「好看 / 好用 / 专业」这类形容词只有落到具体参照物才可判定",
    },
    {
        "id": "M04",
        "name": "反例锚定",
        "signals": ["reference", "non-goal"],
        "asks": 1,
        "fallback": "给一段反例描述让用户指出哪里不像，把「不像」写成非目标",
        "tier": "可选",
        "why": "用户能说清「别做成什么样」却说不出需求时，反例比正例更好用",
    },
    {
        "id": "M05",
        "name": "数值标尺",
        "signals": ["performance", "scale", "acceptance"],
        "asks": 1,
        "fallback": "给数量级选项（100 / 1 千 / 1 万 / 10 万），让用户挑一个量级",
        "tier": "建议",
        "why": "「快 / 能扛住」必须换成可判定的数字，否则验收 A# 只能写「人工」",
    },
    {
        "id": "M06",
        "name": "场景走查",
        "signals": ["context", "workflow", "audience"],
        "asks": 2,
        "fallback": "让用户按真实一天的操作顺序走一遍，漏掉的步骤补进第 9 段",
        "tier": "建议",
        "why": "多角色 / 多步骤流程最容易漏路径，走一遍比问「还有什么」有效",
    },
    {
        "id": "M07",
        "name": "约束清单核对",
        "signals": ["constraints", "input"],
        "asks": 2,
        "fallback": "先报一套默认技术栈，问「有没有必须用的 / 必须不用的」",
        "tier": "必问",
        "why": "版本、依赖、运行环境写不具体，第 6 段就会变成「最新版」式的空话",
    },
    {
        "id": "M08",
        "name": "数据来源与去向",
        "signals": ["input", "output", "data"],
        "asks": 2,
        "fallback": "给出输入样本与输出样例的位置让用户确认，避免替用户猜格式",
        "tier": "必问",
        "why": "要读 / 写文件、调 API、碰数据库时，格式与去向写不清就无法验证",
    },
    {
        "id": "M09",
        "name": "失败兜底",
        "signals": ["risk", "reliability"],
        "asks": 1,
        "fallback": "给 3 种兜底策略（跳过 / 报错停 / 回滚）让用户选",
        "tier": "建议",
        "why": "有数据丢失 / 不可逆动作 / 线上影响时，第 11 段必须写清停下来问人的条件",
    },
    {
        "id": "M10",
        "name": "九维非功能扫描",
        "signals": ["performance", "security", "reliability", "compatibility"],
        "asks": 3,
        "fallback": "按 ISO/IEC 25010 九维给默认档，只把偏离默认的维度转成问题",
        "tier": "建议",
        "why": "性能 / 安全 / 兼容这类非功能项用户几乎不会主动提，漏了就是返工点",
    },
    {
        "id": "M11",
        "name": "范围切分",
        "signals": ["scope", "size"],
        "asks": 2,
        "fallback": "先做一条端到端最小路径，其余明确写进第 5 段非目标",
        "tier": "建议",
        "why": "需求明显超过一句话能做完时，不问边界就会得到一份没人能执行的提示词",
    },
    {
        "id": "M12",
        "name": "接口 / 字段草案",
        "signals": ["data", "interop"],
        "asks": 2,
        "fallback": "先画 3~5 个关键字段让用户改，比让用户从零描述容易",
        "tier": "建议",
        "why": "产出要对接已有系统、或数据结构不确定时，字段名与类型必须先钉住",
    },
    {
        "id": "M13",
        "name": "验收口径",
        "signals": ["acceptance", "success"],
        "asks": 2,
        "fallback": "给「能跑 / 结果对 / 可交付」三档让用户选，再落成 A# 的判定三取值",
        "tier": "必问",
        "why": "「怎么算做完」说不清，第 7 段就只能靠执行者自己定义完成",
    },
    {
        "id": "M14",
        "name": "权限与安全边界",
        "signals": ["security", "risk"],
        "asks": 2,
        "fallback": "按最小权限给默认（只读 / 只在指定目录写），让用户确认例外",
        "tier": "建议",
        "why": "碰隐私数据、他人电脑、多用户时，权限边界是提示词里最容易缺席的一段",
    },
    {
        "id": "M15",
        "name": "决策记录",
        "signals": ["memory", "tradeoff"],
        "asks": 1,
        "fallback": "把被放弃的选项与原因写进需求确认「假设与开放问题」，别让它消失",
        "tier": "可选",
        "why": "问询里的取舍如果不落盘，下一轮迭代会把同一个问题再问一遍",
    },
    {
        "id": "M16",
        "name": "模型 / 版本核验",
        "signals": ["model", "recency"],
        "asks": 1,
        "fallback": "查官方文档；查不到就写「未确认」，绝不编模型名与版本号",
        "tier": "必问",
        "why": "涉及模型名 / API 默认值时，写错版本比写「未确认」危险得多（Model Recency Gate）",
    },
]

#: 需求形态 → 起步方法。形态没覆盖到的缺口再由 --gaps 补。
SHAPES: dict[str, list[str]] = {
    "web": ["M01", "M02", "M03", "M05", "M07", "M08", "M10", "M13"],
    "cli": ["M01", "M02", "M05", "M07", "M08", "M13"],
    "data": ["M01", "M02", "M05", "M08", "M12", "M13"],
    "doc": ["M01", "M02", "M03", "M06", "M13"],
    "skill": ["M01", "M02", "M03", "M07", "M13", "M16"],
    "refactor": ["M01", "M02", "M07", "M09", "M10", "M11"],
    "automation": ["M01", "M02", "M06", "M08", "M09", "M13"],
    "general": ["M01", "M02", "M07", "M13"],
}

#: --gaps 里人类爱写的词 → 方法库的信号维度。一个词只映射到一个主维度，
#: 具体选哪些方法由方法的 signals 决定（保持单一事实来源）。
GAP_ALIASES: dict[str, str] = {
    "output": "output",
    "交付物": "output",
    "产物": "output",
    "input": "input",
    "输入": "input",
    "constraints": "constraints",
    "技术栈": "constraints",
    "约束": "constraints",
    "环境": "constraints",
    "context": "context",
    "场景": "context",
    "audience": "audience",
    "用户": "audience",
    "memory": "memory",
    "记忆": "memory",
    "success": "success",
    "完成度": "success",
    "examples": "examples",
    "参照物": "reference",
    "reference": "reference",
    "style": "style",
    "风格": "style",
    "acceptance": "acceptance",
    "验收": "acceptance",
    "non-goal": "non-goal",
    "nongoal": "non-goal",
    "非目标": "non-goal",
    "data": "data",
    "数据": "data",
    "risk": "risk",
    "风险": "risk",
    "performance": "performance",
    "性能": "performance",
    "scale": "scale",
    "规模": "scale",
    "security": "security",
    "安全": "security",
    "reliability": "reliability",
    "可靠性": "reliability",
    "compatibility": "compatibility",
    "兼容": "compatibility",
    "interop": "interop",
    "对接": "interop",
    "workflow": "workflow",
    "流程": "workflow",
    "scope": "scope",
    "范围": "scope",
    "size": "size",
    "体量": "size",
    "tradeoff": "tradeoff",
    "取舍": "tradeoff",
    "recency": "recency",
    "最新": "recency",
    "model": "model",
    "模型": "model",
}

#: 风险档 → 强制升到「必问」的方法（数据丢失 / 不可逆动作必须有兜底与安全边界）。
RISK_ESCALATE: dict[str, list[str]] = {
    "data-loss": ["M09", "M14"],
    "data": ["M09", "M14"],
    "数据丢失": ["M09", "M14"],
    "irreversible": ["M09", "M14", "M02"],
    "不可逆": ["M09", "M14", "M02"],
    "security": ["M14"],
    "安全": ["M14"],
}

#: 档位排序：必问在前。
_TIER_ORDER = {"必问": 0, "建议": 1, "可选": 2}


def _norm_token(token: str) -> str:
    return token.strip().lower().replace("_", "-").replace(" ", "-")


def method_by_id(method_id: str) -> dict | None:
    for method in METHODS:
        if method["id"] == method_id:
            return method
    return None


def resolve_gaps(tokens: list[str]) -> tuple[set[str], list[str]]:
    """把用户写的缺口词换算成方法信号维度；不认识的词原样放进 warnings。

    规则：认 GAP_ALIASES 里的别名；也直接认方法库里出现过的 signal（`base` 这类内部信号除外）。
    不因为一个拼错的词就整份查询失败 —— 打印警告并继续，让调用者自己决定要不要修。
    """
    known = set()
    for method in METHODS:
        known.update(method["signals"])
    dims: set[str] = set()
    warnings: list[str] = []
    for raw in tokens:
        token = _norm_token(raw)
        if not token:
            continue
        if token in GAP_ALIASES:
            dims.add(GAP_ALIASES[token])
        elif token in known:
            dims.add(token)
        else:
            warnings.append(f"不认识的需求维度：{raw.strip()}（已忽略；可用 --list 看方法信号）")
    return dims, warnings


def select_methods(shape: str, gaps: list[str], risk: str = "") -> tuple[list[dict], list[str]]:
    """返回 (methods, warnings)。methods 已按「必问 → 建议 → 可选 / 编号」排好序。"""
    warnings: list[str] = []
    shape_key = _norm_token(shape) if shape else "general"
    if shape_key not in SHAPES:
        raise ValueError(f"未知需求形态：{shape}；可选：{'、'.join(SHAPES)}")
    dims, gap_warnings = resolve_gaps(gaps)
    warnings.extend(gap_warnings)

    chosen: dict[str, dict] = {}

    def add(method_id: str, reason: str) -> None:
        method = method_by_id(method_id)
        if method is None:  # pragma: no cover - 方法库自检兜底
            raise ValueError(f"方法库里没有 {method_id}")
        if method_id not in chosen:
            chosen[method_id] = {
                "id": method_id,
                "name": method["name"],
                "tier": method["tier"],
                "asks": method["asks"],
                "fallback": method["fallback"],
                "why": method["why"],
                "matched": [],
            }
        chosen[method_id]["matched"].append(reason)

    if not dims:
        # 没给缺口维度：只给形态的起步清单，别假装知道用户缺什么。
        for method_id in SHAPES[shape_key]:
            add(method_id, f"{shape_key} 形态起步清单")
    else:
        for method in METHODS:
            hit = dims.intersection(method["signals"])
            if hit:
                add(method["id"], "缺口：" + "、".join(sorted(hit)))

    risk_key = _norm_token(risk) if risk else ""
    if risk_key:
        if risk_key not in RISK_ESCALATE:
            warnings.append(f"不认识的风险档：{risk}（已忽略；可用：data-loss / irreversible / security）")
        else:
            for method_id in RISK_ESCALATE[risk_key]:
                add(method_id, f"风险档 {risk_key} 强制展开")

    # 主维度命中「非功能」时补九维扫描；要求的方法不够 2 条时兜底到形态起步清单，
    # 避免输出一份只有 1 条方法的「清单」。
    if len(chosen) < 2:
        for method_id in SHAPES[shape_key]:
            add(method_id, f"{shape_key} 形态起步清单（缺口命中过少兜底）")

    if risk_key and risk_key in RISK_ESCALATE:
        # 风险档命中的方法一律升「必问」。旧实现只对 data-loss / irreversible 硬编码 M09/M14，
        # security→M14 在映射表里写了、代码却没生效（独立验证 F6）。
        for method_id in RISK_ESCALATE[risk_key]:
            if method_id in chosen:
                chosen[method_id]["tier"] = "必问"

    methods = sorted(chosen.values(), key=lambda m: (_TIER_ORDER.get(m["tier"], 9), m["id"]))
    return methods, warnings


def render_text(shape: str, gaps: list[str], risk: str, methods: list[dict], warnings: list[str]) -> str:
    feature = f"形态={_norm_token(shape) or 'general'}"
    if gaps:
        feature += f"，缺口={'、'.join(g.strip() for g in gaps if g.strip())}"
    if risk:
        feature += f"，风险={risk.strip()}"
    lines = [f"推荐问询方法（{feature}）", ""]
    for index, method in enumerate(methods, 1):
        matched = "；".join(dict.fromkeys(method["matched"]))
        lines.append(f"{index}. {method['id']} {method['name']}［{method['tier']}］")
        lines.append(f"   为什么：{method['why']}（命中：{matched}）")
        lines.append(f"   一次问几个：{method['asks']} 题；失败退路：{method['fallback']}")
    lines.append("")
    lines.append("用法：按上面的顺序拼进阶段 2 的问询；标「建议」的方法问不到就按默认值写进第 10 段，")
    lines.append("标「可选」的方法只在用户愿意深聊时展开；问完把方法名与题号写进 需求确认.md 的问询表。")
    if warnings:
        lines.append("")
        for warning in warnings:
            lines.append(f"警告：{warning}")
    return "\n".join(lines)


def render_json(shape: str, gaps: list[str], risk: str, methods: list[dict], warnings: list[str]) -> str:
    payload = {
        "shape": _norm_token(shape) or "general",
        "gaps": [g.strip() for g in gaps if g.strip()],
        "risk": risk.strip(),
        "methods": methods,
        "warnings": warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_library_json() -> str:
    """`--list --json`：方法库本身（独立验证 F7：旧版 `--list` 早于 `--json`，永远吐纯文本）。"""
    payload = [
        {
            "id": method["id"],
            "name": method["name"],
            "tier": method["tier"],
            "signals": method["signals"],
            "asks": method["asks"],
            "fallback": method["fallback"],
        }
        for method in METHODS
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def read_features(argv_rest: list[str]) -> list[str]:
    """从命令行剩余参数或 stdin 里读特征词（`python pick_methods.py -` 用）。"""
    if argv_rest and argv_rest[0] != "-":
        return argv_rest
    if not sys.stdin.isatty():
        return sys.stdin.read().replace(",", " ").split()
    return []


def selftest() -> int:
    """自测：钉住映射规则与输出形状，防止改方法库时静默改变推荐结果。"""
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        print(f"{'PASS' if ok else 'FAIL'}  [pick_methods] {name}{('：' + detail) if detail else ''}")
        if not ok:
            failed += 1

    cases: list[tuple[str, str, list[str], str, list[str], dict[str, str]]] = [
        # name, shape, gaps, risk, 必须出现的方法, {方法: 期望档}
        ("Web 形态起步清单", "web", [], "", ["M01", "M07", "M13"], {"M01": "必问", "M10": "建议"}),
        ("缺口命中：交付物 + 验收", "web", ["output", "acceptance"], "",
         ["M01", "M13"], {"M01": "必问", "M13": "必问"}),
        ("缺口别名：中文「性能」", "cli", ["性能"], "", ["M05", "M10"], {}),
        ("缺口别名：中文「非目标」", "general", ["非目标"], "", ["M02"], {"M02": "必问"}),
        ("重构形态带风险：强制兜底 + 安全边界", "refactor", ["constraints"], "data-loss",
         ["M09", "M14"], {"M09": "必问", "M14": "必问"}),
        ("模型维度强制核验", "skill", ["model"], "", ["M16"], {"M16": "必问"}),
        ("未知形态报错", "no-such-shape", [], "", [], {}),
        ("未知缺口词只给警告不炸", "web", ["no-such-gap"], "", ["M01"], {}),
        ("未知风险档只给警告", "web", [], "no-such-risk", ["M01"], {}),
        # F6（独立验证）：映射表说 security→M14 升必问，旧实现只硬编码 data-loss / irreversible。
        ("security 风险把 M14 升必问", "web", [], "security", ["M14"], {"M14": "必问"}),
    ]
    for name, shape, gaps, risk, must_have, tiers in cases:
        try:
            methods, warnings = select_methods(shape, gaps, risk)
        except ValueError as exc:
            if name == "未知形态报错":
                check(name, True, str(exc))
            else:
                check(name, False, f"意外抛错：{exc}")
            continue
        if name == "未知形态报错":
            check(name, False, "应当抛 ValueError 却没有抛")
            continue
        ids = [method["id"] for method in methods]
        ok = all(method_id in ids for method_id in must_have)
        detail = f"拿到 {ids}"
        for method_id, want_tier in tiers.items():
            got = next((m["tier"] for m in methods if m["id"] == method_id), None)
            if got != want_tier:
                ok = False
                detail += f"；{method_id} 档位 {got}（期望 {want_tier}）"
        if name == "未知缺口词只给警告不炸":
            ok = ok and len(warnings) == 1 and "no-such-gap" in warnings[0]
            detail += f"；警告 {warnings}"
        if name == "未知风险档只给警告":
            ok = ok and any("no-such-risk" in warning for warning in warnings)
            detail += f"；警告 {warnings}"
        check(name, ok, detail)

    # 输出形状：文本要给档位、一次问几个、失败退路；JSON 必须能被 json.loads。
    methods, warnings = select_methods("web", ["output"], "")
    text = render_text("web", ["output"], "", methods, warnings)
    ok_text = all(token in text for token in ("推荐问询方法", "［必问］", "一次问几个", "失败退路"))
    check("文本输出含档位 / 题数 / 退路", ok_text)
    try:
        payload = json.loads(render_json("web", ["output", "acceptance"], "", methods, warnings))
        ok_json = (
            payload["shape"] == "web"
            and payload["gaps"] == ["output", "acceptance"]
            and len(payload["methods"]) >= 2
            and all({"id", "name", "tier", "asks", "fallback"} <= set(item) for item in payload["methods"])
        )
        check("JSON 输出可解析且字段齐全", ok_json)
    except (ValueError, KeyError) as exc:  # pragma: no cover - 解析失败即 FAIL
        check("JSON 输出可解析且字段齐全", False, str(exc))

    # F7（独立验证）：`--list --json` 必须是 JSON 数组，不能因为 --list 分支在前就吐纯文本。
    try:
        library = json.loads(render_library_json())
        ok_library_json = (
            isinstance(library, list)
            and len(library) == len(METHODS)
            and all({"id", "name", "tier", "signals", "asks", "fallback"} <= set(item) for item in library)
        )
        check("--list --json 输出方法库 JSON 数组", ok_library_json)
    except (ValueError, KeyError, TypeError) as exc:  # pragma: no cover - 解析失败即 FAIL
        check("--list --json 输出方法库 JSON 数组", False, str(exc))

    # 方法库自检：id 唯一、每条 signals 都能在 GAP_ALIASES 的值域里找到、档位合法。
    ids = [method["id"] for method in METHODS]
    known_dims = set(GAP_ALIASES.values())
    ok_library = (
        len(ids) == len(set(ids))
        and all(method["tier"] in _TIER_ORDER for method in METHODS)
        and all(set(method["signals"]) <= known_dims | {"base"} for method in METHODS)
    )
    check("方法库自检（id 唯一 / 档位合法 / 信号可解析）", ok_library)

    print(f"\n自测结果：{'全部通过' if not failed else f'{failed} 个用例不通过'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按需求形态与缺口维度推荐这次需求问询该用哪些方法（规范：references/需求问询-方法映射.md）")
    parser.add_argument("features", nargs="*",
                        help="自由写的特征词（会被忽略，真正生效的是 --shape/--gaps/--risk；用 `-` 从 stdin 读）")
    parser.add_argument("--shape", default="general",
                        help="需求形态：web / cli / data / doc / skill / refactor / automation / general")
    parser.add_argument("--gaps", default="",
                        help="逗号分隔的缺口维度，如 output,acceptance,constraints（也认中文「交付物」「性能」）")
    parser.add_argument("--risk", default="",
                        help="风险档：data-loss / irreversible / security（中文「数据丢失」「不可逆」也认）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--list", action="store_true", help="列出方法库（id / 名称 / 档位 / 信号）")
    parser.add_argument("--selftest", action="store_true", help="跑本脚本内置用例")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.list:
        if args.json:
            print(render_library_json())
        else:
            for method in METHODS:
                print(f"{method['id']}  {method['name']} ［{method['tier']}］ 信号：{'、'.join(method['signals'])}")
        return 0

    gaps = [token for token in args.gaps.replace("，", ",").split(",") if token.strip()]
    if not gaps:
        extra = read_features(args.features)
        dims, _warnings = resolve_gaps(extra)
        gaps = sorted(dims)
    try:
        methods, warnings = select_methods(args.shape, gaps, args.risk)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(render_json(args.shape, gaps, args.risk, methods, warnings))
    else:
        print(render_text(args.shape, gaps, args.risk, methods, warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
