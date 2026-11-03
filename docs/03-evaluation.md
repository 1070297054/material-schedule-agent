# 效果验证

## 当前结论

**当前版本：真实 DeepSeek 双 Agent 最终验收 21/21，通过 67 个自动化测试；单 Agent 与双 Agent 的离线验收各 21/21。** 早期单 Agent 的真实 18/18 原始记录继续保留。 这证明本次合成用例中的工程行为与模型决策符合断言，不代表任意输入下的成功率。

真实调用使用 `https://api.deepseek.com/responses`，显式非思考模式。业务工具、素材和故障均为本地合成环境；模型选择工具并接收实际工具结果。密钥不包含在仓库或 Trace 中。

| 实际运行 | 结果 | 原始证据 |
| --- | --- | --- |
| 正常与冲突连通验证 | 2/2 | [smoke 汇总](../examples/live/deepseek-smoke/summary.json) |
| 首轮完整验收 | 17/18，保存失败场景不符合预期 | [首轮汇总](../examples/live/deepseek-full/summary.json) |
| 修复后的保存失败验证 | 1/1 | [修复验证](../examples/live/deepseek-save-fix/summary.json) |
| 修复后的完整验收 | 18/18 | [最终汇总](../examples/live/deepseek-final/summary.json) |

生成时间以文件为准。每个案例包含输入、模型信息、实际输出、Trace、响应 ID、工具参数与返回结果、简要决策依据、后续动作、用量、断言及数据库快照；不保存模型内部思维过程。首轮失败记录原样保留。

离线日志见 [test-results.txt](../examples/test-results.txt)，运行环境见 [verification.json](../examples/verification.json)。其中 `real_model_validation=not_run_by_this_command` 表示该离线命令本身不调用真实模型；真实证据在上表对应目录。Mock 的 `is_real_model=false`，其依据来自确定性模拟器，不能当作模型决策证据。

## 两条真实动态路径

[正常路径](../examples/live/deepseek-final/normal.json)：模型读取素材 → 审核 → 查询要求的 19:00 → 保存草稿 → 观察保存成功 → 结束。

[冲突路径](../examples/live/deepseek-final/conflict.json)：模型读取素材 → 审核 → 查询要求的 18:00，观察已占用 → 查询替代时段 → 保存 19:00 建议稿 → 观察待确认状态 → 返回 pending_confirmation。原始 18:00 要求保留，草稿 `reserved_slot=null`，没有擅自确认或发布。

[更换占用时段](../examples/live/deepseek-final/changed_occupancy.json)后，18:00 和 19:00 都不可用，模型建议变为 20:00，仍待确认。动作由模型根据实际观察选择。

## 必须验收的实测结果

| 验收点 | 预期 | 真实模型实际结果 | 证据 |
| --- | --- | --- | --- |
| 正常排期 | 审核通过后保存 19:00 | completed，confirmed，reserved_slot=19:00 | [normal](../examples/live/deepseek-final/normal.json) |
| 信息缺失 | 补查后仍缺资料则请求补充 | needs_input，缺 rights，无草稿 | [missing_info](../examples/live/deepseek-final/missing_info.json) |
| 规则风险 | 禁止声明阻断 | blocked，返回 BLOCKED_CLAIM，无草稿 | [risk](../examples/live/deepseek-final/risk.json) |
| 时段冲突 | 保留要求，替代方案待确认 | pending_confirmation，建议 19:00，未占位 | [conflict](../examples/live/deepseek-final/conflict.json) |
| 重复提交 | 相同请求只产生一份草稿 | 一行、draft_id 不变、duplicate=true | [首次](../examples/live/deepseek-final/duplicate_first.json)、[重复](../examples/live/deepseek-final/duplicate_repeat.json) |
| 保存失败 | 明确失败，不虚报成功 | failed，save_status=failed，数据库无行 | [save_failure](../examples/live/deepseek-final/save_failure.json) |

## 补充验证

