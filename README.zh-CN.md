# Model Processing Engine

[English](README.md) | 简体中文

> [!IMPORTANT]
> **源码可见，但不是开源软件。** Copyright (c) 2026 piercezzs。你可以下载
> 未经修改的副本，并在本地用于个人用途或组织内部用途。未经授权，不得修改、
> 再分发、重新发布、向第三方提供托管服务或发布衍生版本。完整且具约束力的
> 条款以英文 [LICENSE](LICENSE) 为准。
>
> **版本状态：** `0.1.0` 是实验性、local-first 版本。在稳定版本发布前，
> 公开契约和运行行为都可能发生变化。目前尚未认证其适合远程生产环境。

Model Processing Engine（MPE）是一个用于执行结构化模型任务的业务中立运行时。
调用项目负责提示词、Schema、分类体系、缓存语义和业务数据持久化；MPE 负责
Provider 调用、重试、校验、并发、进度、用量统计、执行记录和可丢弃的结果缓存。

MPE 可以作为 Python 库、CLI 或带版本号的本地 HTTP 服务使用。

## 边界

```text
调用项目
  -> 项目自有适配器和 Task Pack
  -> MPE 执行契约
  -> 已配置的模型 Provider
  -> 经过校验的 ResultEnvelope
  -> 项目自有审核与持久化
```

MPE 不包含调用项目的 Job、媒体类型、报告格式、回写规则或领域提示词。图书解析器、
金融分类器或未来的媒体任务，都由调用项目定义，并通过相同契约提交。

## 环境要求与安装

- Python 3.10 或更高版本
- 使用真实模型时，需要通过本地环境变量提供 Provider 凭证

开发环境安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

如需一键完成本地配置并启动托管服务，请使用对应平台的启动脚本：

```bash
# macOS：在终端运行，或通过 Finder 双击
chmod +x start_mpe.command stop_mpe.command
./start_mpe.command

# Windows：在命令提示符运行，或通过资源管理器双击
start_mpe.bat
```

启动脚本会查找 Python 3.10 或更高版本，创建或修复 `.venv`，根据
`pyproject.toml` 安装运行时依赖，并记录依赖指纹；环境未变化时不会重复安装。
服务身份识别和端口冲突处理由 MPE 已验证的进程管理器负责。脚本不会仅仅因为
端口 `8787` 被占用就终止相关进程。

完成配置后，可以通过 `./start_mpe.command --check-only`（macOS）或
`start_mpe.bat --check-only`（Windows）执行不会启动服务的就绪检查。使用
`./stop_mpe.command` 或 `stop_mpe.bat` 停止经过身份验证的托管服务。

正常的一键启动会在健康检查通过后打开 `/admin` 回环管理页面。使用
`--no-open` 可以启动服务但不打开浏览器。远程绑定时，管理页面会被禁用。

## 本地模型管理

管理页面是配置 Provider、模型和 API Key 的常规本地入口：

```text
http://127.0.0.1:8787/admin
```

页面使用顶层标签页区分 Provider 配置和脱敏后的执行历史。执行历史支持按照浏览器
所在 IANA 时区查看日、月、年周期，并可按正式任务/连接测试、Provider 和模型进行
筛选。摘要和逐模型统计会分别展示执行次数、真实 Provider 调用次数、Token 用量、
传输重试、契约修复、MPE 结果缓存命中和 Provider 提示词缓存用量。这些指标含义
不同：MPE 结果缓存命中会跳过 Provider 调用；Provider 提示词缓存命中仍会生成
新结果，只是复用了输入前缀 Token。历史记录不会包含模型结果正文。

Provider 连接测试也会写入同一份技术账本；当 Provider 返回用量数据时，相关用量
也会被记录。每次 Provider 调用都会在输出 Schema 校验前写入记录，因此即使返回的
JSON 被任务契约拒绝，已经消耗的 Token 仍会计入统计。

