---
name: jupiter-reader
description: 读取已配置的 Jupiter Ed 作业和截止日期并保存本地变化；适用于只更新 Jupiter 数据、不写入 TickTick 的请求。
---

# Jupiter 读取器

这是一个本机确定性读取入口。ChatGPT 只负责理解用户要执行“读取”并展示结果；抓取、HTML 解析、去重、截止日期核对和本地保存由 Python 程序完成，不用视觉模型，也不访问 TickTick。

## 执行

先读取 `~/Library/Application Support/Jupiter Study Planner/paths.json`。使用其中的 `instance` 和 `python`；脚本目录优先使用其中的 `scripts`，不存在时使用本插件根目录的 `scripts`。如果没有 paths 文件，检查 `~/JupiterStudyPlanner/paths.json`，仍没有就说明需要先在本机完成一次实例配置，不要猜账户或学校信息。

用确定性命令运行：

```bash
"$JUPITER_PYTHON" "$JUPITER_SCRIPTS/jupiter_runtime.py" run --source-only --instance "$JUPITER_INSTANCE"
```

如果用户只是想验证而不保存，可加 `--dry-run`。如果会话失效，提示用户在独立 Jupiter 登录窗口重新登录；不要复制普通 Chrome 的 Cookie，也不要让用户把密码发到聊天里。

## 回复

读取命令输出 JSON 后，用简洁中文报告：是否成功、观察到的作业数、活动作业数、是否有新增/变更、覆盖是否完整、最近成功时间和需要核对的事项。失败时保留并说明上次成功数据；不要把失败尝试时间说成成功时间。不要把 Jupiter 的本地 Done 标记当成提交证明。

该入口禁止调用 `sync-ticktick`、TickTick API 或模型浏览器。用户要写入 TickTick 时转到 `TickTick 同步器`；用户要生成学习安排时由 GPT 读取 TickTick 的“原始任务”清单后完成规划。

## 与 GPT 的交接

这一步只负责刷新来源事实。成功后，状态保存在实例外的 `state.json`，下一步应按顺序调用 `TickTick 同步器`，把同一批事实幂等镜像到 TickTick 的 **“原始任务”** 清单。不要在本插件里读取或改写“Jupiter任务”计划清单，也不要把模型建议的学习日期写回 Jupiter 来源数据。

交接给 GPT 时遵守三条规则：

- 用 TickTick 任务内容中的 `jupiter:<id>` 作为唯一键；不要用标题、课程或截止日期重新建任务。
- “原始任务”是只读来源层；GPT 读取后在“Jupiter任务”中更新计划日期和计划说明，计划日期不能覆盖 Jupiter 原始截止日期。
- `Jupiter 私人完成：已完成/未完成/未核实` 是个人勾选状态，不是老师收到或评分的证明。SAT、托福和其他长期学习项目只进入 GPT 的计划层，不混入“原始任务”镜像。
