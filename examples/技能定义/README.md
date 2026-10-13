# 技能定义的正反例：闸门②（`check_skill.py`）拿它们当夹具

一句话：**这里每一个目录都是一份完整的 `SKILL.md`，闸门②扫出来的 ERROR / WARN 必须和下表一字不差。**

```bash
python scripts/check_skill.py examples/技能定义/*/SKILL.md   # 逐个扫（bash/zsh 会展开通配符）
python scripts/check_skill.py --selftest                     # 自测：仓库 SKILL.md + 内置反例 + 下面这 9 个
```

| 目录 | 预期 ERROR | 预期 WARN | 它演示的是什么 |
| --- | --- | --- | --- |
| `good-minimal/` | —— | —— | **正例**：自足技能（规范写在自己正文里），只查 frontmatter 与描述 |
| `missing-siblings/` | `E7` | —— | 薄包装只拷了 `SKILL.md`：正文引用了同目录的 `AGENTS.md` / `references/` / 闸门脚本，它们却不在 |
| `oversize-description/` | `E4` | `W1` | 描述 1648 字符：超过规范上限 1024，也超过宿主只渲染的前 500 |
| `Bad-Name/` | `E2` | —— | `name` 不是 kebab-case（大写） |
| `dir-mismatch/` | `E3` | —— | `name` 与所在目录名不一致 |
| `no-frontmatter/` | `E1` | —— | 文件开头没有 YAML frontmatter |
| `no-trigger/` | `E5` | `W2` | 描述只写"这个技能做 X"，没有"何时该用"，也不是祈使句 |
| `no-boundary/` | `E6` | —— | 描述没有"何时不该用"的边界（过宽 = 在不该触发时触发） |
| `boundary-after-500/` | `E6` | `W1` | 边界写了，但写在第 500 字之后：只渲染前 500 字的宿主看不到 |

`--selftest` 里这 9 个用例的期望值**硬编码在 `scripts/check_skill.py` 的 `selftest()` 里**，
并且做了覆盖检查：往这个目录新增一个带 `SKILL.md` 的示例却没登记，自测会直接 FAIL。

## 三个容易踩的地方

**反例目录名本身可能"不合规"**：`Bad-Name/` 的目录名故意大写，因为它要演示 E2（name 格式），
同时不能顺带触发 E3（name 与目录名不一致）—— 所以目录名与 `name` 保持一致。
`dir-mismatch/` 反过来：目录名合规、`name` 不一致，专门触发 E3。

**闸门不会拿 prompt-forge 的家规去要求别人的技能**：E7 只在正文**真的引用了**同目录文件时才查；
W3（分诊三态）、W4（薄包装得写「动手前先读完」）、W5（菜单默认档）分别只在正文**真的声称了**
分诊 / 引用了同目录的 `AGENTS.md` / 给了菜单时才查 —— 只引用 `references/` 或闸门脚本、从不提
`AGENTS.md` 的技能不算薄包装，W4 不查。`good-minimal/` 就是这条的守门用例：
一份完全自足、与 prompt-forge 无关的技能，必须 0 ERROR 0 WARN。
（正文里**引用的样例代码块**不算声明——围栏里的「先分诊」「AGENTS.md」是别人的文字，会被剥掉再看。）
唯一对所有技能都查的是 E5/E6：描述里必须写清"何时该用 / 何时不该用"，这是官方对描述过宽过窄的判据。
E5 认「当…时 / 如果 / 用户说 / 使用本技能 / 触发」这些说法，但「当前只支持 Windows」里的「当前」不算；
W3 的标记词按**词边界**匹配（写 `good` 不会被当成写了 `go`），「不接（kill）」也不会被当成写了「接」。

**"同目录"是字面意思**：`missing-siblings/` 只报它正文里真的引用的那几样；如果正文写的是
`../AGENTS.md` 或 `D:\somewhere\AGENTS.md`，闸门**不**判 E7（你在指向别处，不是缺件），
裸写 `AGENTS.md` 而同目录没有才报。粒度是**文件级**：某条路径在正文任何地方出现过就算「被引用」，
从没出现过的路径才不查。另外 `frontmatter 没闭合`（找不到第二个 `---`）会报 **E1** ——
这种文件会被宿主整个忽略，连正文都不读；它由 `--selftest` 里的内置用例覆盖，不在本目录建实体文件。

## 期望输出

```text
PASS  [示例] Bad-Name: ERROR ['E2'] WARN []（期望 ERROR ['E2'] WARN []）
PASS  [示例] boundary-after-500: ERROR ['E6'] WARN ['W1']（期望 ERROR ['E6'] WARN ['W1']）
PASS  [示例] dir-mismatch: ERROR ['E3'] WARN []（期望 ERROR ['E3'] WARN []）
PASS  [示例] good-minimal: ERROR [] WARN []（期望 ERROR [] WARN []）
PASS  [示例] missing-siblings: ERROR ['E7'] WARN []（期望 ERROR ['E7'] WARN []）
PASS  [示例] no-boundary: ERROR ['E6'] WARN []（期望 ERROR ['E6'] WARN []）
PASS  [示例] no-frontmatter: ERROR ['E1'] WARN []（期望 ERROR ['E1'] WARN []）
PASS  [示例] no-trigger: ERROR ['E5'] WARN ['W2']（期望 ERROR ['E5'] WARN ['W2']）
PASS  [示例] oversize-description: ERROR ['E4'] WARN ['W1']（期望 ERROR ['E4'] WARN ['W1']）
```

检查码的含义与出处见 [`references/校验清单.md`](../../references/校验清单.md) 里「一之二、机器闸门②：技能定义」那张表。