你可以选择 OpenAI、DeepSeek 等预设服务，也可以选择自定义
OpenAI-compatible Provider。预设会自动填写协议 URL 和端点路径，但仍可编辑。
填写 API Key 后，点击 **检测并获取模型** 会请求 Provider 配置的 `/models`
端点，并填充默认模型选择器。如果 Provider 不提供兼容的模型列表端点，仍可手动
输入模型 ID。

一个 Provider 配置可以保留多个已发现的模型 ID，并指定一个默认执行模型。调用
项目仍可在单次执行请求中覆盖该模型。Chat Completions 协议、请求/列表路径、
超时和传输重试次数位于高级连接设置中；同一区域也负责设置 Provider 的最大并发
请求数。实际并发由需求驱动：只有一个待处理执行时只占用一个槽位；负载增大时，
超过 Provider 并发上限的请求会排队。

高级设置中的 **Provider 原生 JSON Schema** 是显式能力声明，不会自动检测。
只有当所选平台和模型支持 Chat Completions
`response_format.type=json_schema` 时才应启用。无论是否启用，MPE 都会在
本地校验每个返回对象。

MPE 会先在内存中测试准确的 Provider 配置、模型列表、默认模型和 Key。只有与
当前内容完全匹配且未过期的成功测试，才能保存并激活。获取模型列表或连接测试
失败都不会修改项目文件。

成功激活后，会在当前项目检出目录中写入由用户管理的状态：

- `.env`：保存当前 Provider/模型、本地 Provider 配置文件的绝对路径和 API Key。
  该文件是明文文件，在 macOS 上权限为 `0600`，并被 Git 忽略。
- `config/providers.local.json`：保存不含秘密的 Provider 元数据，同样被 Git
  忽略。

两个文件都通过原子替换写入。已有 Key 永远不会通过 API 或管理页面返回；UI 只会
显示是否已配置凭证。保存完成后，MPE 会安排一次经过身份验证的托管重启，页面会
等待新服务恢复健康。连接测试失败不会修改这两个文件。

通过平台启动脚本启动 MPE 时，项目 `.env` 会被自动载入。当前进程已有的环境变量
优先级更高，因此无界面和 CI 部署仍可覆盖配置。不要提交、复制或分享项目 `.env`
文件。

内置默认 Provider 是确定性的 `mock`，因此无需网络和凭证也能运行仓库。真实调用
前，请将 `config/providers.json` 复制到你的部署配置中，并设置
`MPE_PROVIDER_CONFIG`。

`MPE_HOME` 默认是 `~/.model-processing-engine`。除非分别覆盖路径，托管服务的
数据、日志和进程记录都会存放在该稳定目录下。显式指定 `--root` 可以选择独立的
运行时配置。

## Task Pack

Task Pack 由调用项目拥有并维护版本：

```text
tasks/example/
  task.json
  prompt.md
  input.schema.json
  output.schema.json
  taxonomy.json       # 可选
```

`task.json` 只包含声明式执行元数据：

```json
{
  "schemaVersion": 1,
  "namespace": "my-project",
  "id": "structured_summary",
  "version": "1",
  "title": "Structured Summary",
  "promptPath": "prompt.md",
  "inputSchemaPath": "input.schema.json",
  "outputSchemaPath": "output.schema.json",
  "cachePolicy": {
    "mode": "exact",
    "semanticVersion": "summary-v1",
    "ttlSeconds": 86400,
    "sensitive": false
  },
  "runtimeDefaults": {
    "providerId": "openai-compatible",
    "model": "your-model-id",
    "temperature": 0.1,
    "contractRetries": 1
  }
}
```

支持以下缓存模式：

- `disabled`：不存储可复用结果；`sensitive` 为 `true` 时必须使用该模式。
- `exact`：完整且经过校验的输入参与缓存键计算。
- `semantic`：只有调用方声明的 `identityFields` 参与输入身份计算；执行时每个
  声明字段都必须存在。

所有模式的缓存身份还会包含 namespace、完整 Task Pack 摘要、Provider 身份、
模型、推理参数和语义版本。缓存输出只是可丢弃的计算复用；权威业务数据仍由调用方
负责。

