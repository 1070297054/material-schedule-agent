# 提交清单

- 题号：**1 — 素材审核与发布排期 Agent**。
- GitHub URL：**尚未提供远程仓库，未推送**。
- 最终 commit SHA：提交前用 `git rev-parse HEAD` 获取并填写到考核提交表。
- 仓库可见性：如使用私有仓库，须给指定评审账号只读访问；当前未提供评审账号。

当前材料包括代码、可执行测试、合成资料、18 组实际离线案例、Trace、测试日志、五份规定文档、依赖文件与 `.env.example`。

仍需补齐的考核证据：真实模型至少两条动态决策路径，以及候选人真实的个人贡献、验证记录与投入时间。没有这些材料，不能将当前版本描述为完整通过考核。

推送前先运行 `python3 scripts/verify.py`。配置真实模型后运行 `python3 scripts/evaluate.py --provider openai --output examples/live`，核对结果并更新评估文档。真实 Trace 目录默认被忽略，检查无敏感信息后可用 `git add -f examples/live` 加入提交。

创建自己的 GitHub 仓库后，可按实际地址执行：

```bash
git remote add origin https://github.com/<你的账号>/<你的仓库>.git
git push -u origin HEAD
git rev-parse HEAD
```

最后提交：题号、真实 GitHub URL、最终 commit SHA。不要提交密钥、`.runtime/` 数据库或虚构的个人贡献。
