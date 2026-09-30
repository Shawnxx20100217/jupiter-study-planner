# Jupiter 作业管家

<img src="assets/jupiter-icon.png" alt="Jupiter 作业管家图标" width="128" height="128">

把 Jupiter Ed 作业整理为动态优先队列。每天课程和自习时间可以不同；你说“现在有 20 分钟”，再安排这一段能推进的任务，不要求固定的每日学习表。

**第一次用或发给朋友，请从[中文上手指南](GETTING_STARTED.zh-CN.md)开始。** 这是需要自行配置的本机工具原型，当前完整流程面向 macOS、Chrome 和 Python 3.12。接收者要用自己的 Jupiter 学生账户登录；跨学校页面尚未完成验证，不承诺下载即用。

同步后的可见入口是个人报告目录里的 `study-plan.html`，用浏览器打开即可。分享包只含代码、说明和虚构示例；安装插件不会自动设置对方的账户或启动定时。

在 macOS 上还可以构建一个独立的 **Jupiter 作业管家 App**。它是一个轻量的本机控制窗口：可以打开 TickTick、立即同步，并用两个开关控制后台同步和登录 Mac 时自动运行。关闭窗口不会停止后台任务；关闭“后台自动同步”会暂停 Jupiter、TickTick 和截止提醒，保留已有任务与本地数据。App 会在运行时读取 `~/Library/Application Support/Jupiter Study Planner/paths.json` 或 `~/JupiterStudyPlanner`，不会把登录状态、作业内容或机器路径编译进程序。

```bash
bash app/build_app.sh
ditto --norsrc "../Jupiter 作业管家.app" "$HOME/Applications/Jupiter 作业管家.app"
open "$HOME/Applications/Jupiter 作业管家.app"
```

App 需要先完成本机实例初始化和首次同步；它不会替代登录或把账户资料复制给别人。设置窗口里的“登录 Mac 时自动启动”只管理当前实例的后台调度，不会打开普通 Chrome，也不会删除 TickTick 中已有的任务。分享包包含 App 源码和构建脚本，接收者在自己的 Mac 上构建即可。

## 当前实现与验收状态

独立流程已经实现：Scrapling 浏览器采集 → 纯规则 HTML 解析与归一化 → 本地状态合并 → 动态队列与变化记录。常规运行不调用模型，不需要 API key，也不需要 Codex 开着。解读复杂通知、诊断页面或调整任务可另外在 Codex 中提出请求。

代码曾在一个学校的学生页面完成独立采集验证，但这不代表每个新实例已可用。每个实例均需自己的首次登录和完整采集验收，以该实例的 `run-status.json` 最近成功时间和覆盖范围为准；是否启用定时另查本机调度。失败保留上次成功结果，不把尝试时间冒充成功时间，也不无声改用模型浏览器重新抓取。

- `scripts/jupiter_collector.py`：用 Scrapling 专用 Chrome 会话校验身份、学校学年、学期与课程，读取当前学期课程列表、Calendar 和最近 14 天教师通知预览。
- `scripts/jupiter_dom.py`、`jupiter_import.py`：纯规则解析、稳定 ID 映射、日期核对、状态分类与通知去重。
- `scripts/jupiter_runtime.py`：串起采集、导入、规划和本地变化记录；提供只生成配置的 macOS 定时器命令。
- `scripts/calendar_output.py`：把 Jupiter 截止项和本地学习块输出为中文月视图与标准 `.ics` 文件，供日历软件手动导入；没有自动订阅地址，本机文件更新不会自动更新第三方日历。
- `scripts/dashboard.py`：生成本机浏览器可打开的只读作业看板，展示同步状态与上次成功的数据。
- `scripts/init_instance.py`：交互填写自己的身份与课程，建立插件外的私密目录；不登录、不采集、不启用定时。
- `scripts/package_share.py`：按文件类型与目录白名单生成分享 ZIP，排除会话、状态、报告和编译缓存。
- `scripts/planner.py`：管理个人进度、动态队列，以及用户明确提供的学习时段。

规划器会为每项任务给出可复现的优先级依据、风险标记和下一步动作：例如“24 小时内到期”“只有日期，具体钟点待确认”或“本次扫描未核实”。这些解释来自固定规则，不调用模型；未知日期会先建议核对，不会伪造截止时间。说“现在有 20 分钟”只安排这一段，并保留未安排的剩余用时。