`runtime.forceRefresh` 会绕过缓存读取。Provider 调用和输出校验成功后，引擎会
替换匹配的可复用缓存项；刷新失败不会破坏之前的有效缓存。

`runtimeDefaults.contractRetries` 控制有限次数的输出契约修复请求，默认值为 `1`；
单次执行可以通过 `runtime.contractRetries` 将其覆盖为 `0` 到 `2`。传输重试和
契约修复是两个独立计数。修复请求会复用原始任务/输入前缀，附加被拒绝的对象和
长度受限的校验诊断，并作为一次新的真实 Provider 调用记录。

敏感任务必须同步执行。其结果会返回给当前调用方，但不会写入持久化执行记录或
结果缓存。

Task 文件不能逃逸其目录。输入和输出 Schema 可以使用 `#/$defs/Item` 等本地
JSON Schema 片段；外部 `$ref` 会被拒绝，避免校验过程获取不受信任的远程资源。

## CLI

校验内置的中立示例：

```bash
.venv/bin/mpe task validate \
  --task-dir examples/tasks/generic_summary
```

使用离线 mock Provider 执行示例：

```bash
.venv/bin/mpe execute \
  --root . \
  --task-dir examples/tasks/generic_summary \
  --input examples/tasks/generic_summary/input.example.json
```

清理过期缓存项：

```bash
.venv/bin/mpe cache cleanup --root .
```

## Python SDK

```python
import json
from pathlib import Path

from model_processing_engine import ExecutionRequest, build_default_engine, load_task_pack

root = Path("/path/to/calling-project")
task = load_task_pack(root / "tasks" / "structured_summary")
payload = json.loads((root / "input.json").read_text(encoding="utf-8"))

request = ExecutionRequest.model_validate(
    {
        "task": task.model_dump(by_alias=True),
        "input": payload,
        "runtime": {"forceRefresh": False},
    }
)
result = build_default_engine(root=root).execute(request)
```

调用方只有在确认 `result.status == "succeeded"` 并应用自己的审核规则后，才应
持久化 `result.result`。

## HTTP 服务

使用稳定的默认运行时根目录启动托管后台服务：

```bash
.venv/bin/mpe start
.venv/bin/mpe status
.venv/bin/mpe restart
.venv/bin/mpe stop
```

需要隔离时，可以显式指定运行时配置：

```bash
.venv/bin/mpe start --root /path/to/runtime-profile
```

前台开发和诊断：

```bash
.venv/bin/mpe serve --root .
```

托管启动具备幂等性。它会写入权限受限的服务记录和日志，等待经过身份验证的健康
响应，并拒绝存在冲突的监听器。停止服务前会验证应用 ID、运行时实例 ID 和进程
ID；无法验证的活动 PID 永远不会被终止。

`status` 可能返回 `running`、`stopped`、`stale`、`unresponsive`、
`conflict` 或 `running_unmanaged`。前台 `serve` 进程不会被托管，必须由启动
它的终端负责停止。

可用路由：

- `GET /v1/health`
- `GET /v1/providers`
- `POST /v1/executions`
- `GET /v1/executions/{execution_id}`
- `POST /v1/cache/cleanup`
- `GET /admin`（回环管理页面）
- `GET /v1/admin/config`（脱敏后的本地配置）
- `GET /v1/admin/executions`（仅回环可用的脱敏执行历史）
- `GET /v1/admin/execution-stats`（限时段聚合和逐模型统计）
- `POST /v1/admin/providers/test`（受同源和 CSRF 保护）
- `POST /v1/admin/providers/models`（在内存中获取 Provider 模型列表）
- `POST /v1/admin/providers/apply`（受同源和 CSRF 保护）

HTTP 请求包含解析后的 `TaskDefinition` 和输入数据，不包含服务器端 Task 目录
路径，从而保持文件系统归调用项目所有。将 `asyncMode` 设置为 `true` 后，会先
收到 queued envelope，再轮询执行路由。

