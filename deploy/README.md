# Ubuntu 小范围演示部署

该方案面向单台 Ubuntu 22.04/24.04 服务器和少量用户：Nginx 对外提供访问，FastAPI 在 Docker 中以 Python 3.11、单 worker 运行，SQLite 保存在宿主机 `data/`，模拟交易只使用 MockBroker。

安装脚本默认通过 DaoCloud 镜像代理下载 Python 基础镜像，以适配中国大陆云服务器。其他地区可在 `.env.production` 中把 `PYTHON_BASE_IMAGE` 改回 `python:3.11.9-slim-bookworm`。

## 1. 阿里云安全组

只添加以下入方向规则：

- TCP 22：来源设置为你自己的公网 IP，不要使用 `0.0.0.0/0`。
- TCP 80：来源 `0.0.0.0/0`。
- TCP 443：有域名并启用 HTTPS 时，来源 `0.0.0.0/0`。

不要开放 8003。Docker 只把 8003 绑定到服务器的 `127.0.0.1`，公网必须经过 Nginx。

## 2. 在本机生成部署包

在项目目录运行：

```powershell
git archive --format=zip --output agent-platform.zip HEAD
```

该命令只打包 Git 中的交付文件，不包含 `.env`、本机虚拟环境、用户数据库和 API Key。

## 3. 上传并解压

使用阿里云“远程连接”的文件上传功能，把 `agent-platform.zip` 上传到服务器 `/tmp/`，然后在服务器终端执行：

```bash
sudo apt-get update
sudo apt-get install -y unzip
sudo mkdir -p /tmp/agent-platform-source
sudo unzip -q /tmp/agent-platform.zip -d /tmp/agent-platform-source
cd /tmp/agent-platform-source
```

## 4. 先用公网 IP 部署

把下面的 `你的公网IP` 替换成阿里云实例的公网 IP：

```bash
sudo bash deploy/install_demo.sh \
  --server-name 你的公网IP \
  --public-origin http://你的公网IP
```

安装结束后访问 `http://你的公网IP/`。首次注册的账号会成为管理员。注册管理员后，在管理页面关闭公开注册。

## 5. 使用域名和 HTTPS

先把域名 A 记录解析到服务器公网 IP，等待解析生效，然后执行：

```bash
sudo bash deploy/install_demo.sh \
  --server-name agent.example.com \
  --public-origin https://agent.example.com \
  --letsencrypt-email 你的邮箱
```

脚本使用 Certbot 申请 Let's Encrypt 证书并启用 HTTP 到 HTTPS 跳转。公网 IP 本身无法申请浏览器信任的 Let's Encrypt 证书。

## 6. 常用运维命令

```bash
cd /opt/agent-platform
sudo docker compose ps
sudo docker compose logs -f --tail=200 app
sudo docker compose restart app
curl -fsS http://127.0.0.1:8003/health
curl -fsS http://127.0.0.1:8003/ready
sudo agent-platform-backup
```

更新代码后，重新上传并执行安装脚本。现有 `.env.production` 和 `data/` 不会被覆盖。

## 7. DeepSeek

默认 `LLM_PROVIDER=mock`，不会产生模型费用。需要真实 DeepSeek 时，在服务器编辑：

```bash
sudo nano /opt/agent-platform/.env.production
```

设置 `LLM_PROVIDER=deepseek` 和新的 `DEEPSEEK_API_KEY`，然后执行：

```bash
cd /opt/agent-platform
sudo docker compose up -d --force-recreate app
```

不要把 Key 写入 Git、截图或聊天记录。建议在 DeepSeek 控制台设置余额告警和调用限额。

## 8. 数据与备份

- SQLite 数据：`/opt/agent-platform/data/`
- 每日备份：`/var/backups/agent-platform/`
- 默认每天 03:17 备份，保留 14 天。
- 删除服务器或释放云盘前，必须下载最近备份。
