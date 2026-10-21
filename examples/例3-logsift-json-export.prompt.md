# 任务：给已有的 logsift 日志分析 CLI 加 --json 导出与结构化错误处理（Python 3.11，改造现有仓库）

## 1. 角色与目标

你是一名 Python 后端工程师，接手一个已经在用的日志分析工具 —— 仓库根目录 `E:\logsift`。目标：在主命令 `logsift filter` 上增加 `--json` 结构化导出，并把三条失败路径从「打印 traceback」改成「约定的退出码 + 一行中文原因」，同时不改动现有文本输出与现有退出码。

做完算成功的定义：`python -m logsift filter --json tests/fixtures/access.log` 的 stdout 是一个可被 `json.loads` 解析的对象，五个键与第 6 段的 schema 完全一致；`python -m pytest -q` 的 passed 数 ≥ 54 且 failed 数为 0；`python -m ruff check .` 退出码 0；不带 `--json` 时三个样本文件的 stdout 与改动前的输出逐字节相同。

## 2. 背景与已知事实

- [用户原话] 「这个 logsift 我们组用了半年，现在要接 Grafana 的日志面板，得先能吐 JSON，别把现在命令行那套搞坏。」
- [用户原话] 「报错别甩 traceback，运维看不懂，给个退出码和一行原因就行。」
- [仓库实测] `E:\logsift` 是 src 布局的 Python 3.11 项目：`pyproject.toml` 里 `version = "0.3.2"`、`requires-python = ">=3.11,<3.12"`、`[tool.ruff] line-length = 100`；源码在 `src/logsift/`（`cli.py` 212 行、`filters.py` 96 行、`timeline.py` 143 行）。
- [仓库实测] 测试在 `tests/`：`test_cli.py` 24 个用例、`test_filters.py` 13 个用例，当前 `python -m pytest -q` 为 37 passed；样本日志在 `tests/fixtures/`，现有 `access.log`（1200 行）与 `cn.log`（中文 message，84 行）两个文件。
- [仓库实测] `src/logsift/cli.py` 用 argparse 定义全局参数 `--level`、`--since`、`--until`、`--format {text}` 与子命令 `filter`/`summary`；主流程在 `main()` 里只有 `sys.exit(0)` 与 argparse 的退出码 2 两处返回，业务异常没有被捕获。
- [仓库实测] 现有退出码只有 3 个：0 正常、1 未捕获异常、2 argparse 用法错误；`README.md` 第 41~48 行有一张旧退出码表，`docs/USAGE.md` 是唯一的使用说明。
- [外部资料 https://docs.python.org/3.11/library/json.html#json.dump] `json.dumps(..., ensure_ascii=False)` 才会让中文按原字符输出，默认 `ensure_ascii=True` 会把中文写成 `\u` 加四位十六进制的转义序列。
- [外部资料 https://docs.python.org/3.11/library/argparse.html#exiting] argparse 在参数错误时用退出码 2，且先于业务代码执行，所以新增 `--json` 的用法错误也会走 2。
- [AI 推断] Grafana 面板要求 JSON 里每条日志占一行（未确认，见第 10 段）。

## 3. 交付物

| 路径 | 是什么 | 格式 / 规模 |
| --- | --- | --- |
| `E:\logsift\src\logsift\render.py` | 新增：文本与 JSON 两个渲染函数 | ≤140 行，不 import argparse |
| `E:\logsift\src\logsift\cli.py` | 改：加 `--json`、`--strict`、`--max-bad-ratio` 三个参数与退出码映射 | 改动 ≤80 行 |
| `E:\logsift\tests\test_json_output.py` | 新增：JSON 导出与 schema 测试 | 8 个用例 |
| `E:\logsift\tests\test_error_paths.py` | 新增：三类失败路径与退出码测试 | 9 个用例 |
| `E:\logsift\tests\fixtures\bad-heavy.log` | 新增：坏行样本 | 60 行，其中 7 行缺时间戳 |
| `E:\logsift\tests\fixtures\bad-first.log` | 新增：首行坏样本 | 20 行，第 1 行缺级别字段 |
| `E:\logsift\docs\USAGE.md` | 改：`--json` 用法段 + 5 行退出码表 | 新增 ≤40 行 |
| `E:\logsift\README.md` | 改：第 41~48 行旧退出码表改为指向 `docs/USAGE.md` | 改动 ≤12 行 |
| `E:\logsift\pyproject.toml` | 改：`version` 升到 `0.4.0` | 改 1 行 |
| `E:\logsift\CHANGELOG.md` | 改：顶部加一条版本记录 | `## [0.4.0] - 2026-10-20` |

## 4. 需求（R#）

- **R1** 在 `logsift filter` 上新增 `--json` 开关；开启后 stdout 只输出一个 JSON 对象，键恰为 `schema_version`、`total_lines`、`matched_lines`、`level_counts`、`bad_lines` 五个，其中 `schema_version` 固定为字符串 `"1"`，`level_counts` 是 `debug`/`info`/`warn`/`error` 四个键齐全的非负整数映射。
- **R2** `--json` 的整个 stdout 必须能被 `json.loads(stdout)` 解析，中文按 UTF-8 原字符输出（用 `ensure_ascii=False`），末尾只允许一个换行符，且 stdout 里不出现任何非 JSON 的提示行。
- **R3** 不带 `--json` 时，三个样本文件的 stdout 与 stderr 内容、进程退出码与改动前的 `0.3.2` 版本逐字节一致，不允许出现字段顺序、空行或末尾换行的差异。
- **R4** 输入文件不存在或不可读时，进程退出码为 3、stdout 为空，stderr 输出且只输出一行 `logsift: 无法读取输入文件 <路径>（<errno 文本>）`；这条路径上不得打印 `Traceback` 字样。
- **R5** 单行解析失败（缺时间戳或缺级别字段）时默认跳过该行并把次数记进 `bad_lines`；全部处理完后若坏行占比超过 `--max-bad-ratio`（默认 `0.05`）则退出码为 4 且 stderr 输出一行 `logsift: 坏行占比 <实际百分比> 超过阈值 <阈值百分比>`；加 `--strict` 时遇到第一行坏行立即退出码 4，stdout 不输出 JSON。
- **R6** 未预期异常统一在 `main()` 兜底捕获：非 `--json` 模式打印一行 `logsift: 内部错误 <异常类名>: <消息>` 后退出码 1；`--json` 模式向 stderr 输出 `{"error":{"code":"INTERNAL","message":"<异常类名>: <消息>"}}` 后退出码 1；两种模式都不打印 traceback，只有环境变量 `LOGSIFT_DEBUG=1` 时才打印完整堆栈。
- **R7** 新增 17 个测试用例（`test_json_output.py` 8 个、`test_error_paths.py` 9 个）覆盖 R1、R2、R4、R5、R6；原有 `tests/test_cli.py` 与 `tests/test_filters.py` 的 37 个用例一行都不改，`python -m pytest -q` 的 passed 数从 37 增加到 54。
- **R8** 文档与版本：`docs/USAGE.md` 增加 `--json` 段与一张 5 行退出码表（0/1/2/3/4）；`README.md` 第 41~48 行的旧表改为一行链接指向 `docs/USAGE.md`；`pyproject.toml` 的 `version` 升到 `0.4.0`；`CHANGELOG.md` 顶部加 `## [0.4.0] - 2026-10-20` 与三行改动说明。

## 5. 非目标

- 不做新的子命令，不改 `summary` 子命令的任何行为与输出。
- 不改 `--format` 参数与文本输出里的列顺序、分隔符、时间格式。
- 不引入第三方依赖：不加 `orjson`、不装 `pydantic`、不用 `rich` 给输出上色。
- 不改 `filters.py` 与 `timeline.py` 的匹配与统计逻辑，只让它们返回计数。
- 不做 PyPI 发布，不改 `.github/workflows/test.yml` 的 Python 版本矩阵。
- 不重排 `tests/test_cli.py` 与 `tests/test_filters.py` 里的用例，不做全仓 `ruff format`。

## 6. 技术约束

- 语言与版本：Python 3.11（`pyproject.toml` 的 `requires-python = ">=3.11,<3.12"`）；本机 Windows 11，命令用 PowerShell 执行。
- 依赖：运行期仅标准库（`argparse`、`json`、`pathlib`、`sys`、`logging`）；测试用现有 dev 依赖 `pytest 8.3` 与 `ruff 0.6`，新增包一律不允许。
- 目录约定：源码只放 `src/logsift/`，每个模块一个职责；测试只放 `tests/` 且文件名以 `test_` 开头；样本日志只放 `tests/fixtures/`。
- 代码风格：新增公共函数加类型注解，私有函数以 `_` 开头，行宽 100 与 `[tool.ruff]` 对齐；不跑 `ruff format`，避免动已有文件。
- JSON schema：键名一律 snake_case；`schema_version` 是字符串 `"1"`；后续版本只许加键、不许删键；`level_counts` 四个键固定齐全，缺哪个补 0。
- 退出码：`EXIT_OK = 0`、`EXIT_INTERNAL = 1`、`EXIT_USAGE = 2`（argparse 自带）、`EXIT_INPUT = 3`、`EXIT_BAD_LINES = 4`；常量定义在 `cli.py` 顶部，`render.py` 里不许调 `sys.exit()`。
- 禁止：禁止 `print(traceback.format_exc())`、禁止改动已有 37 个测试的断言、禁止在 `render.py` 里读环境变量以外的全局状态。

## 7. 验收标准（A#）

- **A1（对应 R1、R2）**：JSON 输出的键与值。判定：命令 `python -m logsift filter --json tests/fixtures/access.log | python -c "import json,sys;d=json.load(sys.stdin);assert d['schema_version']=='1';assert sorted(d)==['bad_lines','level_counts','matched_lines','schema_version','total_lines'];assert sorted(d['level_counts'])==['debug','error','info','warn'];print('OK')"` —— 期望 stdout 为 `OK`、退出码 0。
- **A2（对应 R2）**：中文不转义。判定：命令 `python -m logsift filter --json tests/fixtures/cn.log` —— 期望 stdout 含 `登录失败` 四个汉字、不含 `\u767b` 这六个字符。
- **A3（对应 R3）**：文本输出基线。判定：阈值 三个样本文件的字节差 = 0（测量方法：改动前在仓库根执行 `python -m logsift filter tests/fixtures/access.log > %TEMP%\logsift-before\access.txt` 等三条命令各存一份，改完后跑同一组命令，再用 `git diff --no-index before.txt after.txt` 逐个比对，三个退出码都为 0）。
- **A4（对应 R4）**：输入不可读。判定：命令 `python -m logsift filter --json tests/fixtures/nope.log; echo $LASTEXITCODE` —— 期望退出码 3、stdout 为空、stderr 只有一行且以 `logsift: 无法读取输入文件` 开头。
- **A5（对应 R5）**：坏行占比超标。判定：命令 `python -m logsift filter --json --max-bad-ratio 0.05 tests/fixtures/bad-heavy.log; echo $LASTEXITCODE` —— 期望退出码 4、stderr 出现「坏行占比」与 `11.7%`、`5.0%` 两个数字、stdout 仍是合法 JSON。
- **A6（对应 R5）**：`--strict` 首行即停。判定：命令 `python -m logsift filter --strict tests/fixtures/bad-first.log; echo $LASTEXITCODE` —— 期望退出码 4、stdout 为空、stderr 出现 `第 1 行` 字样。
- **A7（对应 R6）**：内部错误不甩 traceback。判定：人工 — 在 `filters.py` 里临时注入一次 `ZeroDivisionError` 后运行 `python -m logsift filter tests/fixtures/access.log`，stderr 出现一行 `logsift: 内部错误 ZeroDivisionError:` 且全文没有 `Traceback` 字样、退出码为 1 即为通过；验完把注入的代码删掉。
- **A8（对应 R7）**：测试总数与旧用例零改动。判定：阈值 passed 数 ≥ 54、failed 数 = 0（测量方法：`python -m pytest -q` 读最后一行）；旧用例零改动的测量方法是 `git diff --stat tests/test_cli.py tests/test_filters.py` 输出为空。
- **A9（对应 R8）**：版本与文档。判定：命令 `python -c "import tomllib,pathlib;print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"` —— 期望输出 `0.4.0`；再执行 `Select-String -Path docs/USAGE.md -Pattern '退出码'` —— 期望命中 ≥ 5 行。

## 8. 验证方式

```powershell
cd E:\logsift
python -m pytest -q                                          # 期望：passed ≥ 54，failed 0
python -m ruff check .                                       # 期望：All checks passed!
python -m logsift filter --json tests/fixtures/access.log    # 期望：一行 JSON，五键齐全
python -m logsift filter tests/fixtures/access.log           # 期望：文本输出与 0.3.2 的基线输出逐字节相同
```

- 任一条不通过：回到「执行步骤」第 4 步修，不要继续往下。
- 需要用户配合：A7 的故障注入只在自己分支上做，验完用 `git status --porcelain` 确认工作区回到改动前状态。

## 9. 执行步骤

1. 读现有约定并锁现状：读 `pyproject.toml` 的 ruff 配置、`tests/test_cli.py` 的写法、`docs/USAGE.md` 的现有结构，然后对三个样本文件各跑一次文本模式，把输出存到 `%TEMP%\logsift-before\` 下（对应 R3、R8）→ 产物：三份改动前的 stdout 基线，不进仓库。
2. 先补测试再改代码：建 `tests/fixtures/bad-heavy.log` 与 `bad-first.log`，写 `tests/test_json_output.py`（8 个用例）与 `tests/test_error_paths.py`（9 个用例），先让这 17 个用例红（对应 R1、R2、R4、R5、R6、R7）→ 产物：`python -m pytest -q` 显示 17 failed、37 passed。
3. 抽渲染层：把 `cli.py` 里拼文本输出的代码原样搬到新文件 `src/logsift/render.py`，文本输出一个字节都不改，`python -m pytest -q` 保持 37 passed（对应 R1、R3）→ 产物：`render.py` 落地、旧测试全绿。
4. 实现 JSON 渲染与三个新参数：`render_json(result)` 按第 6 段 schema 输出，`cli.py` 加 `--json`、`--strict`、`--max-bad-ratio` 与 `EXIT_*` 常量（对应 R1、R2）→ 产物：`test_json_output.py` 的 8 个用例转绿。
5. 实现三类错误路径：文件读取异常、坏行计数与阈值、`main()` 兜底捕获与 `LOGSIFT_DEBUG` 开关（对应 R4、R5、R6）→ 产物：`test_error_paths.py` 的 9 个用例转绿。
6. 基线回归并对照 `%TEMP%\logsift-before\` 的三份基线输出做逐字节比对，有差异就回到步骤 3 找原因（对应 R3）→ 产物：三个 `git diff --no-index` 退出码都为 0。
7. 更新文档与版本：`docs/USAGE.md` 加 `--json` 段与 5 行退出码表、`README.md` 旧表改为链接、`pyproject.toml` 升 `0.4.0`、`CHANGELOG.md` 加条目（对应 R8）→ 产物：第 8 段四条命令全绿。
8. 跑第 8 段四条命令并把输出留档（对应 R3、R7、R8）→ 产物：一段含四条命令与结果的验证记录，贴进汇报。

- 可中断点：步骤 3 做完先把「旧测试仍 37 passed」给用户看一眼，再继续动 JSON。
- 顺序依赖：步骤 4 依赖步骤 3 的 `render.py`；步骤 5 依赖步骤 4 的 `--json` 参数（JSON 模式的错误对象复用同一套渲染）；步骤 6 必须在步骤 3~5 全部做完之后。
- 每步做完先跑第 8 段对应命令，不要攒到最后一起验。

## 10. 假设与开放问题

- 未确认：JSON 键名用 snake_case 还是 camelCase（默认取 snake_case，与仓库现有 Python 命名一致；改成 camelCase 只需动 `render.py` 里 `SCHEMA_KEYS` 一处）。
- 未确认：坏行占比的阈值取值（默认取 5%，即 `--max-bad-ratio 0.05`；理由：`bad-heavy.log` 实测坏行占 11.7%，阈值定 5% 能拦住它）。
- 未确认：JSON 里 `message` 字段内的换行怎么处理（默认取原样保留 `\n`；理由：转义后还原不出原始字节；要一行一条得在 `render.py` 里加一次 `replace`）。
- 开放问题：`--json` 是否也要覆盖 `summary` 子命令（本次按只覆盖 `filter` 处理，覆盖 `summary` 需再改一个函数与两条测试）。

## 11. 何时必须停下来问人

- 触发条件：用户要求 JSON 键名改成 camelCase → 停下来先确认是哪几个键要改（`level_counts`、`bad_lines` 这类），避免下游脚本对不上。
- 触发条件：真实日志的坏行占比超过 5%，或用户对 5% 这个阈值有意见 → 停下来贴前 10 行坏行样本，问是放宽阈值还是加跳过规则。
- 触发条件：用户要求 JSON 的 `message` 一条一行（把 `\n` 转义成 `\\n`）→ 停下来确认下游的读法，再决定改不改。
- 触发条件：用户要求 `--json` 也覆盖 `summary` 子命令 → 停下来确认改动范围（多改一个函数与两条测试）。
- 触发条件：要把 `version` 推到 `0.5.0` 或发布到 PyPI → 停下来，发布流程不在第 5 段范围内。
- 触发条件：需要改动 `tests/test_cli.py`、`tests/test_filters.py` 里已有 37 个用例的任何断言 → 停下来说明要改哪几条，问是不是允许放宽旧行为。
- 触发条件：文本模式的输出出现任何字节差异、且原因不是新增代码 → 停下来贴 diff，不要继续往下改。

## 12. 汇报格式

汇报前先自证：按第 8 段的命令自己跑一遍，把真实输出贴出来；没有自证不许说「已完成」。

完成后按下面三段回话，不要写别的：

1. **改了什么**：按文件列清单（路径 + 一句话 + 标「新增 / 修改」），并贴一份 `git diff --stat` 的输出。
2. **怎么验的**：逐条贴 A1~A9 的命令与结果，标「通过 / 不通过」；`python -m pytest -q` 贴最后一行。
3. **还剩什么**：三条未确认项的默认取值、那条开放问题，以及每项改动要是用户不同意需要动哪个文件。

硬要求：任何 A# 不通过，不许写「已完成」，要写「A<n> 未通过，原因 <…>，建议 <…>」。
