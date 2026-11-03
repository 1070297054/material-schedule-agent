# 本机网页、HTTP 与双 Agent

## 启动与停止

在仓库根目录运行，Python 3.9+，不需要安装第三方运行依赖：

```bash
# 离线 Mock，开放合成占用编辑和故障演示
PYTHONPATH=src python3 -m schedule_agent.server --demo

# 加载本机 .env 后，网页可选择真实 DeepSeek
PYTHONPATH=src python3 -m schedule_agent.server --env-file .env --demo
```

打开终端输出的完整 URL。服务只绑定 `127.0.0.1`，默认 8765 端口；端口占用时加 `--port 8766`。在启动终端按 Ctrl+C 停止，正在运行的工作线程会等待当前任务结束。未配置固定口令时每次启动生成新口令；刷新页面可沿用当前浏览器标签页 sessionStorage 中的口令，服务重启后请使用新链接。口令通过 URL 片段带入页面、随即从地址栏移除，并以 Authorization 请求头发送；它不是模型 API key。

本机口令可选配置为 `.env` 中的 `SCHEDULE_API_TOKEN`（至少 24 个 ASCII 字符），不要提交真实值。模型 API key 只由后端读取，不发送给前端。此版本不做公网部署，不将本机口令当作多用户权限系统。

## 评审操作

1. 选择“时段冲突 / 重复提交”，保持 Mock 或切换真实 DeepSeek。双 Agent 默认开启。
2. 提交后应看到原要求 18:00、建议 19:00、保存成功、等待用户确认、实际占位为空。
3. 在演示占用中同时勾选 18:00 和 19:00，保留业务请求 ID，再点“审核并生成排期草稿”。建议变成 20:00，草稿 ID 不变。
4. 点“原请求再运行一次”，应看到“已复用同一草稿”，草稿列表仍一行。该按钮复用上次提交快照；修改表单后的提交请使用主按钮。
5. 勾选明确同意 20:00 的确认框，再点确认按钮。后端根据上一任务的真实建议重建批准请求、重新审核与查询，再更新原草稿；没有直接写 confirmed 的接口。
6. 选择保存失败场景，程序应明确失败。工具失败不是成功；已有其他请求的草稿会继续保留。

切换场景会生成新的业务请求 ID；“新请求”也会生成新 ID。已确认草稿会占用时段，因此在同一工作台反复测试“正常排期”时可能遇到此前草稿的占位，这是预期。要完全隔离一批演示，可停止后使用新的 `--runtime .runtime/web-demo-2` 目录启动。无需删除旧数据。

页面从轮询接口显示 finalized Trace 事件，包含角色、参数、观察、后续动作和耗时；可下载完整 JSON。运行中末尾的一步可能直到下一动作已知才显示。Mock 会标注离线，真实调用有服务响应 ID 和 Token 用量。

## CLI 对照

```bash
PYTHONPATH=src python3 -m schedule_agent \
  --provider deepseek --env-file .env --orchestration team \
  --input examples/inputs/conflict.json \
  --db .runtime/team-cli.sqlite3 --output .runtime/team-cli.json \
  --max-steps 24 --max-total-tokens 60000 --max-seconds 180

python3 scripts/evaluate.py --provider deepseek --env-file .env \
  --orchestration team --output .runtime/team-live-new
```

把 `team` 改为 `single` 可运行对照。去掉 provider/env-file 参数则使用 Mock。预算在下一次调用前检查，不是严格费用上限；180 秒期限可能加上一个尚在进行的 API 请求耗时。

## HTTP API

除静态资源和健康检查，所有接口需要 `Authorization: Bearer 本机访问口令`。只接受本机 Host 和同源浏览器请求；不开放 CORS。模型服务名从后端已配置列表选择，请求不能指定任意模型 API 地址、文件路径或 SQL。

