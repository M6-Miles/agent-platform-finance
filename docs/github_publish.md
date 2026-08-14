# GitHub 整理与上传

## 发布建议

首次发布建议创建 **Private（私有）** 仓库。项目包含个人实习文档、真实 LLM 离线评测结果和金融研究材料；改为 Public 前，应确认导师、公司和数据许可均允许公开。

不要上传 `.env`、`.env.production`、API Key、SQLite 数据库、日志、备份、虚拟环境或部署压缩包。仓库已通过 `.gitignore` 排除这些文件。

## 1. 发布前检查

在项目根目录执行：

```powershell
git status --short
git diff --check
git grep -n -E "sk-[A-Za-z0-9_-]{20,}|BEGIN .*PRIVATE KEY"
```

第三条命令没有输出才是预期结果。示例占位符应写成 `your_key`，不要使用真实 Key。

## 2. 创建 GitHub 仓库

登录 GitHub，点击右上角 `+` -> `New repository`：

- Repository name：建议 `agent-platform-finance`
- Visibility：选择 `Private`
- 不要勾选初始化 README、`.gitignore` 或 License

创建后复制仓库的 HTTPS 地址，例如：

```text
https://github.com/你的用户名/agent-platform-finance.git
```

## 3. 连接并首次推送

```powershell
git branch -M main
git remote add origin https://github.com/你的用户名/agent-platform-finance.git
git push -u origin main
```

GitHub 不再接受账户密码作为 Git 密码。Git for Windows 通常会弹出浏览器登录；按页面授权即可。若使用 Personal Access Token，只授予该仓库所需的最小权限，并且不要把 Token 写进命令、文档或截图。

## 4. 后续更新

```powershell
git status --short
git add <本次确实要提交的文件>
git commit -m "说明本次修改"
git push
```

不要使用 `git add -A` 盲目提交本地数据库、报告草稿或非预期删除。每次提交前用 `git diff --cached --stat` 和 `git diff --cached` 检查暂存内容。

## 5. GitHub 验收

推送后打开仓库的 `Actions` 页面。CI 应执行 Python 3.11、静态检查和离线测试；默认使用 Mock LLM 与样例行情，不产生 DeepSeek 费用，也不会触发真实交易。

仓库主页应至少能看到 `README.md`、`SECURITY.md`、`CONTRIBUTING.md`、`.env.example`、`src/`、`tests/` 和 `deploy/`。
