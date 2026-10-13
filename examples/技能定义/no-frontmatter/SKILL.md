# no-frontmatter —— 反例：没有 YAML frontmatter

这份 SKILL.md 直接从标题开始，没有 `---` 包住的 frontmatter。宿主拿不到 `name` 与 `description`，
结果不是"报错"，而是**这个技能根本不存在于模型目录里**：既不显示、也不会被自动触发，
用户只会觉得"我明明装了"。

## 正确写法

文件第一行必须是 `---`，中间写 `name` 与 `description`，再用 `---` 收尾：

```markdown
---
name: 你的技能名
description: 当……时，使用本技能：……。不接：……。
---

# 技能标题

正文……
```

`check_skill.py` 期望：`ERROR: [E1]`（开头没有 YAML frontmatter）。
