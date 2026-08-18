# Skill 市场使用说明

项目支持可视化 Skill 市场。用户登录后可以搜索、查看说明、版本、权限、来源和 SHA256，并把 Skill 安装到自己的账号，无需修改 Agent 或底层代码。

## 用户隔离

每个账号拥有独立目录：

```text
data/user_skills/<user_id>/<skill_name>/
```

用户 A 安装、启用、停用或删除 Skill 都不会影响用户 B。Agent 每次对话只加载当前用户已启用的 Skill。生产环境必须启用认证；认证关闭时只能使用 `anonymous` 演示用户，不能提供用户隔离。

## 页面操作

1. 登录平台。
2. 打开左侧“Skill 市场”。
3. 输入名称、说明或作者进行搜索。
4. 查看权限、来源和完整 SHA256。
5. 点击“下载并安装到我的账号”。
6. 安装后可以启用、停用或删除。
7. 启用后，可执行插件注册为 Agent 工具；说明型 Skill 注入当前账号的 Agent 工作流程。

兼容性说明：纯说明型 Skill 可以直接参与 Agent 对话；依赖本平台已有工具的 Skill 可以使用相应部分；依赖 Figma、Notion、Playwright、GitHub CLI、MCP 或专用脚本的 Skill 只安装说明，必须额外接入对应工具后才能完整运行。下载成功不等于外部依赖已经安装。

## 市场安全

市场目录位于 `skill_catalog/catalog.json`，只接受受信任项目目录中的 Skill 包。安装过程会：

- 限制来源路径，拒绝目录越界。
- 重新计算目录文件的 SHA256 并与目录声明比对。
- 校验 `skill.json`、入口函数和包结构。
- 检查危险模块导入。
- 拒绝交易、下单、Broker 和数据库写入权限。
- 在临时目录完成校验后再写入用户目录。

平台区分两类 Skill：`plugin` 是经过审查的本地 Python 工具，`instruction` 是只读工作流程。官方 GitHub Skill 按 `instruction` 安装，平台只下载 `SKILL.md`，不会下载或执行仓库脚本。

## API

```text
GET    /skill-marketplace?q=关键词
POST   /skill-marketplace/{id}/install
GET    /skills
POST   /my/skills/reload
PATCH  /my/skills/{name}       {"enabled": false}
DELETE /my/skills/{name}
POST   /skills/{name}/run      {"arguments": {...}}
```

## Skill 包格式

```text
skills/my_skill/
├── skill.json
└── handler.py
```

```json
{
  "name": "my_skill",
  "version": "1.0.0",
  "description": "说明这个 Skill 做什么",
  "entrypoint": "handler:run",
  "permissions": [],
  "allowed_agents": ["chat_agent"]
}
```

入口函数接收 JSON 字段并返回可序列化对象：

```python
def run(text: str) -> dict:
    return {"result": text, "source": "local_skill/my_skill"}
```

项目内置 `skills/text_summary` 作为市场最小示例。

## 官方 GitHub Skill 目录

默认接入两个可审查的公开来源：

```env
SKILL_GITHUB_SOURCES=openai/skills|skills/.curated,anthropics/skills|skills
```

平台先读取仓库当前提交和递归文件树，市场展示来源、提交版本和 Git 对象哈希；列表阶段不会逐个下载文件。安装时才从固定提交下载所选 `SKILL.md`，校验 Git 对象完整性并计算、保存 SHA256，之后仅写入当前用户目录。外部仓库中的脚本、二进制文件和其他附件不会被安装或执行。目录缓存 30 分钟，GitHub 暂时不可用时仍显示本地 Skill 并给出明确告警。

说明型 Skill 只能补充工作流程，不能覆盖平台安全规则、读取密钥、扩大工具权限或调用真实交易接口。OpenAI 与 Anthropic 同名 Skill 会使用来源前缀，例如 `openai_pdf` 与 `anthropic_pdf`，不会互相覆盖。

## 自建远程插件目录

远程目录必须是 HTTPS JSON 地址，内容格式可参考 `skill_catalog/remote_catalog.example.json`。每个条目必须提供 `download_url` 和下载压缩包的 SHA256：

```env
SKILL_REGISTRY_URLS=https://your-approved-domain.example/skill-catalog.json
```

多个目录用英文逗号分隔。平台每 5 分钟缓存一次目录，远程目录暂时不可用时保留本地结果并在页面显示告警。下载地址必须使用 HTTPS，并且域名必须与目录来源域名一致；本地测试才允许 `127.0.0.1` 的 HTTP。

可以把 `catalog.json` 和 Skill 压缩包托管在 GitHub Releases、GitHub Pages、公司对象存储或自建 HTTPS 服务。需要执行 Python 的第三方 Skill 仍必须转换为 `skill.json + handler.py`，加入受信任目录并经过人工代码审查；官方 `SKILL.md` 不会自动升级为可执行代码。
