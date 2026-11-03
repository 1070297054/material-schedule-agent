# 素材审核与发布排期 Agent

小影校招 AI Agent 开发考核 · 第 1 题。帮助内容运营审核素材，发现排期冲突，保存可确认的草稿。**只保存草稿，没有发布接口。**

Python 标准库 + SQLite + 原生 Responses API 工具调用，不依赖 Agent 框架。提供真实模型接入和可复现的离线模拟入口。

> **验证状态：已接入真实 DeepSeek `deepseek-flash`。** [真实正常路径](examples/live/deepseek-smoke/normal.json)与[真实冲突路径](examples/live/deepseek-smoke/conflict.json)已通过：模型观察时段冲突后查询替代时间，保存待确认草稿，再观察保存结果结束。完整验收结果见[真实模型汇总](examples/live/deepseek-final/summary.json)。Mock 与真实模型记录分开保存；一次通过不代表所有模型和任意输入都能通过。

## 评审入口

1. 运行 `python3 scripts/verify.py`，执行测试并重新生成离线验收记录。
2. 查看 [测试结果](examples/test-results.txt)、[验证摘要](examples/verification.json) 和 [18 组案例汇总](examples/mock/summary.json)。
3. 对比真实模型的 [正常路径](examples/live/deepseek-final/normal.json)、[冲突路径](examples/live/deepseek-final/conflict.json)、[更换占用时段](examples/live/deepseek-final/changed_occupancy.json)。离线对照记录保留在 `examples/mock/`。
4. 查看 [Agent 循环](src/schedule_agent/agent.py)、[真实模型接入](src/schedule_agent/providers.py)、[工具边界](src/schedule_agent/tools.py) 与 [数据库事务](src/schedule_agent/storage.py)。
5. 配置自己的模型密钥后执行本文的真实评估命令，复核模型动态决策；密钥不在仓库中。

## 环境与运行