| 方法与路径 | 用途 |
| --- | --- |
| GET /healthz | 本机进程健康状态 |
| GET /api/config | 合成素材、时段、可用服务与演示开关，不返回凭据 |
| POST /api/runs | 提交任务，返回 202 与任务 ID |
| GET /api/runs/{id} | 查询 queued/running/finished/error/interrupted 和实际 Trace |
| POST /api/runs/{id}/confirm | 明确确认此前任务的实际建议，发起新一轮任务 |
| GET /api/drafts | 查看已持久化的草稿 |

HTTP `finished` 仅表示任务执行结束；业务是否成功看 `result.task_status` 和逐项 `save_status`，不能将 HTTP 200 或 finished 解释为保存成功。

使用终端隐藏输入读取本机访问口令（macOS 默认 zsh）：

```bash
read -s 'SCHEDULE_HTTP_TOKEN?本机访问口令: '
export SCHEDULE_HTTP_TOKEN
curl --fail-with-body http://127.0.0.1:8765/api/runs \
  -H "Authorization: Bearer $SCHEDULE_HTTP_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/inputs/http-conflict.json

# 替换 TASK_ID 为上一步返回的 id
curl --fail-with-body http://127.0.0.1:8765/api/runs/TASK_ID \
  -H "Authorization: Bearer $SCHEDULE_HTTP_TOKEN"
```

确认请求体例子：`{"material_id":"M01","slot":"2026-11-03T19:00:00+08:00"}`；只能是此前真实 pending_confirmation 建议。认证客户端直接提交标准请求 JSON 的 approved_slot 同样代表操作者批准；模型没有此参数。共享一个本机口令意味着共享操作者权限，尚未实现用户身份级确认凭证。

## 运行器边界

单进程、单任务工作线程，最多 8 个排队/运行任务，满时 HTTP 429。内存最多保留 100 个任务，历史最终结果可从本地文件读取；磁盘历史没有自动清理策略。每个任务默认 24 次模型调用，上限 48；网页固定 60000 Token 和 180 秒软预算。短期历史在内存；草稿 SQLite 持久化；请求/结果 JSON 与步骤 JSONL 在运行目录持久化。

任务中断不会自动续跑；重启后仅有请求记录但无最终结果的任务标记 interrupted。一次数据库提交可能早于最终报告写入，所以报告保存失败或崩溃时须查询草稿，复用业务 request_id 重试。没有发布接口，所有业务输出都标记 published=false。

## 跨天多平台活动工作台与接口

在原页面点击“跨天多平台活动”打开 `/campaign`，沿用同一访问口令。填写硬窗口、平台顺序/间隔与模型服务；“演示与执行预算”可将上限改为 11，完成审核后暂停，再改回 64 点击“从检查点继续”。活动列表从 SQLite 加载，服务重启后重新登录即可打开原活动继续。不会自动在次日运行。

待确认时逐项核对素材、平台与时间，勾选明确批准，再确认整组草稿；UI 提交页面显示的版本和哈希。输入框改动不会暗中改变原活动，修改硬要求须新建活动。已确认结果只是草稿。

所有下列 API 沿用本机 Bearer / Host / Origin 检查：

| 方法与路径 | 内容 |
| --- | --- |
| POST /api/campaigns | request 为活动输入；可选 provider、max_steps、演示专用 fail_save |
| GET /api/campaigns | 最近 100 个持久活动 |
| GET /api/campaigns/{request_id} | 原要求、当前检查点与草稿 |
| POST /api/campaigns/{request_id}/resume | 可选 provider、max_steps、fail_save；不允许覆盖 request |
| POST /api/campaigns/{request_id}/confirm | 必须精确提供 plan_version 与 plan_hash |

request_id 在 URL 中须编码。三个 POST 均返回 202 与运行 ID，轮询既有 `/api/runs/{id}` 读取实际执行结果；202 只表示已接收，不能当作保存成功。确认任务由本机队列执行，不调用模型。`/api/drafts` 返回单项与活动草稿，活动行带 `workflow=campaign`，单项网页以 C 前缀区分 ID。

输入格式、CLI 复现、11 个工具、恢复和安全边界见 [08-campaign-runtime.md](08-campaign-runtime.md)。