| 情况 | 真实模型实际结果 | 证据 |
| --- | --- | --- |
| 缺字段但有补充资料 | 补查、重新审核后完成 | [supplement](../examples/live/deepseek-final/supplement.json) |
| 人工复核规则 | 阻断并列出确认项 | [manual_review](../examples/live/deepseek-final/manual_review.json) |
| 18:00、19:00 均占用 | 建议 20:00，待确认 | [changed_occupancy](../examples/live/deepseek-final/changed_occupancy.json) |
| 无任何可用时段 | 查询原时段和替代时段后主动结束，incomplete | [no_slots](../examples/live/deepseek-final/no_slots.json) |
| 首次读取失败 | 模型观察错误后重新读取，最终完成 | [read_recovery](../examples/live/deepseek-final/read_recovery.json) |
| 用户确认替代时段 | 同一草稿由待确认更新为已确认 | [确认前](../examples/live/deepseek-final/confirmation_before.json)、[确认后](../examples/live/deepseek-final/confirmation_after.json) |
| 确认后重放旧请求 | 保留既有确认与占位 | [confirmation_replay](../examples/live/deepseek-final/confirmation_replay.json) |
| 缺少要求时间 | needs_input | [missing_time](../examples/live/deepseek-final/missing_time.json) |
| 执行上限为 2 | incomplete，stop_reason=step_limit | [step_limit](../examples/live/deepseek-final/step_limit.json) |
| 批量成功、冲突、风险混合 | partial，保留逐项真实状态 | [batch](../examples/live/deepseek-final/batch.json) |

## 失败、修复与验证改进

首轮真实验收的 [save_failure](../examples/live/deepseek-full/save_failure.json) 未通过：保存故障被错误标记为可重试，模型观察错误后再次保存，实际保存成功并结束。它没有虚报数据库成功，但不符合本项目“本次保存故障明确结束为失败”的约定。提示词与工具错误契约不一致是问题所在。

修复后，非时段冲突的保存故障标记为不可重试，工具层阻止本次执行再次写入，模型被要求返回 failed。用户仍可使用相同请求 ID 发起新一轮执行。时段冲突允许重新查询，可恢复的读取错误仍允许重试。[单项复测](../examples/live/deepseek-save-fix/save_failure.json)和[最终完整复测](../examples/live/deepseek-final/save_failure.json)均观察到错误后明确返回失败且数据库为空。新增测试同时验证同轮不能重试写入、新一轮可恢复。

评估脚本复核还发现：只判断 no_slots 的最终 incomplete，会让重复调用直到耗尽预算的错误路径通过。现在必须观察原时段无可用结果、替代时段无可用结果，再由模型 finish；step_limit 不能冒充正确决策。测试以持续读取的协议替身验证该反例不能通过。

DeepSeek 可能返回一批工具提议。运行器拒绝整批执行，逐个 call_id 返回未执行结果，由模型重新选择一个工具。协议替身测试验证配对结果回传及无提前写入；这些测试与真实模型证据分别标注。

既有工程验证还覆盖：确认后重放旧请求不能撤销确认；保存事务内二次检查时段和唯一索引，使并发争抢同一时段只有一个成功。

## 自动化测试

- [test_business.py](../tests/test_business.py)：数据库实际状态、并发竞争、回滚、确认边界、审核失效、幂等、保存故障隔离及批量部分成功。
- [test_runtime.py](../tests/test_runtime.py)：工具结果回传、非法 JSON、重复 call_id、纯文本虚报、HTTP 错误、密钥及内部推理不写入 Trace、CLI 退出码。
- [test_deepseek.py](../tests/test_deepseek.py)：DeepSeek 请求配置、多工具拒绝后的模型重新选择、独立凭据、配置文件不执行 shell、不泄露配置错误内容，以及评估假阳性和用例依赖。