默认主机是 `127.0.0.1`。非回环绑定必须同时设置 `MPE_ALLOW_REMOTE=1` 和
`MPE_API_TOKEN`；经过认证的路由随后要求
`Authorization: Bearer <token>`。只有回环部署的健康检查保持公开。如果服务跨越
可信计算机边界，请使用提供 TLS 的反向代理。

每个 `MPE_DATA_DIR` 只能由一个服务进程持有。异步请求会在 API 返回前写入同一个
SQLite 数据库。固定 Worker 池按照 FIFO 顺序领取任务；服务重启后，会重新排队
之前正在运行的任务，并继续处理已排队请求。没有持久请求载荷的旧记录或不完整记录
会被标记为已中断，不会根据残留信息猜测恢复。`MPE_ASYNC_WORKERS` 默认值为 `4`，
`MPE_ASYNC_QUEUE_CAPACITY` 默认值为 `100`；队列已满时返回 HTTP 429。队列容量
同时计算 queued 和 running 请求。

敏感任务保持同步执行，因为可恢复队列在任务到达终态前必须持久化完整请求。任务
完成后会删除对应队列行。对于终态快照提交前被中断的请求，恢复语义为
at-least-once；已经存在终态快照的任务不会再次执行。多个调用方可以共享同一服务，
但不同服务进程应使用不同的数据路径。

健康检查会返回应用版本、API 版本、稳定的运行时实例 ID 和进程 ID，供本地服务
管理器验证进程身份。

## Provider 配置

`config/providers.json` 展示了支持的 Provider 类型：

- `mock`：根据 Schema 生成确定性结果，用于测试和集成配置。
- `openai_compatible`：使用 JSON Chat Completions 传输，并对限流、服务器错误、
  超时和连接失败执行有限次数的重试。

Provider 配置可以声明 `availableModels`、`capabilities` 和 `maxConcurrency`。
`/v1/providers` 响应会公开这些非秘密声明，同时保留原有 Provider ID 列表以保持
兼容性。`structured_json` 使用 JSON-object 模式；`native_json_schema` 会通过
Provider 的严格 JSON Schema 请求字段发送任务输出 Schema，只应为已知兼容的
平台/模型组合启用。`maxConcurrency` 会针对每个 Provider 独立执行，并覆盖所有
调用方。环境级 `MPE_MAX_PROVIDER_CONCURRENCY` 仍是整个服务的安全上限；最终生效
值是两者中较小的一个。

Provider 配置通过环境变量名引用凭证。不得将凭证写入 Task Pack、请求载荷或已
提交文件。为每个账号/端点上下文设置一个非秘密 `cacheIdentity`；上下文改变时应
递增该值，因为它会参与技术缓存身份计算。

## 验证

运行完整的确定性项目验证：

```bash
# macOS / Linux
.venv/bin/python scripts/verify_project.py

# Windows
.venv\Scripts\python scripts\verify_project.py
```

该脚本会运行完整单元测试、Python 编译、管理端 JavaScript 语法检查、
`git diff --check` 和中立示例 Task Pack 校验，不会重启或修改正在运行的服务。

只有在确定需要重启托管服务时，才添加 `--runtime`：

```bash
.venv/bin/python scripts/verify_project.py --runtime
```

Runtime 模式会使用经过验证的进程管理器重启 MPE，检查服务身份和状态，探测脱敏
历史记录契约，并确认当前管理端脚本以防缓存响应头提供。它还会通过持久异步队列
提交一个离线 mock 请求，并轮询到成功终态。基于浏览器的视觉验证仍需单独手动完成
或由代理协助完成。

自动化测试只使用 mock Provider 或模拟传输。真实 Provider 验收调用需要本地凭证，
并可能产生费用，因此有意与默认测试分离。

## 许可证

MPE 是专有的 source-available 软件，不是开源软件。允许将未经修改的副本用于
个人或组织内部的本地运行；未经 piercezzs 事先书面授权，不得修改、再分发、
重新发布、向第三方提供托管服务或发布衍生版本。完整且具约束力的条款以英文
[LICENSE](LICENSE) 为准。

所有权与设计决策见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
