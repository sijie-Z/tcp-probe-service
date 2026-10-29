# 设计说明（DESIGN）

本文记录「简易 TCP 连接探测服务」的设计决策、边界条件与取舍。接口速查与启动方式见 [README](../README.md)。

## 1. 需求拆解

题目原文要点 → 设计回应：

1. **HTTP 接口服务**：选 FastAPI（团队最常见、自带 OpenAPI 文档），业务逻辑与框架解耦，`app/api.py` 只做协议转换。
2. **接收探测任务 + 后台探测 + 返回结果**：任务模型（创建 → 轮询结果），提交立即返回 202；另提供 `wait=true` 长轮询作为便利糖。
3. **批量探测**：一个任务携带 1..N 个目标，结果与提交顺序一一对应（`null` 表示尚未完成）。
4. **简单限流**：按来源 IP 的令牌桶（自研，不引第三方），429 + `Retry-After`。
5. **不用第三方探测组件**：探测引擎纯标准库实现（asyncio 非阻塞 socket），见 §4。
6. **工程习惯/完整性/文档/逻辑缜密**：配置、日志、错误信封、测试、Docker、冒烟脚本、本文档与问答手册。

### 明确不做（作业范围内的裁剪）

- 鉴权 / 目标白名单（README 已知限制中说明生产化方向）；
- 任务持久化与多实例共享（存储层已隔离，可替换）；
- WebSocket 推送（轮询足够，减少状态面）。

## 2. 总体架构

```
                   ┌─────────────────────────────────────────────┐
HTTP 客户端 ──────▶ │  ASGI 中间件链                               │
                   │  RequestContextMiddleware（X-Request-ID/日志）│
                   │  RateLimitMiddleware（按 IP 令牌桶）          │
                   └──────────────┬──────────────────────────────┘
                                  ▼
                   ┌──────────────────────────────┐
                   │  FastAPI 路由（api.py）        │
                   │  POST/GET/DELETE /api/v1/tasks │
                   └──────────────┬───────────────┘
                                  ▼
                   ┌──────────────────────────────┐
                   │  ProbeService（service.py）    │
                   │  提交/等待/取消/关停/背压       │
                   └───────┬──────────────┬───────┘
               每任务一个后台协程        全局信号量（在途连接上限）
                          ▼              ▼
                   ┌──────────────────────────────┐
                   │  TcpProber（prober.py）        │
                   │  asyncio + 标准库 socket       │
                   └──────────────────────────────┘
                          │
                   TaskStore（store.py）—— 内存任务表（TTL + 容量治理）
```

## 3. 任务与目标的状态机

任务（TaskStatus）：

```
PENDING ──▶ RUNNING ──▶ SUCCEEDED
                │
                ├────────▶ FAILED      （编排层未预期异常，防御性分支）
                └────────▶ CANCELLED   （DELETE 或服务关停）
```

- `SUCCEEDED` 表示**所有目标都产出了结果**（单个目标失败不算任务失败——`timeout` 也是有效结果）。
- 终态任务保留 `TASK_TTL_SECONDS`（默认 1h），到期后已读/写入时惰性清理。

目标（TargetStatus）判定矩阵（按优先级从高到低）：

| 优先级 | 状态 | 触发条件 |
| --- | --- | --- |
| 1 | `open` | 任一地址连接成功（握手完成） |
| 2 | `closed` | 任一地址返回 RST（ECONNREFUSED）：主机可达、端口明确拒绝 |
| 3 | `dns_error` | 解析失败，直接返回（不进入地址尝试） |
| 4 | `timeout` | 有地址尝试超时且无任何确定性结论 |
| 5 | `unreachable` | 全部尝试均为网络层不可达（ENETUNREACH/EHOSTUNREACH/重置等） |
| — | `invalid` | 入参非法，不发网络 I/O（API 层 422 之外的第二道防线） |
| — | `error` | 未预期异常兜底 |
| — | `cancelled` | 任务取消后补齐的未完成目标 |

**为什么 `closed` 优先于 `timeout`**：RST 是确定性证据（包到了主机、端口没监听）；同一个域名解析出多个地址时，只要任一地址明确拒绝，就说明"该端口在某条可达路径上未监听"，比其余地址的静默超时更可信。

### 预算模型

