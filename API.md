# 接访洞察 API

输入已转写文本，异步生成「带看总结 / 客户洞察」双 Tab HTML 和结构化 JSON。支持 Kimi Code、DeepSeek、单份任务内最多 8 路并发和逐调用 token 记录。

AI Agent 可直接通过同一服务的 `/mcp` 调用，详见 [MCP.md](MCP.md)。

## Docker 部署

```sh
cp .env.example .env
# 编辑 .env：填写 VISIT_API_TOKEN、DEEPSEEK_API_KEY。
docker compose -f compose.api.yaml up -d --build
```

Windows PowerShell 使用 `Copy-Item .env.example .env`。`VISIT_API_TOKEN` 是你自行设置的随机服务访问密钥，与厂商 API Key 不同。可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。不要使用示例占位值。

默认模型是 `deepseek-v4-pro`，可通过 `DEEPSEEK_MODEL` 切换；使用 Kimi 时设置 `LLM_PROVIDER=kimi` 和 `KIMI_CODE_API_KEY`。将 `.env` 中的 `KIMI_CODE_CONCURRENCY` 改为 `8` 可启用 8 路节点并发。在已有工作台占用 8767 时设置 `VISIT_PORT=8768`。

- 访问地址：`http://服务器IP:8767`，监听 `0.0.0.0`。
- 交互文档：`/docs`，点击 Authorize 填写服务访问密钥。
- 健康检查：`GET /healthz`，无需密钥。
- 请求认证：`Authorization: Bearer <VISIT_API_TOKEN>`。
- 浏览器打开工作台/报告会提示登录：用户名任意（例如 `api`），密码为服务访问密钥。
- 任务和报告保存在 Docker 命名卷 `visit-insight-api_api-runs`；容器重建保留数据，勿使用 `down -v`，除非确实要删除数据。
- 服务访问密钥保护新旧接口、配置、报告及原文；这是单租户服务，持有同一密钥的调用者可访问全部任务和管理配置。公网部署应在前面配置 HTTPS。
- 不依赖 `model.json` 或本机录音文件夹，不把密钥及客户资料打进镜像。

默认 `compose.yaml` 仍用于原有本机工作台。新部署请显式选择 `compose.api.yaml`。

## 提交 JSON 文本

`POST /api/v1/jobs`，成功返回 **202 Accepted**，随后轮询任务。模型调用会产生所选厂商费用。

```json
{
  "filename": "客户_20260830.txt",
  "text": "[00:01] 客户：想看四房，预算还没确定。\n[00:05] 销售：我们先看一下户型。",
  "meta": {
    "client_alias": "示例客户",
    "project": "示例案场",
    "advisor": "",
    "visit_date": ""
  },
  "options": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "effort": "high",
    "max_concurrency": 8,
    "max_tokens": 32000,
    "timeout": 600
  }
}
```

`meta`、`options` 可省略。日期留空时从文件名末尾解析；未出现则保留未知。文本按 UTF-8 不超过 2 MB。`options` 只作用于这份任务，不修改网页全局配置；凭据由服务端按厂商选择，不能通过该接口提交厂商密钥或第三方接口地址。任务入队时固定参数。

```sh
curl -X POST http://localhost:8767/api/v1/jobs \
  -H "Authorization: Bearer $VISIT_API_TOKEN" \
  -H "Content-Type: application/json" --data-binary @request.json
```

响应包含 `id`、`status`、`progress`、`message`、`usage` 和相对地址 `links.status / links.usage / links.report_html / links.report_json`。报告未生成时下载地址返回 404。

## 查询和下载

| 方法 | 地址 | 作用 |
|---|---|---|
| GET | `/api/v1/jobs/{id}` | 状态、进度、用量和结果链接 |
| GET | `/api/v1/jobs/{id}/usage` | 总用量及逐调用明细 |
| GET | `/reports/{id}/report.html` | 完整 HTML，可离线查看 |
| GET | `/reports/{id}/report.json` | 分析结果、原文依据、质检和用量 |
| POST | `/api/v1/jobs/{id}/stop` | 请求停止，保留已完成节点 |
| POST | `/api/v1/jobs/{id}/resume` | 继续未完成任务，使用当前全局模型配置 |
| POST | `/api/v1/jobs/upload` | multipart 文件上传，使用当前全局模型配置 |

上传字段为 `file`，可选 `client_alias / project / advisor / visit_date`。文件类型支持 txt、md、srt、vtt、json、docx，具体文本结构见 README。

终态：`complete` 成功；`needs_review` 已有报告但需复核；`failed` 失败；`cancelled` 已停止；`interrupted` 服务中断。`ready / queued / running / stopping` 为过程状态。建议每 3–5 秒轮询，单次 HTTP 请求超时与整个任务等待超时分别设置。

服务重启会把未完成任务标为 `interrupted`，不会自动重新付费运行；调用 resume 继续。完成或待复核任务不能 resume，需要重新分析时创建新任务。提交接口目前无幂等键：提交超时后先查询旧接口 `GET /api/jobs` 确认是否已创建，避免盲目重试造成重复费用。

错误：401 访问密钥不正确；403 跨站/请求来源拒绝；400 参数或厂商配置错误；422 请求结构错误；404 任务不存在或报告尚未生成。没有设置服务访问密钥的旧版内网模式，写请求仍需 `X-Visit-Workflow: 1`；API Docker 配置强制设置密钥。

## Python 调用

安装 `httpx`，设置 `VISIT_API_TOKEN` 后运行：

```sh
python examples/api_client.py 接访文本.txt --url http://localhost:8767 --model deepseek-v4-pro --output result.html
```

示例会提交、轮询并下载 HTML；不会打印厂商密钥或转写内容。`needs_review` 会明确提示。

## 部署限制

使用单个 API 容器、单个服务进程，整份报告按队列处理，任务内部并发上限 8；不要用多个进程或副本共享同一个数据卷。当前未实现跨实例任务队列、按租户隔离、自动高低价值路由或定时低谷执行。高价值路由仍是待验证方案。

测试：`python -m unittest discover -s tests -v`。测试使用模拟模型返回，不消耗真实模型额度。
