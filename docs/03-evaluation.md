# 效果验证

## 当前结论

**已完成离线工程验证；真实模型决策效果尚未验证。**

本次实际运行的命令为 `python3 scripts/verify.py`，Python 3.9.6。测试日志见 [test-results.txt](../examples/test-results.txt)，环境与命令见 [verification.json](../examples/verification.json)。43 个自动化测试通过，18 组离线验收案例通过；精确时间以生成文件为准。

所有 `examples/mock/*.json` 由实际执行程序生成，包含规范化输入、实际输出、Trace、初始故障配置、测试断言和执行后的数据库快照。没有手写“模型决策日志”。`provider.is_real_model=false`，里面的行动依据来自确定性模拟器，**不能当作真实模型生成的决策**。

## 必须验收的实测结果

| 验收点 | 预期 | 实际离线结果 | 证据 |
| --- | --- | --- | --- |
| 正常排期 | M01 审核通过，19:00 保存 | completed，confirmed，reserved_slot=19:00 | [normal](../examples/mock/normal.json) |
| 信息缺失 | 补查失败后说明缺 rights | needs_input，无草稿 | [missing_info](../examples/mock/missing_info.json) |
| 规则风险 | 禁止声明阻断 | blocked，返回 BLOCKED_CLAIM，无草稿 | [risk](../examples/mock/risk.json) |
| 时段冲突 | 保留 18:00，建议 19:00 待确认 | pending_confirmation，reserved_slot=null | [conflict](../examples/mock/conflict.json) |
| 重复提交 | 相同请求只存一行 | draft_id 不变，duplicate=true | [首次](../examples/mock/duplicate_first.json)、[重复](../examples/mock/duplicate_repeat.json) |
| 保存失败 | 不能称保存成功 | failed，save_status=failed，数据库无行 | [save_failure](../examples/mock/save_failure.json) |

## 补充验证

| 情况 | 实际结果 | 证据 |
| --- | --- | --- |
| M02 缺字段但有补充资料 | 补查后重新审核并完成 | [supplement](../examples/mock/supplement.json) |
| 人工复核规则 | blocked，列出人工确认项 | [manual_review](../examples/mock/manual_review.json) |
| 18:00、19:00 都占用 | 建议变为 20:00 | [changed_occupancy](../examples/mock/changed_occupancy.json) |
| 全部时段占用 | incomplete，明确没有替代时段 | [no_slots](../examples/mock/no_slots.json) |
| 首次读取失败 | 观察错误后重读成功 | [read_recovery](../examples/mock/read_recovery.json) |
| 用户确认替代时间 | 同一行由待确认更新为已确认 | [确认前](../examples/mock/confirmation_before.json)、[确认后](../examples/mock/confirmation_after.json) |
| 确认后重放旧请求 | 不撤销既有确认 | [confirmation_replay](../examples/mock/confirmation_replay.json) |
| 未提供发布时间 | needs_input | [missing_time](../examples/mock/missing_time.json) |
| 上限设为 2 | incomplete，stop_reason=step_limit | [step_limit](../examples/mock/step_limit.json) |
| 批量含成功、冲突、风险 | partial，逐项真实状态保留 | [batch](../examples/mock/batch.json) |

两条离线路径的差异可直接对比 Trace：正常路径查询后保存；冲突路径查询失败后进一步查替代时段，再保存待确认草稿。更换占用结果后建议随之变化。这证明工具与状态链路可复现，不能证明真实模型会做同样的选择。

## 自动化测试覆盖

[test_business.py](../tests/test_business.py) 验证数据库实际状态、并发竞争、事务回滚、确认边界、审核失效、请求幂等及批量部分成功。

[test_runtime.py](../tests/test_runtime.py) 使用协议替身验证：实际工具结果确实回传到下一轮 Responses 请求；非法 JSON、重复 call_id、多工具并发、纯文本虚报、模型 HTTP 错误不会变成成功；秘密和模型连续推理项不写入 Trace。另有子进程 CLI 测试核对退出码与输出文件。

这些协议替身不是实际模型。CI 工作流已配置但未推送，不能将其写成已通过线上 CI。

## 发现并处理的问题

实现复核发现：确认替代时段后重放原始未确认请求，简单的覆盖写入可能撤销已保存的用户确认并释放占位。现采用单调确认状态，重复输入保持已有确认，并用回归测试与 confirmation_replay 实际记录验证。

另外，为避免“查时可用、保存时已被占用”，保存采用写事务内二次检查和唯一索引。两个独立连接并发保存同一时段时，测试验证仅一方成功，另一方得到 SLOT_CONFLICT。

## 未完成验证与限制

1. 开发环境未配置 `OPENAI_API_KEY`、`OPENAI_MODEL`；真实 API 连通性、模型工具选择质量和至少两条真实 ReAct 路径仍待验证。
2. 当前运行只有 Python 3.9.6 / macOS 实测；其他 Python/OS 的 CI 结果待仓库推送后获得。
3. 未评估真实内容审核准确率、语义绕过、多语言风险和大规模吞吐。合成禁词规则不能替代生产审核。
4. 无 UI、身份鉴权、真实平台同步、实时外部排期事务、草稿撤销、从失败步骤 checkpoint 续跑。重复执行会重新读取/审核/查询，成功保存步骤通过幂等复用。
5. 批量顺序执行不保证最优排期；若前项耗尽共享预算，后项明确未完成。

## 补齐真实模型证据

配置环境变量后运行：

```bash
python3 scripts/evaluate.py --provider openai --output examples/live
```

这会使用同一批合成输入、隔离数据库和断言。不要只看“运行结束”，应检查 `summary.json` 中实际通过数和失败原因。至少核对：正常路径模型在保存后结束；冲突路径模型观察时段不可用后主动查询替代；保存失败后模型不返回 completed。然后更换占用时段复跑，核对下一步动作。

如模型执行不符合预期，保留原始记录，修改提示词/工具说明后写入新目录再评估。不要修改生成的 Trace 或仅凭 Mock 结果宣称达标。
