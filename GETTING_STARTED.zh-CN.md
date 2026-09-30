# 第一次使用 Jupiter 作业管家

这是可复制的本机工具原型，提供作业看板、截止日期汇总和学习队列。分享包包含程序、说明和虚构测试，不包含原作者的作业、登录会话、浏览器资料或定时设置。

**可以发给别人；目前不是下载后双击就能用的成品。** 接收者需要自己的 Jupiter Ed 学生账户，独立完成安装、填写自己的学校与课程、登录和首次同步。当前只在一个学校的学生页面做过实际验证；其他学校、家长账户、不同页面布局或验证流程可能需要适配。

## 我在哪里看结果？

成功同步后，在个人数据目录的 `reports` 文件夹查看：

| 文件 | 用途 |
| --- | --- |
| `study-plan.html` | 浏览器打开的作业看板；无需启动网页服务、无需 Codex。 |
| `study-plan.md` | 学习优先队列文字版。 |
| `study-plan-calendar.md` | 按月汇总的截止日期。 |
| `study-plan.ics` | 导入日历软件的文件快照。 |

也可以在 macOS 终端直接打开看板：

```bash
open "$HOME/JupiterStudyPlanner/reports/study-plan.html"
```

如果想要一个可以从 Finder 双击打开的控制窗口，在插件目录执行：

```bash
bash app/build_app.sh
ditto --norsrc "../Jupiter 作业管家.app" "$HOME/Applications/Jupiter 作业管家.app"
open "$HOME/Applications/Jupiter 作业管家.app"
```

这个 App 是本机同步控制面板，显示 Jupiter → TickTick 的连接状态、最近成功时间，并提供“立即同步”和“打开 TickTick”。在窗口中打开“后台自动同步”会管理 Jupiter 读取、TickTick 发布和截止提醒；打开“登录 Mac 时自动启动”会把当前实例的后台任务加入登录启动。关闭开关只暂停后续运行，不删除 TickTick 里已有的任务；正在进行的一轮读取会自然结束后停止。关闭窗口不影响已开启的后台同步。

看板显示上次同步时间；App 打开时会检查是否超过 15 分钟未尝试读取，并在前台每分钟检查一次，手动同步始终可用。作业和老师通知旁边有勾选，分别记录本地完成和已读；下一次同步会与 Jupiter 核对。电脑上的文件不会自动出现在手机上。

**`.ics` 是文件导入，不是自动订阅服务。** Apple Calendar 或 Google Calendar 导入的是当时的快照；本机文件后来更新，不会自动更新已导入的第三方日历。再次导入如何处理重复或改期，由日历软件决定。只给日期的截止项标为全天且时间待确认；未知日期保留在报告，不编造日历事件。

## 准备条件

- macOS，Google Chrome，Python 3.12；当前完整独立运行与定时方案针对 macOS。Windows 尚未适配。
- 自己可以登录的 Jupiter Ed 学生账户和网络。
- 首次安装需要下载依赖；常规采集、解析与规划不调用模型，也不需要模型 API key。

将解压后的 `jupiter-study-planner` 文件夹放在稳定位置。下面的命令从此文件夹运行；`~/JupiterStudyPlanner` 是新建的个人数据目录，**不能放进分享包内**。

## 1. 安装本机运行环境

```bash
python3.12 -m venv "$HOME/.jupiter-runtime"
"$HOME/.jupiter-runtime/bin/python" -m pip install -r requirements-collector.txt
"$HOME/.jupiter-runtime/bin/scrapling" install
```

这一步需要 Python 和网络。如果电脑找不到 `python3.12`，先安装 Python 3.12。插件不自带 Python、Chrome 或已安装依赖，也不自动更改系统软件。

## 2. 建立你自己的配置

先在自己的 Jupiter 页面核对：学生姓名、学校及学年、当前学期、全部课程名称、学校所在时区。按照页面文字原样填写。然后运行：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/init_instance.py --data-dir "$HOME/JupiterStudyPlanner"
```

它会逐项询问并创建 `~/JupiterStudyPlanner/state/local-instance.json`，自动填好个人数据、浏览器和报告路径；不会索取密码，也不会登录、同步或启用定时。已有数据不会覆盖。

`examples/identity.example.json` 与 `examples/local-instance.example.json` 都是虚构示例。也可以在**插件外**复制、修改身份示例，再用 `--identity-json /你的路径/identity.json` 初始化。不要直接采用示例姓名、课程或时区。课程或学年改变时，需要核对并更新自己的配置。

## 3. 首次登录并同步

打开专用 Chrome：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_login.py --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

在新窗口自己登录。在 Jupiter 的 Settings 内开启 “Stay logged in on this computer/device”，然后关闭这个专用 Chrome 窗口，让采集器可以使用它的资料目录。普通 Chrome 或 Codex 里已经登录，并不等于这个独立会话已经登录。

运行首次同步：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py run --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

成功后，用 Finder 打开 `~/JupiterStudyPlanner/reports/study-plan.html`。如果报错，查看 `state/run-status.json` 的错误与最近成功时间；程序保留旧结果，不会把采集失败当作“没有作业”。没有首次成功记录，就不能称为已可用。会话失效时，需要再次自行登录。

要核对无人值守可用性，请关闭专用 Chrome 后再成功同步一次。这能验证浏览器重启后的登录会话；不能保证 Jupiter 会话永不过期。

## 4. 安排当下的学习时间

不要求每一天都有固定课表。说清或输入本次实际可用时间，例如 20 分钟：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/planner.py plan --state "$HOME/JupiterStudyPlanner/state" --minutes 20 --output "$HOME/JupiterStudyPlanner/reports/session.md"
```

