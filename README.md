# 简易 TCP 连接探测服务

HTTP 接口接收**批量 TCP 连通性探测任务**，后台使用 **asyncio + 标准库 socket 自研探测引擎**（未使用任何第三方探测组件）执行探测，按**来源 IP 的令牌桶限流**，结果按提交顺序返回。

> 北京悦游信息技术有限公司 招聘作业 —— 完整设计说明见 [docs/DESIGN.md](docs/DESIGN.md)，实现细节问答见 [docs/INTERVIEW_QA.md](docs/INTERVIEW_QA.md)。

## 需求对照

| 题目要求 | 实现方式 |
| --- | --- |
| 开发一个 HTTP 接口服务，对外提供接口 | FastAPI 应用：`POST /api/v1/tasks` 提交任务，`GET /api/v1/tasks/{id}` 查询结果，`DELETE` 取消；交互式文档 `/docs` |
| 接收探测任务，后台对指定目标做连通性探测 | 提交后立即返回（202），后台协程执行；`wait=true` 可长轮询直接拿到结果 |
| 返回探测结果 | 每个目标返回：状态、`resolved_ip`、握手耗时 `latency_ms`、失败原因 |
| 支持批量探测 | 单任务最多 200 个目标（可配置），全局同时在途连接数上限 100（可配置） |
| 简单的限流 | 按来源 IP 的令牌桶：默认 5 rps / 突发 10，超限返回 429 + `Retry-After` |
| 不要使用第三方封装好的探测组件 | 探测引擎完全自研：`asyncio.open_connection` 不做，直接 `loop.sock_connect` + 标准库 socket，见 [app/prober.py](app/prober.py) |
| 交付 Git 仓库 + README，拉下来能启动和验证 | 本仓库：`docker compose up` 或本地 venv 两种方式；`scripts/smoke_test.py` 一键端到端验证 |

## 快速开始

### 方式一：Docker（推荐，免装依赖）

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/healthz
```

> Dockerfile 默认使用清华 PyPI 镜像构建（国内网络更快，容器直连 pypi.org 常超时）；
> 海外/内网环境可覆盖：`docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple -t tcp-probe-service:latest .`

### 方式二：本地 Python（3.10+）

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Windows Git Bash:   source .venv/Scripts/activate
# macOS / Linux:      source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

启动后：

- 服务信息：<http://127.0.0.1:8000/>
- 交互式 API 文档（Swagger UI）：<http://127.0.0.1:8000/docs>
- 存活探针：<http://127.0.0.1:8000/healthz>（不参与限流）

## 验证

服务启动后，另开终端执行端到端冒烟脚本（纯标准库，无需装依赖）：

```bash
python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

脚本自动验证：健康检查与 `X-Request-ID` 透传 → 批量探测（open / closed / dns_error 三种状态与顺序）→ 异步提交 + 轮询 → 限流（429 与 `Retry-After`）。全部通过退出码为 0。

> Docker 方式启动时注意：容器内的 `127.0.0.1` 是容器自身，而冒烟脚本的 open/closed 探测目标建在宿主机上。
> 这时需要把目标主机指向宿主机（compose 已配置 `extra_hosts`）：
>
> ```bash
> python scripts/smoke_test.py --target-host host.docker.internal
> # 或：make smoke SMOKE_ARGS="--target-host host.docker.internal"
> ```

手工验证示例：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/tasks?wait=true&wait_timeout_ms=8000' \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "demo-batch",
        "targets": [
          {"host": "example.com", "port": 443},
          {"host": "127.0.0.1", "port": 22},
          {"host": "no-such-host.invalid", "port": 80}
        ]
      }'