当前 67 个测试全部通过。新增 test_team.py 与 test_server.py 验证角色权限、可信交接、共享预算、HTTP 鉴权/跨源/请求体边界、占用变更后重复提交、明确确认及中断状态。CI 运行离线测试，Python 3.9、3.11、3.13 的结果以对应提交的 [GitHub Actions](https://github.com/1070297054/material-schedule-agent/actions) 为准；CI 不包含真实模型费用与凭据。

## 限制与未完成项

1. 真实验收使用有限合成用例，最终一次全量通过不能代表统计稳定性或任意输入的泛化效果；未测试全部模型和服务商。
2. 未评估真实审核准确率、语义绕过、多语言风险和大规模吞吐。合成规则不等于生产内容审核能力。
3. 已有本机 UI/HTTP 和访问口令，但未做多用户身份鉴权、真实平台同步、外部排期事务、草稿撤销或从失败步骤 checkpoint 续跑。重复执行重新读取、审核、查询，通过幂等复用保存结果。
4. 批量顺序执行不保证全局最优；共享预算耗尽时后续素材明确未完成。
5. 候选人个人投入时间、独立验证和个人学习收获仍需本人如实补充，不能把 AI 的执行记录当作个人贡献。

## 复现命令

```bash
# 离线验证，不消耗 API 额度
python3 scripts/verify.py --output .runtime/offline-recheck

# 已在本机 .env 配置 DeepSeek 后执行，会消耗 API 额度
python3 scripts/evaluate.py --provider deepseek --env-file .env --orchestration team --output .runtime/live-recheck
```

可加 `--cases normal conflict` 先复现两条路径。完整验收自动运行全部 21 组，用隔离数据库测试；指定重复或确认用例时自动包含前置执行。应检查 summary.json 的断言、Trace 和数据库快照，保留失败原始记录，禁止手改运行结果。


## 本机网页与双 Agent 增量验收

真实双 Agent 初次验收 [20/21](../examples/live/deepseek-team-v2/summary.json)，最终完整复测 [21/21](../examples/live/deepseek-team-final/summary.json)。初次唯一未通过为 [missing_info](../examples/live/deepseek-team-v2/missing_info.json)：读取基础资料后模型直接发现缺 rights，先补查，再审核并返回 needs_input。业务结果正确，但旧断言强制要求补查前必须先 check_rules。这是评估误判，并非模型越权。

修复后的评估接受两种有事实依据的路径：正式审核发现缺字段，或基础资料本身显示缺字段；二者均须执行补查并在之后审核。新增正反例测试证明前者/后者合理时可通过，删去补查步骤仍不能通过。没有改写原始 Trace，也没有取消保存、确认、风险或结束状态约束。

[占用变更前](../examples/live/deepseek-team-final/occupancy_repeat_before.json) → [变更后同请求](../examples/live/deepseek-team-final/occupancy_repeat_after.json) → [再次重复](../examples/live/deepseek-team-final/occupancy_repeat_again.json)共享同一测试数据库。建议从 19:00 改为 20:00；原 18:00 要求保留；三次只有一行草稿、相同 draft_id，未批准时 reserved_slot=null，最后 duplicate=true。

[正常](../examples/live/deepseek-team-final/normal.json)与[冲突](../examples/live/deepseek-team-final/conflict.json)包含审核 Agent 发起 handoff_schedule，运行器提供实际审核状态，再由排期 Agent 查询/保存的真实响应 ID。两个角色共用 deepseek-flash，拥有不同指令与工具权限，不是同时并行运行，也没有借助多角色伪造用户授权。

[双 Agent 离线汇总](../examples/team-mock/summary.json)与单 Agent 离线案例使用同一验收目标，均 21/21。当前数据集由项目开发过程手工构造，属于开发/回归集，不是独立隐藏测试集，不以 21/21 推断生产成功率。尚未做随机化多轮稳定性或单/双 Agent 的统计效果对比。

网页实际交互验证见 [UI 验证记录](../examples/ui/verification.json)：Mock 场景先冲突、修改占用、重复、明确确认，再从网页运行真实 DeepSeek 正常场景。五次原始 HTTP 结果与 [真实结果截图](../examples/ui/workbench-live.png)保存在 examples/ui；截图仅辅助核验，不替代代码和 Trace。真实网页运行 6 次模型调用、8910 Token，实际保存成功。