教师通知目前只读取 **Last 14 days 预览**，新内容进入待核对列表，不承诺读取全文或自动理解布置、生成截止日期。课程列表覆盖不代表通知全文、旧通知、隐藏作业或逐项详情已读取。

## 安装与首次登录

独立采集使用 Python 3.12 和已安装的 Google Chrome；依赖固定为 `scrapling[fetchers]==0.4.15`。以下路径均为占位符。把插件放在稳定位置，把运行环境和实例数据放在插件外的私密目录：

```bash
python3.12 -m venv /PRIVATE/jupiter-runtime
/PRIVATE/jupiter-runtime/bin/python -m pip install -r /PLUGIN/requirements-collector.txt
/PRIVATE/jupiter-runtime/bin/scrapling install
```

在 `/PRIVATE/jupiter-state/local-instance.json` 写入本机配置。身份字段须与自己页面显示的文字一致；下面没有真实账户数据：

```json
{
  "state_dir": "/PRIVATE/jupiter-state",
  "profile_dir": "/PRIVATE/jupiter-state/browser-profile",
  "report_path": "/PRIVATE/reports/study-plan.md",
  "school_timezone": "Asia/Shanghai",
  "expected_student": "页面显示的学生姓名",
  "expected_school_year": "页面显示的学校名称 2026-27",
  "expected_term": "页面显示的当前学期",
  "expected_courses": ["页面显示的课程名称"]
}
```

时区改为学校实际时区，课程列表填写全部预期课程。配置与浏览器会话属于私密数据，不随插件分享。首次登录使用专用采集浏览器；普通 Chrome 或 Codex 已登录并不代表独立会话也已登录：

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/jupiter_login.py --instance /PRIVATE/jupiter-state/local-instance.json
```

在打开的窗口自行登录，无需把密码发给 Codex。设置持久登录后关闭这个专用 Chrome 窗口，再执行同步；程序不复制其他浏览器的 Cookie 或凭据库。窗口成功打开或登录成功不表示完整采集通过，仍需下一步成功。

## 同步、规划和进度

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/jupiter_runtime.py run --instance /PRIVATE/jupiter-state/local-instance.json
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/planner.py plan --state /PRIVATE/jupiter-state --minutes 20 --output /PRIVATE/reports/session.md
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/planner.py update --state /PRIVATE/jupiter-state --id TASK_ID --remaining 20
```

`run --dry-run` 会实际采集和校验，但不更新任务状态、报告或通知文件。正常 `run` 生成默认队列；不给空闲时段就不编造日程。`plan --availability FILE` 只使用用户确认的时段，并减去其中已知的忙碌时间，格式见[数据说明](references/data-format.md)。

每次成功同步还会在报告旁生成 `study-plan.html`、`study-plan-calendar.md` 和 `study-plan.ics`。Jupiter 只给日期时，月视图会标成“时间待确认”；本地规划块只有在你明确提供可用时段后才会写入日历。日期型截止可作为全天事件导出，未知日期不会伪造事件。`.ics` 只是文件快照；导入后不会随本机同步自动更新，重复导入的处理取决于日历软件。

在 Codex 中可以说“同步 Jupiter 作业”“现在有 20 分钟”“这项还剩 15 分钟”。已配置实例时，插件优先调用本地流程。只有用户要求诊断时才使用模型浏览器检查页面；独立运行失败不会触发隐蔽的模型采集开销。

作业文字、未评分和实际提交分别处理；提交文档、答案或论坛文字的任务不会启用本地完成勾选，课堂记录和 Participation / Attendance 项目才启用。新发现的历史未评分项单列待核实；已有待办不会仅因逾期就被隐藏。信息、免做和评分汇总项不计入工作量。已确认课堂互评保留课堂提醒；已被细分作业覆盖的汇总通知不重复计时。仅有日期时不编造钟点，原先明确的截止时刻只在新日期相容时保留。Calendar 空档不自动成为自习时间。

## 本地定时与结果