```

响应示例（`wait=true` 且任务在等待窗口内完成，返回 200）：

```json
{
  "id": "5f0c9d3e8a7b4c1d9e2f6a0b3c4d5e6f",
  "name": "demo-batch",
  "status": "succeeded",
  "total": 3,
  "completed": 3,
  "results": [
    {"host": "example.com", "port": 443, "status": "open",
     "resolved_ip": "93.184.216.34", "latency_ms": 23.4, "error": null},
    {"host": "127.0.0.1", "port": 22, "status": "closed",
     "resolved_ip": "127.0.0.1", "latency_ms": null, "error": "connection_refused: 127.0.0.1:22"},
    {"host": "no-such-host.invalid", "port": 80, "status": "dns_error",
     "resolved_ip": null, "latency_ms": null,
     "error": "dns_error: no-such-host.invalid: [Errno 11001] getaddrinfo failed"}
  ],
  "created_at": "2026-10-29T09:00:00.000000Z",
  "started_at": "2026-10-29T09:00:00.001000Z",
  "finished_at": "2026-10-29T09:00:00.180000Z",
  "error": null
}
```

限流效果（默认突发 10、每秒补充 5）：

```bash
for i in $(seq 1 30); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST 'http://127.0.0.1:8000/api/v1/tasks' \
    -H 'Content-Type: application/json' -d '{"targets":[{"host":"127.0.0.1","port":22}]}'
done
# 先出现若干 202（突发额度），随后出现 429 与 Retry-After
```

> 注意：每次 `curl` 进程启动约百毫秒，期间令牌桶按 5 rps 持续补充，所以实际要多发一些才会出现 429；
> 想要确定性地验证限流，直接跑 `scripts/smoke_test.py` 的限流步骤（同进程快速连发，突发耗尽即 429）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/tasks` | 创建探测任务（批量）。默认 202 立即返回；`wait=true&wait_timeout_ms=10000` 时最多等待指定毫秒，任务结束则返回 200 与完整结果 |
| GET | `/api/v1/tasks?limit=20` | 最近任务摘要列表（不含结果明细），按创建时间倒序 |
| GET | `/api/v1/tasks/{task_id}` | 任务详情与结果；结果与提交顺序一一对应，未完成的槽位为 `null` |
| DELETE | `/api/v1/tasks/{task_id}` | 取消未结束的任务（未完成目标以 `cancelled` 补齐，已出结果保留）；已结束返回 409 |
| GET | `/healthz` | 存活探针，不参与限流 |

**请求体**（`POST /api/v1/tasks`）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string(≤100) | 否 | 任务名 |
| `targets` | array(1..200) | 是 | 探测目标列表 |
| `targets[].host` | string(≤253) | 是 | 主机名或 IP 字面量（支持 `[::1]` 形式 IPv6） |
| `targets[].port` | int(1..65535) | 是 | TCP 端口 |
| `targets[].timeout_ms` | int(1..10000) | 否 | 单目标超时，缺省 3000；不得超过服务端上限 |

