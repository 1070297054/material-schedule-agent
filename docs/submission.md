# 提交清单

- 题号：**1 — 素材审核与发布排期 Agent**。
- GitHub URL：[https://github.com/1070297054/material-schedule-agent](https://github.com/1070297054/material-schedule-agent)。
- 最终 commit SHA：提交前用 `git rev-parse HEAD` 获取并填写到考核提交表。
- 仓库可见性：公开。如之后改为私有，须给指定评审账号只读访问。

当前材料包括代码、可执行测试、合成资料、18 组实际离线案例、Trace、测试日志、五份规定文档、依赖文件与 `.env.example`。

仍需补齐的考核证据：真实模型至少两条动态决策路径，以及候选人真实的个人贡献、验证记录与投入时间。没有这些材料，不能将当前版本描述为完整通过考核。

推送前先运行 `python3 scripts/verify.py`。配置真实模型后运行 `python3 scripts/evaluate.py --provider openai --output examples/live`，核对结果并更新评估文档。真实 Trace 目录默认被忽略，检查无敏感信息后可用 `git add -f examples/live` 加入提交。

本机完成 GitHub CLI 授权后，可同步当前 main 分支并提交后续修改。当前远程地址为：

```bash
git remote -v
git push origin HEAD:main
git rev-parse HEAD
```

最后提交：题号、真实 GitHub URL、最终 commit SHA。不要提交密钥、`.runtime/` 数据库或虚构的个人贡献。
