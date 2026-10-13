---
name: wrong-name
description: 当用户给出模糊需求、而它要交给另一个 AI 去执行时，使用本技能把它问询成可直接开工的长提示词。不接：单步任务、纯问答检索翻译。
---

# dir-mismatch —— 反例：`name` 与所在目录名不一致

目录叫 `dir-mismatch`，`name` 却写着 `wrong-name`。目录包形态的规范是 `<name>/SKILL.md`：
宿主按目录发现技能、按 `name` 登记技能，两者不一致时轻则调用名对不上（文档里写 `/wrong-name`，
用户看到的却是 `dir-mismatch`），重则被当成另一个技能或直接跳过。

## 正确写法

要么把 `name` 改成目录名，要么把目录改成 `name`：

```text
examples/技能定义/dir-mismatch/SKILL.md   →    name: dir-mismatch
```

`check_skill.py` 期望：`ERROR: [E3]`。
