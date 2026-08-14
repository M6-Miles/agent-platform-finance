# 企业部署与运维基线

小范围单机演示的可执行部署包位于 `deploy/`，完整操作步骤见 `deploy/README.md`。Ubuntu 22.04 默认 Python 3.10，因此部署包使用官方 Python 3.11 Docker 镜像，避免修改服务器系统 Python。

## 强制配置

生产环境必须配置：

```env
APP_ENV=production
AUTH_ENABLED=true
AUTH_SECRET=<独立生成的至少32字符随机值>
ALLOWED_ORIGINS=https://your-frontend.example.com
TRUSTED_HOSTS=your-api.example.com
LANGGRAPH_USE_MEMORY_SAVER=false
```

服务在生产环境关闭认证、使用短密钥或通配符域名时会拒绝启动。密钥不得写入代码、镜像或版本库，应由 Secret Manager 注入。

## 安全边界

- 首次注册用户为管理员；初始化后公开注册自动关闭。
- 用户资源通过 `resource_ownership` 隔离；普通用户无法读取他人的会话、模拟账户、监控任务或研究线程。
- 模拟盘只连接本地 `MockBroker`，不连接真实券商，不执行真实下单。
- 应在反向代理终止 TLS，并设置 HSTS。应用内 CSP、TrustedHost、CORS 和安全响应头是第二层保护。
- 应在 API 网关或 Redis 实现跨实例限流。应用自带限流只覆盖单进程，不能替代分布式限流。

## 数据库与扩容

- SQLite 已启用 WAL、`busy_timeout`、外键和模式版本。
- 模拟订单使用 `BEGIN IMMEDIATE`，幂等检查、账户更新和响应留存处于同一事务。
- 定时监控使用数据库租约，避免多个 worker 重复调度。
- SQLite 适合单机或低并发部署。需要多主机横向扩容时应迁移 PostgreSQL，并将调度任务移至独立 worker。

## 健康与备份

- `/health`：进程存活探针。
- `/ready`：数据库完整性、LangGraph checkpoint 与监控线程状态。
- `POST /admin/database/backup`：管理员在线备份并执行完整性检查。
- `POST /admin/database/retention`：管理员清理超过保留期的运行数据。

建议每天备份，至少保留 7 个每日副本和 4 个每周副本。每季度执行恢复演练；仅生成备份文件不等于备份机制已验证。

## 已知边界

- 当前交易日历默认只能判断工作日候选日期，尚非交易所权威节假日日历。
- 样本外 Sharpe、真实 LLM 效果和 7 至 14 个真实交易日运行证据必须按实际结果验收，不能由代码测试替代。
- 前端仍为单体 HTML，后续应拆分构建并把 Tailwind、字体资源本地化。
