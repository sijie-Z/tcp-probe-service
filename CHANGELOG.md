# 变更记录

## v1.1.0（2026-10-30）

### 新增
- `GET /metrics`：Prometheus 文本格式计数器（任务提交/拒绝、任务终态、目标结果分布、限流与请求体拦截计数），手写零依赖实现；不参与限流
- 请求体大小上限 `MAX_BODY_BYTES`（默认 1 MiB），超限返回 413 `PAYLOAD_TOO_LARGE` 统一信封
- OpenAPI 契约测试（断言 served schema 覆盖文档化端点与关键字段）
- CI 覆盖率报告（coverage.xml）
- `scripts/load_test.py` 压测脚本；README「性能自测」含本机实测数据
- Docker 镜像构建工作流（tag / 手动触发，构建后跑容器与冒烟）；主 CI 增加 compose 配置校验
- README 增加架构图（mermaid）

### 变更
- 测试规模：76 → 77 项（新增 /metrics 计数器测试）

## v1.0.0（2026-10-30）

首个稳定版本：
- 批量 TCP 连通性探测（HTTP 异步任务模型，wait 长轮询可选）
- 令牌桶限流（按来源 IP，含清扫冷却与硬上限）、活动任务背压、在途连接信号量
- 取消/关停链路、TTL 与容量治理、统一错误信封、X-Request-ID、结构化日志
- 冒烟脚本 7 项检查（open/closed/dns_error、异步轮询、错误契约、限流）
- GitHub Actions CI 全绿、Docker 镜像实测（healthy + 冒烟 6/6 当次）
