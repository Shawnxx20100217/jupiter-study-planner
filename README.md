# Jupiter Ed → TickTick

把本人可访问的 Jupiter Ed 作业读取为稳定的原始任务，再交给 GPT 规划学习日期和学习量。

> 当前公开版是面向 macOS 的本机源码。它需要 Python 3.12、Google Chrome 和自己的 Jupiter Ed 账户；只有同步到 TickTick 时才需要自己的 API token。Jupiter 页面结构只在少量页面上验证过，跨学校使用前请先做一次完整同步。

## 数据流

```text
Jupiter 读取器 → TickTick「原始任务」 → GPT 规划 → TickTick「Jupiter任务」
```

- **Jupiter 读取器**只读取 Jupiter、保存最近一次成功的原始事实，不访问 TickTick，也不调用模型。
- **TickTick 同步器**只发布已经保存的原始事实，并拉回 TickTick 的个人完成状态；它不重新抓取 Jupiter。
- **GPT**读取「原始任务」，按任务内容里的 `jupiter:<id>` 更新「Jupiter任务」中的计划日期、学习量和说明。GPT 的计划日期不会覆盖 Jupiter 的原始截止日期。
- Jupiter 的私人 Done 只是本地进度标记，不代表老师收到提交。文档、论文、论坛回复等提交型任务不会被当作私人完成标记。

两个插件使用同一套根目录 `scripts/`。插件目录只保存 manifest、技能说明和图标；发布包由脚本生成，避免三份运行时代码互相漂移。

## 仓库内容

- `scripts/`：唯一的确定性运行代码和打包脚本。
- `plugins/jupiter-reader/`：Jupiter 读取器的 manifest 和技能说明。
- `plugins/ticktick-sync/`：TickTick 同步器的 manifest 和技能说明。
- `app/`：可选的 macOS 同步控制窗口；不包含账户、浏览器会话或任务数据。
- `examples/`、`references/`、`tests/`：虚构配置、数据约定和离线回归测试。

## 构建 macOS 控制窗口

```bash
JUPITER_APP_OUT_DIR="$HOME/Applications" ./app/build_app.sh
```

构建脚本会先完成应用签名，再把透明的笔记本同步图标写入本机 Finder 的显示层，这样 macOS Tahoe 不会给传统 `.icns` 自动套灰色底板。这个 Finder 图标层只属于本机安装，不会把个人数据带进仓库；如果需要保留严格的代码签名检查，可用 `JUPITER_APPLY_FINDER_ICON=0` 构建。

## 本机运行

先创建虚拟环境并安装采集依赖：

```bash
python3.12 -m venv "$HOME/.jupiter-runtime"
"$HOME/.jupiter-runtime/bin/python" -m pip install -r requirements-collector.txt
"$HOME/.jupiter-runtime/bin/scrapling" install
```

创建个人配置（数据必须放在仓库外）：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/init_instance.py \
  --data-dir "$HOME/JupiterStudyPlanner"
```

在专用 Jupiter 窗口登录后，只读取并保存 Jupiter 原始事实：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_login.py \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py run \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" \
  --source-only
```

TickTick 连接和清单选择通过本机授权向导完成：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/ticktick_authorize.py \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" --web
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py sync-ticktick \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

授权前请先在 TickTick 手动创建两个清单：`原始任务`（Jupiter 事实来源）和 `Jupiter任务`（GPT 规划结果）。授权向导只绑定 `原始任务`，不会偷偷把缺失的来源清单改绑到计划清单；如果来源清单被删除，请重新授权或显式传入 `--project-id`。

本机自动运行需要生成并启用 launchd 配置；生成配置本身不会启用定时。推荐用控制脚本开启每小时读取和发布，并可选登录时启动：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/sync_control.py configure \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" \
  --python "$HOME/.jupiter-runtime/bin/python" --enabled on --login on
```

`watch` 和 TickTick 发布任务默认每小时运行；旧式 `sync` 模式仍是每天本机时间 07:00 和 17:00，仅为旧实例兼容保留。

## 生成两个插件包

```bash
python3 scripts/package_share.py --plugin jupiter-reader \
  --output dist/jupiter-reader.zip
python3 scripts/package_share.py --plugin ticktick-sync \
  --output dist/ticktick-sync.zip
```

两份 ZIP 都包含自己的 manifest、技能说明和由根目录生成的完整运行时；不会包含个人 state、浏览器 profile、报告、Cookie 或令牌。源码分享包可以用不带 `--plugin` 的命令生成：

```bash
python3 scripts/package_share.py --output dist/jupiter-study-planner-source.zip
```

ZIP 是可审阅、可导入的插件包，不会因为发布到 GitHub 自动注册到 ChatGPT/Codex；需要在支持本地插件的客户端插件管理界面导入或安装。两个插件的运行权限不同：读取器只读 Jupiter，同步器只写入你已授权的 TickTick 来源清单。

## 隐私与使用边界

只用于本人有权访问的 Jupiter Ed 账户和本人 TickTick 清单。程序读取已登录页面，不绕过权限或验证码，不提交作业，不上传 Cookie、密码或 API token，也不会把个人任务数据提交到仓库。使用前请遵守学校和相关服务的条款。

Jupiter 没有在本项目中验证过可用的学生端 API 或 ICS 接口；读取失败时程序保留上一次成功数据，不把失败尝试伪装成成功。TickTick 的提醒由 TickTick 任务设置和客户端通知负责，是否送达仍取决于 TickTick 账户和设备设置。

## 验证

```bash
python3 -m unittest discover -s tests -q
```

测试使用虚构数据，不登录 Jupiter、不连接 TickTick。提交前请检查仓库中没有个人配置：

```bash
git status --short
git ls-files | rg '(^|/)(work|state|reports|browser-profile)/|local-instance\.json$|collector-session\.json$|ticktick-token$'
```

仓库当前没有附带开源许可证；如果要基于此项目再发布衍生版本，请先取得维护者授权。
