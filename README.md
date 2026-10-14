# 接访洞察工作流

**输入已经转写的案场接访文本，使用 Kimi Code 或 DeepSeek 分析，输出可下载的 HTML 客户洞察看板。**

已按三份示例搭建：画像、5W2H、需求成熟度、关键对话、决策旅程、核心卡点、跟进建议、原文证据与质量说明。统一使用带看总结与客户洞察双 Tab 报告。

## 如何使用

**作为 API 部署：** 见 [API.md](API.md)。使用 `docker compose -f compose.api.yaml up -d --build`，支持 Bearer 密钥认证、JSON 文本提交、单任务模型参数、异步状态查询和 HTML/JSON 下载。交互接口文档位于 `/docs`。

**交给 AI Agent 调用：** 同一容器提供 `/mcp`（Streamable HTTP），另支持 stdio 转接。接入配置、9个工具和调用流程见 [MCP.md](MCP.md)。

1. 双击项目目录中的 **启动工作流.cmd**。
2. 浏览器打开 `http://127.0.0.1:8767`。
3. 点击“从本地文件夹选择”，选择“录音文本”目录或其项目子文件夹中的文件；也可以拖入文件或一次选择多份文件。
4. 可填写客户称呼、项目、顾问、实际接访日期。接访日期留空时，自动读取文件名末尾的日期，例如 `7625包女士_淼_20261005.txt` → `2026-10-05`，也支持 `2026-10-05`、`2026_10_05`。手动填写优先；末尾没有有效日期时标记未知，不使用文件创建时间或报告生成时间。
5. 点击“开始生成看板”。
6. 在处理记录中查看进度。完成后可打开报告、下载 HTML 或分析 JSON。

看板分为「带看总结」「客户洞察」两个页签，原文与质检信息共用。默认打开带看总结，支持方向键切换。打印时两个页签全部输出。看板可离线打开，点击时间戳或行号会展开并定位原文。

服务默认监听 `0.0.0.0:8767`，同一内网的同事可通过本机内网 IP 访问。文本解析和页面渲染在服务器完成；LLM 分析节点把接访内容发送至所选厂商的官方接口。

## 模型配置

网页顶部「模型设置」提供 **Kimi Code / DeepSeek** 厂商切换，分别保留模型参数与密钥。选择 DeepSeek 后填写 API Key，点击「测试当前参数」，成功后「保存配置」。测试未保存的配置不会切换当前工作流。切换后新建/继续的任务使用所选厂商，已排队和运行中的任务保持原配置。

