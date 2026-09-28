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

如果想要一个可以从 Finder 双击打开的窗口，在插件目录执行：

```bash
bash app/build_app.sh
open "../Jupiter 作业管家.app"
```

这个 App 是浏览器看板的 macOS 外壳，工具栏提供“同步 Jupiter”“现在有几分钟？”和“打开文件夹”。它会在运行时查找你自己的 `~/JupiterStudyPlanner` 数据目录；分享包只带源码和构建脚本，不带任何账户、作业或浏览器会话。

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

这个命令**仅生成**，不安装、不启动。可以请负责设置的人检查生成文件并启用；启用后应以本机调度状态和新的成功记录验证。Mac 需要运行、联网且会话有效；不保证睡眠、关机或会话过期期间按时读取。提醒写在本机文件中；macOS App 会对新增作业、状态变化或读取失败显示系统通知，没有手机推送。

如希望在 Codex 中用自然语言操作，可以另外安装包含 `.codex-plugin/plugin.json` 的此插件，并告知 Codex 自己的实例配置路径。独立命令不依赖 Codex；分享包不会自动注册到接收者的 Codex，也不会继承原作者的任务、权限或定时任务。

## 分享前

只分享生成的 ZIP。不要发送个人数据目录、浏览器 profile、`collector-session.json`、自己的 `local-instance.json`、作业报告或实际看板文件。打包器只选代码、文档、测试和素材，不扫描个人目录：

```bash
python3.12 scripts/package_share.py --output ../Jupiter-作业管家-分享包.zip
```

Jupiter 网页结构或登录规则改变可能使采集停止，需要更新程序。这是第三方只读工具，没有使用已验证的 Jupiter 学生端官方 API，也不承诺跨校即用。