新实例不会自动启用定时；已有实例应检查实际调度状态。以下命令只生成 macOS launchd 配置，不安装或启动它：

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/jupiter_runtime.py generate-launch-agent --instance /PRIVATE/jupiter-state/local-instance.json --python /PRIVATE/jupiter-runtime/bin/python --output /PRIVATE/jupiter-sync.plist
```

同步配置为每天 **本机当地时间 07:00、17:00**，不是强制北京时间。用户授权启用并完成真实运行验收后，由 launchd 调用 Python；定时运行没有模型调用，也无需 Codex 保持打开。Mac 必须可运行、会话有效且能访问网络；不保证睡眠、关机或登录失效期间准时执行。生成配置不代表已启用。

如果老师经常在白天临时布置作业，推荐把同步 plist 生成为 `--mode watch`：它每 15 分钟读取一次 Jupiter，电脑唤醒时立即补查；没有变化时不会新增通知，也不会重复创建 TickTick 任务。它仍然受 Jupiter 登录状态、网络和电脑睡眠影响，不能代替服务器推送。

截止提醒由第二个本机 launchd 任务负责。它每 30 分钟读取最近一次成功同步的本地计划，不打开 Jupiter 浏览器，默认在截止前 **24 小时、2 小时、30 分钟**各提醒一次。生成命令是在同步命令上加 `--mode remind` 并输出另一个 plist。截止日期只有日期时，提醒文字会标明“日期型截止”；不会把 Jupiter 未提供的具体钟点伪造成事实。

提醒会写入本地报告、`pending-review.json` 和 `notifications.json`；当前 macOS App 及本机提醒任务会显示系统通知（可在实例配置中关闭）。没有邮件或手机推送。重复内容、相同故障和同一任务的同一提醒档位不会重复通知。`run-status.json` 记录最近尝试、最近成功及错误；`notification-state.json` 用于去重。通知日期从 Today 变为 Yesterday 不被当作新内容。

## TickTick 输出

### 一次性连接向导

个人自用不需要注册 OAuth 应用。TickTick 官方 Open API 文档提供的快速方式是：打开 TickTick 网页，进入头像 → **Settings → Account → API Token**，创建个人令牌。令牌只在本机输入并保存，不要粘贴到聊天、插件仓库或 GitHub。可以用一次性本机页面完成验证和清单选择：

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/ticktick_authorize.py \
  --instance /PRIVATE/jupiter-state/local-instance.json --web
```

命令会打印一个仅绑定 `127.0.0.1`、随机路径的临时地址。提交前会只读验证令牌和目标清单；验证失败不会写入令牌或打开同步。成功后会把令牌以权限 `600` 写入实例目录，并自动启用 TickTick。它不读取 Codex 的连接凭据，也不需要模型。官方流程与 OAuth 备用流程见 [TickTick Open API 文档](https://developer.ticktick.com/docs/openapi.md)。

如果你希望由 TickTick 负责提醒和任务界面，可以打开实例配置中的 `ticktick_enabled`，填写 TickTick 项目和一个只放在本机、权限为 600 的 API token 文件：

```json
{
  "ticktick_enabled": true,
  "ticktick_api_base": "https://api.ticktick.com/open/v1",
  "ticktick_project_id": "你的 TickTick 项目 ID",
  "ticktick_token_file": "/PRIVATE/jupiter-state/ticktick-token",
  "ticktick_reminder_minutes": [1440, 120, 30]
}
```

也可以先用环境变量 `TICKTICK_ACCESS_TOKEN` 做一次验证。随后运行：

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/jupiter_runtime.py sync-ticktick --instance /PRIVATE/jupiter-state/local-instance.json
```

同步以 Jupiter 为作业事实源、以 TickTick 为任务和提醒界面：同一个 Jupiter 作业只会对应一个 TickTick 任务，截止日期或标题变化会更新它。提交文档、答案或论坛文字的任务只同步任务和状态，不猜测提交结果；课堂记录和 Participation / Attendance 项目才显示本地完成勾选。TickTick 负责任务提醒，Jupiter 只负责提供文字、课程和截止日期。不会因为一次 Jupiter 读取失败或部分页面缺失而删除 TickTick 任务。连接验证后可以关闭本机截止弹窗。国际版默认使用 `api.ticktick.com`；中国滴答清单请改用 `https://api.dida365.com/open/v1`，并使用对应区域的 token。

真实作业、进度、来源证据和浏览器 profile 放在私密实例目录。可分享插件只含代码、文档和虚构测试。没有已验证的学生端 Jupiter iCal/API 连接器；插件以 Jupiter 自带 Calendar 和课程表为事实来源，再生成本地 `.ics`。不会打开每个作业详情页去扫描或写回 Jupiter 的 Done；这样同步只读取课程列表文字，避免为了本地标记启动大量浏览器页面。

更多语义见[读取约定](references/jupiter-reading.md)。从插件目录运行回归测试：

```bash
python3.12 -m unittest discover -s tests -v
```