`timeout_ms` 是**单个目标的全部时间预算**：DNS 解析 + 全部地址尝试共享同一个 deadline（`time.monotonic()`）。每个地址的 `sock_connect` 超时 = 剩余预算；预算耗尽即收敛为已收集的最佳结论。这样"一个目标最多花 timeout_ms"的承诺对调用方是确定性的，不会因为多 A/AAAA 记录成倍放大。

## 4. 探测引擎

- **纯标准库**：`loop.getaddrinfo`（executor 线程池）、非阻塞 socket、`loop.sock_connect`。没有 ping/nmap/scapy 等第三方探测组件。
- **IPv4/IPv6**：`getaddrinfo` 默认双栈；IP 字面量（含 `[::1]` 写法）跳过 DNS。
- **地址尝试**：最多取前 8 条 addrinfo 顺序尝试，共享剩余预算。
- **延迟度量**：`latency_ms` 只统计成功连接那一次握手的 `perf_counter` 差值（不含 DNS、不含此前失败地址的耗时），语义清晰、可对比。
- **可测试性**：`_connect_once` 是模块级协程，测试 monkeypatch 它即可确定性构造"超时"场景；`classify_socket_error` 是纯函数，异常→状态映射可完全单测。

**DNS 超时的取舍**：Python `getaddrinfo` 无原生超时；`asyncio.wait_for` 取消的是等待者，executor 线程会在系统解析返回后才释放。极端 DNS 卡死时线程会超出预算存活（有界：一次探测最多一个解析线程，受信号量约束）。生产化可用异步 DNS 库或自建解析缓存；作业范围内记录为已知限制。

## 5. 并发模型

- **单事件循环**：所有任务状态写入都发生在同一线程，`Task` 模型无需加锁（这是选择"协程内原地更新"而非线程池的原因）。
- **每任务一个后台协程**；**每目标一个探测协程**，`asyncio.gather` 并发。
- **全局信号量** `Semaphore(PROBE_MAX_CONCURRENCY)`：真正的并发上限是"在途 TCP 连接数"，批量任务再多也不会打爆 fd。信号量是公平队列（FIFO），避免大任务长期霸占许可。
- **背压**：`TASK_MAX_ACTIVE` 限制未终态任务数，超限 503；`TASK_MAX_TARGETS` 限制单任务规模。两者叠加保证最坏情况下的在途探测数 ≤ `TASK_MAX_ACTIVE × TASK_MAX_TARGETS`（但实际同时在跑 ≤ `PROBE_MAX_CONCURRENCY`）。
- **为什么不用线程池**：探测是 IO 等待，协程调度开销远小于线程；且线程池会引入跨线程写 Task 的同步问题。

## 6. 限流

**算法：令牌桶（连续补充）**

```
tokens(t) = min(capacity, tokens(t0) + (t - t0) * rps)
```

- 桶容量 = `RATE_LIMIT_BURST`（允许的瞬时突发），补充速率 = `RATE_LIMIT_RPS`。
- 拒绝时 `Retry-After = ceil((1 - tokens) / rps)`（至少 1 秒，符合 HTTP 头整数秒语义）。
- **为什么是令牌桶而不是固定窗口**：固定窗口有窗口边界双倍突发问题；令牌桶天然支持突发 + 平滑速率，实现也就几十行。
- **为什么不用漏桶**：漏桶（如 `asyncio.Queue` + 固定出队速率）适合"整形"而非"限流"，这里要的是"允许一定突发、长期不超过 rps"，令牌桶更贴合。
- **限流键 = TCP 对端 IP**（`scope["client"]`）。**不信任 X-Forwarded-For**：该头可被客户端随意伪造，只有可信代理清洗后才可用；部署在代理后时应由代理层限流或显式开启信任配置（当前版本不做，避免半吊子实现）。
- **内存治理**：每个 IP 一个桶；桶空闲超过 `RATE_LIMIT_IDLE_TTL_SECONDS` 惰性清扫（每 1024 次检查或桶数超 4096 触发），防止伪造来源打爆内存。
- 只作用于 `/api/*`：`/healthz` 与 `/` 不受限，保证存活探针与信息页永远可用。

## 7. 存储与内存治理

单实例内存存储（`store.py`），三条规则让内存有界：

1. **TTL 惰性清理**：终态任务超过 `TASK_TTL_SECONDS` 在 `add/get/list` 时清理；
2. **条数上限**：超过 `TASK_MAX_RETAINED` 时按 `finished_at` 淘汰最旧的终态任务；**运行中的任务永不淘汰**；
3. **背压**：活动任务数达到 `TASK_MAX_ACTIVE` 后直接拒绝新任务（503）。

