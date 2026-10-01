---
name: ticktick-sync
description: 将最近一次本地 Jupiter 原始事实幂等同步到 TickTick，并拉回 TickTick 完成状态；适用于 GPT 规划前的来源发布请求。
---

# TickTick 同步器

这是一个本机确定性发布入口。它只读取已经保存的 Jupiter 状态并调用 TickTick Open API，不打开浏览器、不重新抓取 Jupiter、不调用视觉模型或其他模型，不删除 TickTick 任务。当前实例使用“原始同步”模式：它写入 TickTick 的 **“原始任务”** 清单，保留 **“Jupiter任务”** 给 GPT 和用户维护学习计划。

## 执行

先读取 `~/Library/Application Support/Jupiter Study Planner/paths.json`。使用其中的 `instance` 和 `python`；脚本目录优先使用其中的 `scripts`，不存在时使用本插件根目录的 `scripts`。如果没有 paths 文件，检查 `~/JupiterStudyPlanner/paths.json`；仍没有就说明需要先完成本机实例配置和 TickTick 连接，不要索取令牌或把令牌写入回复。

用确定性命令运行：

```bash
"$JUPITER_PYTHON" "$JUPITER_SCRIPTS/jupiter_runtime.py" sync-ticktick --instance "$JUPITER_INSTANCE"
```

先向用户说明这一步写入的是最近一次成功读取的 Jupiter 原始事实；如果需要先读取老师的新作业，应先调用 `Jupiter 读取器`，并确认它返回成功且覆盖完整后再调用本插件。遇到 API 超时、未配置令牌或权限错误，报告清晰错误并保留本地数据。

## 回复

读取命令输出 JSON 后报告创建、更新、远端完成、本地完成状态回写、警告和失败项。同步必须可重复：同一个 `jupiter:<id>` 保持一个 TickTick 原始任务；只更新来源标题、来源截止日期、来源状态和个人完成状态，不按标题复制，也不删除远端任务。任务内容中的 `Jupiter 私人完成` 可能是“已完成”“未完成”或“未核实”；它不代表老师收到提交。Jupiter 提供的截止日期是来源事实，不能把模型建议日期写成原始任务截止日期。

## 给 GPT 的下一步

同步成功后，GPT 读取 TickTick 的“原始任务”清单，按 `jupiter:<id>` 做差异比较，再更新“Jupiter任务”中的计划任务。原始任务新增时才新增计划任务；原始任务有变化时原地更新；没有变化时不动。已经在“Jupiter任务”中完成的任务默认保持完成，除非原始状态明确出现 reopened、returned 或 resubmit。SAT、托福复习和其他长期目标只在 GPT 规划层合并，不复制到“原始任务”。
