# Docker 内网部署

同事通过 `http://服务器内网IP:8767` 访问；服务器及 Docker 运行时可用。可迁移的 API 部署、访问认证和调用方式请见 [API.md](API.md)，使用独立的 `compose.api.yaml`。

在项目目录运行，或双击「启动Docker.cmd」：

```powershell
docker compose up -d --build
docker compose ps
```

容器和宿主机端口均为 8767，发布到 `0.0.0.0`，即监听所有网卡。同事应访问实际内网 IP，不能把 `0.0.0.0` 当访问地址。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)。

部署复用 `runs/` 中现有报告，不需要再次调用模型。`model.json` 和 `录音文本/` 只读挂载，`runs/` 可写挂载；这三项均不打进镜像。迁移时，将项目、模型配置与上述目录放到目标电脑再启动。容器仅使用单个工作进程，避免多个进程同时写任务。

```powershell
docker compose logs --tail=100 app
docker compose restart app
docker compose down
```

停止或重建容器不会删除本机报告。修改模型配置后重启容器生效。不要同时启动本地 Python 服务占用 8767；若本地服务正在运行，先关闭其启动窗口。Docker 已运行时，原「启动工作流.cmd」会打开现有服务。

当前为可信内网协作模式：能够访问工作台的同事可以查看已有接访报告、上传文本和生成报告，共用该模型配置。模型密钥不通过网页返回。此版本未设置用户登录和按人员隔离报告。

健康检查：`/healthz`。若其他电脑打不开，先确认处于可互通的内网，再检查 Windows 对 Docker Desktop 的入站规则和公司网络隔离策略。

仅本机访问可用 Python 启动参数 `serve --host 127.0.0.1`；Docker 可将端口绑定改成 `127.0.0.1:8767:8767`。

模型设置支持 Kimi Code 与 DeepSeek。DeepSeek 临时密钥可直接在网页填写；要在重建后保留，在项目 `.env` 配置 `DEEPSEEK_API_KEY` 后执行 `docker compose up -d`，或在只读挂载的 `model.json` 添加 DeepSeek 条目。网页非密钥参数及厂商分别保存的配置保存在 `runs/.model-settings.json`。