结果存储按提交顺序用固定长度列表占位：`results[i] = None` 表示未完成，探测协程写自己的槽位，不需要排序或加锁。

## 8. 取消与关停

- `DELETE /api/v1/tasks/{id}` → `service.cancel()`：`asyncio.Task.cancel()` 取消协调协程 → `gather` 取消所有在途探测协程（`CancelledError` 沿调用栈传播）→ `_run` 的 `except CancelledError` 分支把**未完成槽位**补为 `cancelled`（已出的结果保留）→ 任务终态 `CANCELLED` → 重新抛出以保留取消语义，外层 `cancel()` 收敛。
- 任务已终态时取消返回 409（`TASK_ALREADY_FINISHED`）。
- 进程关停：FastAPI lifespan 的 shutdown 阶段调用 `service.shutdown()`，取消全部在途任务并等待清理，配合 uvicorn `--timeout-graceful-shutdown` 默认行为。
- 关键约束：探测协程**绝不吞 `CancelledError`**（prober 里有明确注释），否则取消会退化为"等它跑完"。

## 9. 错误模型与可观测性

- 统一错误信封 `{"error": {code, message, request_id, details}}`：HTTP 状态码与业务 code 双通道，前端/脚本可按 code 分支。
- 所有框架级异常（参数校验、404/405/415）都被 `@app.exception_handler` 转成同一信封，避免 FastAPI 默认 `{"detail": ...}` 与业务错误两种形状并存。
- `X-Request-ID`：中间件透传合法值（限长 64、限字符集），否则生成 uuid4；同步写入响应头、日志字段与错误体。JSON 日志字段：`ts/level/logger/msg/request_id/method/path/status/duration_ms/client_ip`。

## 10. 测试策略

- **真实本地监听器**：open 用 `socket.listen`（不 `accept`，内核完成握手即可）；closed 用"绑定后立即关闭"制造 RST；dns_error 用 RFC 2606 的 `.invalid` 保留 TLD（保证 NXDOMAIN 且不外呼）。
- **确定性超时**：monkeypatch `_connect_once` 抛 `TimeoutError`，不依赖网络黑洞。
- **可注入时钟**：令牌桶/存储测试注入假时钟，避免 `sleep`。
- **打桩编排层**：`test_service.py` 用 fake probe 验证顺序、取消、背压语义，不触发真实网络。
- API 集成测试用 `TestClient`（ASGI 进程内），覆盖状态码、信封、限流头与轮询流程。

> 环境差异提示：`closed` 分支依赖系统对未监听端口回 RST。个别环境（防火墙 stealth 模式、
> 代理软件）会**延迟**送达 RST（实测某安全软件延迟约 2 秒），因此集成测试与冒烟脚本为 closed
> 目标预留 5 秒预算（大于延迟窗口），断言仍为严格的 `closed`；若环境彻底静默丢包，返回的
> `timeout` 同样是诚实结果——这正是 `timeout` 语义存在的原因。

## 11. 安全考量

- 服务本身是"内网工具型"定位：**无鉴权**，任何人可让它探测任意目标（SSRF/端口扫描面）。作业范围内接受；生产化必须加 API Key、目标白名单、出口网络策略与审计日志。
- 限流只降低滥用速率，不解决授权问题。
- 不信任 `X-Forwarded-For`（见 §6）；日志不记录请求体（避免记录敏感目标清单时把凭据一并落盘——当前请求体本就无凭据）。
- DNS 解析发生在服务端 executor 线程，受同一信号量约束，不会成为无限并发入口。

## 12. 后续改进路线

1. **多实例**：`TaskStore` 抽象成接口，Redis（任务 hash + TTL）实现，任务调度用队列（如 Redis Streams / Kafka）。
2. **鉴权与授权**：API Key → JWT；目标允许清单（CIDR 白名单）与每租户配额。
3. **指标**：Prometheus（任务状态计数、探测耗时直方图、限流命中数）；OpenTelemetry 链路。
4. **探测增强**：Happy Eyeballs（双栈并发首包）、可配置探测类型（如 HTTP HEAD/ICMP 扩展）。
5. **请求体大小限制**：uvicorn 层限制 body 大小，防超大 JSON 占内存。