DeepSeek 支持 `deepseek-flash`（V4.1 Flash）和 `deepseek-v4-pro`，官方地址 `https://api.deepseek.com`（也支持 `/v1`）；思考强度 low/high/max 或关闭。模型和参数根据 [DeepSeek 官方文档](https://api-docs.deepseek.com/) 与 [思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/) 接入，未提供自定义第三方转发地址。

网页输入的密钥只存在当前服务内存中，按厂商隔离、不回显、不写入报告和参数文件。需要重启后保留 DeepSeek 密钥，可在项目 `.env` 设置 `DEEPSEEK_API_KEY=你的密钥`；Docker 使用 `docker compose up -d` 重新创建服务后读取。也可在 `model.json` 的 `models` 数组中添加 `{ "provider": "deepseek", "model": "deepseek-flash", "apiKey": "你的密钥", "url": "https://api.deepseek.com" }`。Kimi 和 DeepSeek 的密钥不会相互替代。

已支持当前项目的 `model.json`。从数组或 `models` 列表中选择 `id/model` 为 `k3` 或 `kimi-k3` 的条目，读取 `apiKey`、`url` 和 `reasoning.defaultEffort`。

在网页顶部点击「模型设置」，可选 K3、K3 256K、K2.8 Preview，调节 low/high/max 思考强度、1–8 路并发、最大输出 token、请求超时及国内/海外官方接口。所有节点使用任务启动时所选模型；切换模型、思考强度或输出上限会使对应节点缓存失效，报告与用量记录显示实际请求的模型。

非密钥参数优先级：网页已保存参数 → 环境变量 → 项目 `.env` → `model.json`。网页参数保存在 `runs/.model-settings.json`，Docker 重建后仍保留，不修改只读的 `model.json`。网页保存对全工作台新建/继续的任务生效，已排队或运行的任务使用原配置。「测试当前参数」只测试草稿，不自动保存。模型是否可用取决于账号权限；接口拒绝时展示错误，不自动换模型。

页面临时输入的密钥仅保存在本次服务内存中，不写入参数文件，重启后恢复环境变量/本地配置中的密钥。非密钥参数可长期保存，各厂商密钥来源分别读取。

也可以复制 `.env.example` 为 `.env`，填写：

```dotenv
KIMI_CODE_API_KEY=你的KimiCode密钥
KIMI_CODE_BASE_URL=https://api.kimi.com/coding/v1
KIMI_CODE_REASONING_EFFORT=high
KIMI_CODE_TIMEOUT=600
```

不要把密钥写入提示词或报告。`model.json`、`.env`、原始接访文本和 `runs` 已加入版本控制忽略规则。系统仅读取显式支持的厂商密钥配置。

地址和模型 ID 依据 [Kimi Code 官方模型配置](https://www.kimi.com/code/docs/kimi-code/models.html)。请求使用真实客户端标识 `VisitInsightWorkflow/1.0`；若接口拒绝账号或客户端权限，显示原始错误原因，不伪装身份、不切换模型。

## 真实执行链路

```text
文件读取与证据编号
    ↓
多个文本分块并发提取事实（最多 3 路）
    ↓ 按原文顺序合并去重、校验
    ├─ 客户画像、5W2H、需求成熟度 ─┐
    ├─ 带看房源、预算、关键对话 ──┤ 同时分析
    └─ 决策阶段、准备度、核心卡点 ─┘
    ↓ 三路汇合并校验
摘要与跟进建议 → 独立质检 → 必要时修正并复检（最多两轮）
    ↓
双 Tab HTML 看板 + 分析数据 + Token 用量
```

`KIMI_CODE_CONCURRENCY` 控制单份任务内的模型并发数，默认 3，范围 1–8。本地运行写入 `.env`；Docker Compose 会读取同名变量，修改后运行 `docker compose up -d`。并发数为 1 时使用相同的新节点结构逐个运行。批量文件仍按队列逐份处理，避免把多份任务的并发数相乘。

跟进建议依赖三路汇合结果；质检依赖完整草稿；修正依赖质检问题，因此保留这些必要的先后关系。某分支失败时停止待执行任务，取消尚在运行的同级调用并保留已完成节点；继续运行时复用有效缓存。并发缩短独立节点的等待时间，但三路分析会重复读取共同上下文，不保证节省 token。

## Token 用量

处理记录显示累计用量，点击「Token 用量」查看输入、输出、缓存输入、推理 token、各节点统计和逐次请求；运行期间刷新统计，独立报告页保存生成完成时的用量快照。

- `runs/<任务ID>/usage.json`：逐次调用账本，在请求开始与结束时立即写盘，记录节点、结构修正轮次、网络重试次数、状态、起止时间、耗时和接口原始 usage。
- `usage-summary.json`：总量与节点汇总，包含重试、质检与修正调用。续跑累计，不覆盖原账本。
- 缓存输入是输入的一部分，推理 token 是输出的一部分，总量不重复相加。
- 复用本地节点标记为缓存复用，不发模型请求，也不增加 token。
- 没有接口 usage 的中断或失败请求标记用量未知；显示「已记录」值，不臆估总消耗。
- 历史账本可直接汇总，但旧版本遗漏的失败请求无法补算；页面注明历史记录限制。

统计为接口返回的 token 数，不换算会员额度比例或费用；连接测试返回自身 usage，但不计入任一接访任务。不同节点耗时之和含并行重叠，不等于任务实际用时。

各节点的实际提示词位于 `visit_workflow/prompts/`，结构化数据契约位于 `visit_workflow/models.py`，已导出的 JSON Schema 位于 `schemas/`。

每个节点在 `runs/<任务ID>/nodes/` 留存输出、校验后的结果和用量。缓存签名包含模型、地址、思考程度、提示词、Schema 和节点输入；内容变化时缓存自动失效。

失败或停止后可点击“继续运行”。已通过校验且输入未变化的节点会被复用，未完成节点重新调用。已生成的完整报告不会通过“继续运行”覆盖；要重新分析，建立新任务。

## 输入格式

支持 `.txt`、`.md`、`.srt`、`.vtt`、`.json`、`.docx`，不做录音转写，不把报告 PDF 当接访原文。

单文件最大 2 MB，正文最大 18 万字符。超过限制会明确报错，不截断后继续分析。

推荐格式一：

```text
客户 00:00:01
我们想换四房。
销售 00:00:06
预算大概多少？
客户 00:00:10
希望三百万以内。
```

推荐格式二：

```text
[00:01] 客户：我们想换四房。
[00:06] 顾问：预算大概多少？
```

JSON 支持顶层数组或 `segments` / `utterances` 数组，片段字段如下：

```json
{"segments":[{"speaker":"客户","text":"我们想换四房。","start_sec":1,"end_sec":5}]}
```

`start` / `end` 也支持，以秒为单位，或使用 `MM:SS` / `HH:MM:SS` 字符串。没有时间信息时不要填 0，直接省略。JSON 的“原文行号”表示片段序号；DOCX 表示提取后的段落行号。

没有说话人或时间戳的纯文本也可以导入，但会提示角色不明，使用原文行号作为证据位置。文本中的 `1:1` 等比例表达不会当作说话人名称。

## 输出与质量边界

| 文件 | 内容 |
|---|---|
| `report.html` | 可独立打开的最终看板 |
| `report.json` | 最终结构化报告 |
| `transcript.json` | 文本、角色标签、原有时间和行号 |
| `evidence.json` | `E00001` 等证据 ID 与原文对应 |
| `facts.json` | 提取事实、旁路、未知和冲突 |
| `quality.json` | 审查结论、问题和处理说明 |
| `manifest.json` | 模型、版本、输入摘要和处理时间 |
| `usage.json` | 各实际调用返回的 token 用量 |

“明确”表示原文中有人明确陈述，不等于外部事实已核实。销售报价、贷款、学校、库存等没有接入实时外部资料核验。

严重问题两轮修正后仍未解决，报告显示 **待复核草稿**。疑似多个客户混在同一文本中，也只生成待复核草稿，建议拆开重新运行。

程序校验保证引用存在、Schema 正确和部分算术关系可核对；模型审查不能保证所有语义推断绝对正确。业务使用前应重点复核客户角色、金额与核心卡点。

报告来自转写文件，不会声称已校验原音频、声纹或音频噪声。页面不支持录音播放；时间点用于原文定位。

## 命令行方式

在项目目录运行：

```powershell
.venv/Scripts/python.exe -m visit_workflow check
.venv/Scripts/python.exe -m visit_workflow serve
.venv/Scripts/python.exe -m visit_workflow run "录音文本/某客户.txt" --client "客户称呼"
.venv/Scripts/python.exe -m visit_workflow resume 任务ID
```

其他可选参数：`--project`、`--advisor`、`--date YYYY-MM-DD`。

## 安装与验证

当前电脑已安装运行环境。换电脑时需要 Python 3.11 或更新版本：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock.txt
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe -m visit_workflow.demo
```

`output/demo/` 是人工构造的演示看板，有显著演示标记。它只用于离线排版与测试，不会在真实 API 失败时冒充生成结果。

若服务已经运行，直接访问本地地址即可。关闭启动窗口会停止服务；停止后可以重新启动并继续未完成任务。请避免同时使用两个服务或命令行进程处理同一个任务目录。