打开 `reports/session.md` 查看这一段建议。未提供空闲时段时只生成优先队列，不把 Jupiter 日历上的空档当成自习时间。预计用时可以调整；老师通知预览仍待核实，不会自动理解成新任务。

## 5. 定时运行与 Codex 是独立选项

**解压分享包、安装 Codex 插件或创建实例，都不等于定时已启动。** 每个人都需要在本机另外完成首次采集验收和调度设置。

目前提供以下命令生成 macOS launchd 配置（每天本机时间 07:00、17:00）：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py generate-launch-agent --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" --python "$HOME/.jupiter-runtime/bin/python" --output "$HOME/JupiterStudyPlanner/jupiter-sync.plist"
```

这个命令**仅生成**，不安装、不启动。可以请负责设置的人检查生成文件并启用；启用后应以本机调度状态和新的成功记录验证。Mac 需要运行、联网且会话有效；不保证睡眠、关机或会话过期期间按时读取。

若希望老师临时布置的作业更快出现，可以把上面的 `generate-launch-agent` 改成 `--mode watch`。watch 每 15 分钟读取一次 Jupiter；没有变化时不会重复写入通知。Jupiter 没有可靠的推送接口，因此这已经是本机轮询能做到的及时性上限，电脑唤醒后会重新检查。

截止前提醒使用独立的本机任务，不打开浏览器，每 30 分钟检查最近一次成功读取的计划：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py generate-launch-agent --mode remind --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" --python "$HOME/.jupiter-runtime/bin/python" --output "$HOME/JupiterStudyPlanner/jupiter-reminders.plist"
```

默认在截止前 24 小时、2 小时和 30 分钟各提醒一次。提醒记录在本地 `notifications.json`，macOS App 和系统通知会显示；没有邮件或手机推送。Jupiter 登录失效、Mac 睡眠或关机时，提醒会等下一次本机任务运行。

### 让 TickTick 负责提醒

最省事的首次连接方式是运行本机授权向导：

```bash
/PRIVATE/jupiter-runtime/bin/python /PLUGIN/scripts/ticktick_authorize.py \
  --instance /PRIVATE/jupiter-state/local-instance.json --web
```

它会输出一个只在本机有效的临时页面。先在 TickTick 网页中打开头像 → **Settings → Account → API Token** 创建个人令牌，再把令牌粘贴到页面；页面不会回显令牌。程序会先只读验证令牌和 `Jupiter 作业` 清单，验证成功才以权限 `600` 保存并启用自动同步。令牌不会经过聊天、Codex 连接器或 GitHub。这个过程每台电脑只需一次；后续 15 分钟采集完全由本地程序运行，不调用模型。

如果你已经在用 TickTick，可以把它作为任务和提醒的最终界面。先在 TickTick 的账户设置中创建 API token，把 token 单独保存到个人实例目录（不要放进插件或 GitHub），然后在 `local-instance.json` 加上：

```json
{
  "ticktick_enabled": true,
  "ticktick_api_base": "https://api.ticktick.com/open/v1",
  "ticktick_project_id": "你的项目 ID",
  "ticktick_token_file": "/PRIVATE/jupiter-state/ticktick-token",
  "ticktick_reminder_minutes": [1440, 120, 30]
}
```

用一次手动发布验证连接：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py sync-ticktick --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

它把活动 Jupiter 作业创建或更新到指定项目；重复运行不会重复创建。TickTick 勾选后会在本地记录对应任务完成；提交文档、答案或论坛文字的任务不使用 Jupiter 私人 Done，本地界面也不会显示误导性的完成勾选。课堂记录和 Participation / Attendance 项目才显示可点击的本地完成框。同步只提取 Jupiter 课程列表中的文字、课程和截止日期，不逐个打开详情页，也不会把本地勾选当成老师收到的提交证据。连接验证后可以关闭本机 `remind` plist，后续提醒由 TickTick 负责。若使用中国滴答清单，把 API 地址换成 `https://api.dida365.com/open/v1`。

如希望在 Codex 中用自然语言操作，可以另外安装包含 `.codex-plugin/plugin.json` 的此插件，并告知 Codex 自己的实例配置路径。独立命令不依赖 Codex；分享包不会自动注册到接收者的 Codex，也不会继承原作者的任务、权限或定时任务。

## 分享前

只分享生成的 ZIP。不要发送个人数据目录、浏览器 profile、`collector-session.json`、自己的 `local-instance.json`、作业报告或实际看板文件。打包器只选代码、文档、测试和素材，不扫描个人目录：

```bash
python3.12 scripts/package_share.py --output ../Jupiter-作业管家-分享包.zip
```

Jupiter 网页结构或登录规则改变可能使采集停止，需要更新程序。这是第三方只读工具，没有使用已验证的 Jupiter 学生端官方 API，也不承诺跨校即用。
