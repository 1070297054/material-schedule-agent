# 提交清单

- 题号：**1 — 素材审核与发布排期 Agent**。
- GitHub URL：[https://github.com/1070297054/material-schedule-agent](https://github.com/1070297054/material-schedule-agent)。
- 最终 commit SHA：提交前用 `git rev-parse HEAD` 获取并填写到考核提交表。
- 仓库可见性：公开。如之后改为私有，须给指定评审账号只读访问。

当前材料包括代码、可执行测试、合成资料、单/双 Agent 各 21 组实际离线案例、真实 DeepSeek 单/双 Agent 执行记录（含修复前失败记录）、Trace、测试日志、五份规定文档、依赖文件与 `.env.example`。新增三角色跨天活动的 14 组离线/真实回归记录、92 项测试和本机网页暂停/恢复/确认验收。

真实模型的正常、冲突等动态路径已保存在 examples/live/，结果和失败分析见 03-evaluation.md。仍需候选人如实补充个人贡献、独立验证记录、实际投入时间与个人学习收获；不能把 AI 执行的工作写成独立完成。

推送前先运行 `python3 scripts/verify.py`。配置真实模型后运行 `python3 scripts/evaluate.py --provider deepseek --env-file .env --orchestration team --output .runtime/live-recheck`，核对结果并更新评估文档。真实 Trace 目录默认被忽略，检查无敏感信息后可用 `git add -f examples/live` 加入提交。

本机完成 GitHub CLI 授权后，可同步当前 main 分支并提交后续修改。当前远程地址为：

```bash
git remote -v
git push origin HEAD:main
git rev-parse HEAD
```

最后提交：题号、真实 GitHub URL、最终 commit SHA。不要提交密钥、`.runtime/` 数据库或虚构的个人贡献。
