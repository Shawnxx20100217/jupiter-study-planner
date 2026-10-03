# Jupiter Ed → TickTick：本机上手

这份指南只覆盖本人使用的最短路径。程序不会替你登录，也不会把密码、Cookie 或 TickTick token 发到聊天或 GitHub。

## 需要什么

- macOS、Google Chrome、Python 3.12。
- 自己可以登录的 Jupiter Ed 学生账户。
- TickTick API token（可选；只在需要同步到 TickTick 时配置）。

当前版本先按“本人可访问账户、自己的学校页面”验证。跨学校页面、家长账户和不同登录流程需要自行检查。

## 0. 构建 macOS 控制窗口（可选）

```bash
JUPITER_APP_OUT_DIR="$HOME/Applications" ./app/build_app.sh
```

构建完成后，应用会使用透明的大主体图标；脚本会在签名完成后设置本机 Finder 图标层，以绕过 macOS Tahoe 对传统 `.icns` 的灰色底板处理。这个设置只影响当前 Mac 的显示，不会上传账户数据。若只需要严格的签名验证，可设置 `JUPITER_APPLY_FINDER_ICON=0`。

## 1. 安装依赖

```bash
python3.12 -m venv "$HOME/.jupiter-runtime"
"$HOME/.jupiter-runtime/bin/python" -m pip install -r requirements-collector.txt
"$HOME/.jupiter-runtime/bin/scrapling" install
```

## 2. 创建个人实例

数据目录必须在仓库之外：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/init_instance.py \
  --data-dir "$HOME/JupiterStudyPlanner"
```

按提示填写 Jupiter 页面显示的学生姓名、学校及学年、学期、学校时区和全部课程名称。示例文件只是假数据，不能直接使用。

## 3. 登录并读取

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_login.py \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

在打开的专用窗口里登录 Jupiter，并在 Settings 开启 “Stay logged in on this computer/device”。退出专用 Jupiter 浏览器后执行（不要退出你平时使用的 Chrome）：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py run \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" \
  --source-only
```

结果在 `~/JupiterStudyPlanner/reports/`。先看 `study-plan.html` 和 `state/run-status.json`；只有 `last_success` 更新，才算本轮读取成功。

## 4. 连接 TickTick

先在 TickTick 创建两个清单：**原始任务** 和 **Jupiter任务**。前者只放 Jupiter 的原始截止事实，后者由 GPT 写入计划日期。然后在 TickTick 网页的 **Settings → Account → API Token** 创建个人 token，再运行：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/ticktick_authorize.py \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" --web
```

向导只在本机临时页面中接收 token，验证通过后把它以受限权限保存在个人实例目录。接着发布原始作业：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/jupiter_runtime.py sync-ticktick \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json"
```

授权向导只绑定 **原始任务**；如果清单不存在会明确报错，不会回退到 **Jupiter任务**。重复运行按 `jupiter:<id>` 更新，不会按标题复制任务，也不会因为一次读取失败删除 TickTick 任务。

## 5. 交给 GPT 规划

同步成功后，让 GPT 读取 TickTick 的「原始任务」，再更新「Jupiter任务」：

1. 原始任务新增时创建对应计划任务。
2. 原始任务改变时按 `jupiter:<id>` 原地更新。
3. 未改变时保持不动。
4. 已完成的计划任务默认保持完成，除非 Jupiter 明确标记 reopened、returned 或 resubmit。
5. SAT、托福和其他长期目标只放进 GPT 的计划层，不写回「原始任务」。

模型建议的学习日期不能覆盖 Jupiter 的原始截止日期。

## 6. 定时运行（可选）

推荐直接开启每小时读取和发布，并让它在登录 Mac 时启动：

```bash
"$HOME/.jupiter-runtime/bin/python" scripts/sync_control.py configure \
  --instance "$HOME/JupiterStudyPlanner/state/local-instance.json" \
  --python "$HOME/.jupiter-runtime/bin/python" --enabled on --login on
```

需要关闭时把 `--enabled on --login on` 改成 `--enabled off --login off`。电脑睡眠、关机、断网或 Jupiter 会话过期时，本机无法按时读取。若只想检查生成的 plist，可使用 `jupiter_runtime.py generate-launch-agent --mode watch`；该命令只生成文件，不会启用它。

如果 TickTick 已负责提醒，可以不启用本地 `remind` 任务。项目仍保留本地提醒代码作为可选功能，但它不是两插件主流程。

## 插件包

从仓库生成两个可独立安装的插件包：

```bash
python3 scripts/package_share.py --plugin jupiter-reader --output dist/jupiter-reader.zip
python3 scripts/package_share.py --plugin ticktick-sync --output dist/ticktick-sync.zip
```

读取器只负责 Jupiter；同步器只负责 TickTick 原始任务。GPT 负责两者之间的规划层。仓库没有保留旧的综合插件入口，也不会把个人配置打进 ZIP。插件包不会自动注册到 ChatGPT；请在支持本地插件的客户端中导入对应 ZIP。

## 常见问题

- **没有成功时间**：重新登录专用 Jupiter 窗口，不要复制普通 Chrome 的 Cookie。
- **TickTick 授权失败**：检查 token 是否有效、目标清单是否仍存在，以及 API 地址是否与区域匹配。
- **任务重复**：检查任务内容中的 `jupiter:<id>`；不要按标题手动复制。
- **日期只有日历日期**：程序保留“时间待确认”，不会猜具体钟点。
- **想查看本地状态**：看 `state/run-status.json`、`ticktick-source-status.json` 和报告目录；这些内容都属于个人数据，不要提交到 GitHub。