Python **3.9 或以上**。本地实测 Python 3.9.6 / macOS；CI 配置了 3.9、3.11、3.13，最新执行状态见 [GitHub Actions](https://github.com/1070297054/material-schedule-agent/actions)。无需安装第三方运行依赖。以下命令均在仓库根目录执行，macOS/Linux shell：

```bash
# 一键验证，无需 API 密钥或网络
python3 scripts/verify.py

# 正常排期：19:00 可用
PYTHONPATH=src python3 -m schedule_agent \
  --input examples/inputs/normal.json \
  --db .runtime/normal.sqlite3 \
  --output .runtime/normal.json

# 冲突：18:00 被占用，保存 19:00 待确认建议稿
PYTHONPATH=src python3 -m schedule_agent \
  --input examples/inputs/conflict.json \
  --db .runtime/conflict.sqlite3 \
  --output .runtime/conflict.json
```

默认 `--provider mock`；输出始终标记 `provider.mode`、`is_real_model` 和验证边界。Mock 是确定性测试模拟器，不是模型。

CLI 退出码：`0` 所有素材完成草稿保存；`2` 待确认、缺信息、阻断、未完成或混合结果；`1` 配置/输入错误或任一素材执行失败。冲突示例返回 `2` 属于预期。

Windows PowerShell 可先执行 `$env:PYTHONPATH="src"`，再运行 `python -m schedule_agent ...`。也可在虚拟环境中 `python3 -m pip install -e .`，然后使用 `schedule-agent` 命令；此安装方式可能需要下载 setuptools，离线评审推荐上面的免安装方式。

## 输入与确认

```json
{
  "request_id": "campaign-001",
  "items": [
    {
      "material_id": "M01",
      "requested_slot": "2026-11-03T18:00:00+08:00",
      "approved_slot": null
    }
  ]
}
```

- `request_id`：同一业务请求重试时保持不变；与素材 ID 共同组成幂等键。
- `requested_slot`：原始硬性要求，缺失时请求补充。日期为固定合成案例，不随当前日期改变。
- `approved_slot`：**只有用户明确同意后**才填入的替代时段。模型工具没有此参数。
- 支持 1–20 个素材，按输入顺序处理；模型调用预算为整个请求共享，默认 24 次。
- 本原型限定 Asia/Shanghai 整点时段、同平台同一时段容量为 1；同一天的可用窗口由 `data/schedule.json` 指定。

用户确认 19:00 后，用相同请求 ID 和原始要求再次执行：

```bash
PYTHONPATH=src python3 -m schedule_agent \
  --input examples/inputs/confirmed.json \
  --db .runtime/conflict.sqlite3 \
  --output .runtime/confirmed.json
```

此操作更新同一草稿并占用 19:00。待确认建议稿不占位；确认时会重新检查可用性。模型只能建议改期，不能代替用户批准。CLI 输入文件由运行者控制，身份认证不在本原型范围内。

输出包含每项素材的 `task_status`、审核结果与风险、原始/批准/建议时段、`pending_items`、`save_status`、数据库返回的 `draft`、去重标记与错误。`completed` 仅代表草稿链路完成，**不代表已发布**。批量状态不同则总体为 `partial`，逐项结果保留。

## 真实模型运行

使用支持 **Responses API function calling** 的模型，将模型名称填为账户实际可访问的型号。程序不会读取 Codex 登录凭据，也不会在真实模型失败时悄悄切换 Mock。

推荐复现本次使用的 DeepSeek 模型。复制 `.env.example` 为本机 `.env`，在编辑器中填入自己的 `DEEPSEEK_API_KEY`；保留 `DEEPSEEK_MODEL=deepseek-flash` 和 `DEEPSEEK_BASE_URL=https://api.deepseek.com`。`.env` 已被 Git 忽略，命令通过 `--env-file` 显式加载，既有环境变量优先，配置内容不会被当作 shell 执行。

```bash
PYTHONPATH=src python3 -m schedule_agent \
  --provider deepseek --env-file .env \
  --input examples/inputs/conflict.json \
  --db .runtime/deepseek.sqlite3 \
  --output .runtime/deepseek-conflict.json

# 先只运行正常与冲突两条路径
python3 scripts/evaluate.py --provider deepseek --env-file .env \
  --cases normal conflict --output .runtime/live-smoke

# 全部 18 组真实验收，会消耗 API 额度
python3 scripts/evaluate.py --provider deepseek --env-file .env \
  --output .runtime/live-full
```

DeepSeek 使用 Responses API 和非思考模式，保留公开的一句话行动依据，不收集内部思维过程。它不保证禁止并行工具调用，因此运行器会拒绝整批提议，将未执行结果逐一返回给模型，要求重新选择单个工具；次数计入共享执行上限。接口取舍见 [DeepSeek 官方兼容说明](https://api-docs.deepseek.com/guides/responses_api/)。

也支持原有 OpenAI 服务，在终端设置以下环境变量，或在本机 `.env` 中填写后加 `--env-file .env`：

```bash
export OPENAI_API_KEY='替换为你的真实密钥'
export OPENAI_MODEL='替换为支持工具调用的模型名称'
# 可选：默认 https://api.openai.com/v1；其他服务必须兼容 Responses 协议
# export OPENAI_BASE_URL='https://你的服务/v1'

PYTHONPATH=src python3 -m schedule_agent \
  --provider openai \
  --input examples/inputs/conflict.json \
  --db .runtime/live.sqlite3 \
  --output examples/live/conflict.json

# 同一套用例与断言切换到真实模型，运行会消耗 API 额度
python3 scripts/evaluate.py --provider openai --output examples/live
```

真实调用采用本地严格参数校验、每轮至多执行一个工具、45 秒请求超时和执行次数上限。OpenAI 还请求服务端严格 schema。调用方式依据 [OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)：工具结果返回给模型后由模型选择后续动作。模型的连续上下文保存在内存，Trace 不保存内部推理内容，只记录简短行动依据与响应 ID。

`examples/live/` 默认忽略。提交前人工检查实际 Trace，确认无敏感信息后执行 `git add -f examples/live`。至少应有正常和冲突两条真实路径；如果失败，也应如实保留结果并更新评估文档，不修改记录伪装成功。

## 故障、重复与限制演示

```bash
# 在同一个数据库上重复 normal 命令，不会增加第二份草稿
PYTHONPATH=src python3 -m schedule_agent --input examples/inputs/normal.json --db .runtime/normal.sqlite3

# 保存失败：实际无新增草稿、save_status=failed、退出码 1
PYTHONPATH=src python3 -m schedule_agent --input examples/inputs/normal.json --db :memory: --fail save_draft:1

# 达到上限：incomplete + stop_reason=step_limit
PYTHONPATH=src python3 -m schedule_agent --input examples/inputs/normal.json --db :memory: --max-steps 2

# 将验收结果写到新目录，保留已有证据
python3 scripts/verify.py --output .runtime/verification
```

`--fail` 仅为演示故障注入，初始注入配置记录在输出中。模型收到的是实际工具错误。数据库测试还使用真实 SQLite 失败触发器验证回滚，不仅依赖 Mock 错误。

## 目录与提交

```text
src/schedule_agent/       CLI、Agent 循环、模型、规则、工具、SQLite
tests/                   可执行业务、并发、模型协议和 CLI 测试
data/                    合成素材、补充资料、规则、时段
examples/inputs/          输入文件
examples/mock/            实际离线输出、Trace、断言和数据库快照
examples/live/            已审查的真实 DeepSeek 执行记录和验收断言
examples/test-results.txt 实际测试日志
docs/01-requirements.md   需求与成功标准
docs/02-solution.md       动态决策与技术取舍
docs/03-evaluation.md     预期、实测、失败与限制
docs/04-ai-usage.md       AI 使用披露与待补个人贡献
docs/05-retrospective.md  实现复盘与下一步
scripts/                 一键验证、可切换真实模型的评估脚本
```

GitHub 仓库：[1070297054/material-schedule-agent](https://github.com/1070297054/material-schedule-agent)。最终 commit SHA 可用 `git rev-parse HEAD` 获取，见 [提交清单](docs/submission.md)。不要将当前 Mock 证据描述为真实模型决策结果；候选人需要亲自运行、理解和修改，并如实补充个人贡献。