**统一错误信封**（所有 4xx/5xx；307 尾斜杠重定向为空 body）：

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "请求过于频繁，请在 1 秒后重试",
    "request_id": "b1e2c3d4...",
    "details": null
  }
}
```

常见错误码：`VALIDATION_FAILED`(422)、`TARGETS_TOO_MANY`(422)、`TIMEOUT_TOO_LARGE`(422)、`TASK_NOT_FOUND`(404)、`TASK_ALREADY_FINISHED`(409)、`TASK_CAPACITY_EXCEEDED`(503)、`RATE_LIMITED`(429)、`INTERNAL_ERROR`(500)。

每个响应都带 `X-Request-ID`（调用方传入合法值则透传，否则服务端生成），与响应体 `error.request_id`、服务端 JSON 日志中的 `request_id` 一一对应，便于排障。

## 探测结果状态语义

| 状态 | 含义 | 判断依据 |
| --- | --- | --- |
| `open` | 端口可连接 | TCP 三次握手成功；`latency_ms` 为握手耗时（不含 DNS） |
| `closed` | 主机可达、端口未监听 | 对端返回 RST（`ECONNREFUSED`）——这是**确定性**结论 |
| `timeout` | 无法判断 | 超时预算内没有任何地址给出明确答复（SYN 无响应） |
| `dns_error` | 域名解析失败 | NXDOMAIN 或 DNS 临时故障 |
| `unreachable` | 网络层不可达 | `ENETUNREACH` / `EHOSTUNREACH` / 连接被重置等网络层明确否定 |
| `invalid` | 入参非法 | 主机名/端口不合法，未发起任何连接（防御性兜底，正常请求在 422 处即被拦截） |
| `error` | 服务内部错误 | 探测中出现未预期异常 |
| `cancelled` | 任务被取消 | 取消后未完成的目标 |

> 关键区分：`closed` 说明包到了主机且被明确拒绝，而 `timeout` 什么也证明不了（可能被防火墙静默丢包）。`unreachable` 是网络层的明确否定，比 `timeout` 更强。

## 限流与背压

- **限流**：按来源 IP 的令牌桶，默认稳态 5 rps、突发 10（`RATE_LIMIT_RPS` / `RATE_LIMIT_BURST`）。只作用于 `/api/*`；超限返回 429 + `Retry-After`。空闲桶在 `RATE_LIMIT_IDLE_TTL_SECONDS`（默认 600s）后惰性回收，桶数另有 10 万硬上限（超出按字典顺序近似随机淘汰，O(n) 截断、无排序，保证内存有界）。开启限流时 rps 下限为 1/3600，保证 `Retry-After` 反映真实等待时间。
- **客户端 IP 取 TCP 对端地址**，不信任 `X-Forwarded-For`。部署在反向代理之后时，限流会退化为按代理 IP 计算，应改由代理层做真实来源限流（见 DESIGN.md「安全考量」）。
- **背压**：活动任务数达到 `TASK_MAX_ACTIVE`（默认 100）时直接 503 + `Retry-After`，而不是无限排队；单任务目标数受 `TASK_MAX_TARGETS`（默认 200）限制。
- **并发上限**：所有任务共享 `PROBE_MAX_CONCURRENCY`（默认 100）个在途连接许可，防止文件描述符耗尽。

## 配置项

全部通过环境变量覆盖（12-factor），默认值开箱即用；配置项与对应环境变量见 [.env.example](.env.example)。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | 监听地址（`python -m app.main` 时生效；uvicorn 命令行参数优先） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `PROBE_DEFAULT_TIMEOUT_MS` | `3000` | 默认单目标超时 |
| `PROBE_MAX_TIMEOUT_MS` | `10000` | 请求可指定的超时上限 |
| `PROBE_MAX_CONCURRENCY` | `100` | 全局同时在途连接数 |
| `TASK_MAX_TARGETS` | `200` | 单任务目标数上限 |
| `TASK_MAX_ACTIVE` | `100` | 活动任务数上限（背压） |
| `TASK_TTL_SECONDS` | `3600` | 终态任务内存保留时长 |
| `TASK_MAX_RETAINED` | `1000` | 内存中最多任务条数 |
| `RATE_LIMIT_ENABLED` | `true` | 是否开启限流 |
| `RATE_LIMIT_RPS` | `5` | 每客户端每秒令牌数（开启限流时不低于 1/3600） |
| `RATE_LIMIT_BURST` | `10` | 每客户端桶容量（突发上限） |
| `RATE_LIMIT_IDLE_TTL_SECONDS` | `600` | 空闲令牌桶回收时间 |

非法配置（如 `RATE_LIMIT_RPS=abc`、`PROBE_DEFAULT_TIMEOUT_MS > PROBE_MAX_TIMEOUT_MS`）在启动时立即报错退出。

## 设计要点

- **异步任务模型**：提交即返回，避免长探测阻塞 HTTP 线程；`wait=true` 只是长轮询糖，方便手工验证。
- **纯标准库探测**：`asyncio` 事件循环 + 非阻塞 socket，每个探测协程先解析（DNS 在 executor 中执行，计入总预算），再对最多 8 个地址按剩余预算依次发起 `sock_connect`；状态判定优先级 `open > closed > timeout > unreachable`。
- **队列与资源治理**：全局信号量限流在途连接；任务结果按提交顺序原地填充；取消通过 `CancelledError` 传播，未完成槽位补 `cancelled`；关停时取消全部在途任务。
- **内存治理**：任务 TTL 惰性清理 + 条数上限淘汰最旧终态任务 + 活动任务背压，三层限制保证内存有界。
- **可观测性**：JSON 结构化日志（访问日志、请求 id 贯穿）、`X-Request-ID` 响应头、`/healthz`。

完整设计取舍与边界条件见 [docs/DESIGN.md](docs/DESIGN.md)；面试问答见 [docs/INTERVIEW_QA.md](docs/INTERVIEW_QA.md)。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

覆盖：探测状态分类（真实本地监听器：open/closed/dns_error/invalid/timeout）、令牌桶数学与内存回收（可注入时钟）、任务编排（顺序、取消、背压、关停）、HTTP 层（状态码、错误信封、`X-Request-ID`、429 + `Retry-After`、异步轮询）。测试不依赖外网。

> 环境说明：`closed` 依赖系统对未监听端口回 RST。若机器上防火墙 stealth 模式或代理软件
> **延迟**送达 RST（实测某安全软件延迟约 2 秒），测试与冒烟脚本为 closed 目标预留了大于
> 延迟窗口的预算（集成测试默认 3000ms、冒烟脚本 5000ms），断言仍为严格的 `closed`；
> 若环境彻底静默丢包，返回 `timeout` 同样是诚实结果。

## 目录结构

```
tcp-probe-service/
├── app/
│   ├── api.py            # FastAPI 工厂、路由、ASGI 中间件、统一错误信封
│   ├── config.py         # 环境变量配置（12-factor，启动即校验）
│   ├── log.py            # JSON 结构化日志 + request_id 上下文
│   ├── main.py           # 入口（uvicorn app.main:app）
│   ├── models.py         # 领域模型与请求/响应 schema（Pydantic）
│   ├── prober.py         # TCP 探测引擎（纯标准库，自研）
│   ├── ratelimit.py      # 令牌桶限流（纯标准库，自研）
│   ├── service.py        # 任务编排：后台协程、并发上限、取消、背压
│   └── store.py          # 任务存储：TTL + 容量治理
├── tests/                # pytest 单元测试与 API 集成测试
├── scripts/smoke_test.py # 端到端冒烟脚本（纯标准库）
├── docs/
│   ├── DESIGN.md         # 设计说明与边界条件
│   └── INTERVIEW_QA.md   # 实现细节问答（面试准备）
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt / requirements-dev.txt
├── pyproject.toml
└── README.md
```

## 已知限制与后续改进

- **单实例、内存态**：任务不持久化，进程重启丢失；多实例部署需把任务存储换成 Redis/数据库（接口已隔离在 `store.py`，替换成本低）。
- **无鉴权**：探测服务天然可被滥用（SSRF / 端口扫描），当前按作业范围未做鉴权与目标白名单；生产化应加 API Key、目标允许清单与审计。
- **DNS 解析超时**：`getaddrinfo` 在 executor 中执行，等待超时后线程仍可能短暂占用（Java/Python 网络客户端的共性问题），已在设计文档说明取舍。
- **未实现 Happy Eyeballs（RFC 8305）**：多地址是顺序尝试、共享总预算；后续可按双栈并发优化 IPv6 环境下的首包时延。
- **可观测性**：可接入 Prometheus 指标（任务数、探测耗时直方图）与 OpenTelemetry 链路追踪。

## 开发规范

- 代码风格：`ruff`（配置见 `pyproject.toml`，行宽 100）。
- 提交前：`python -m pytest` 全绿。
