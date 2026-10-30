# tcp-probe-service 常用开发与运维命令（GNU make）
# 约定：Python 命令统一走 `python -m ...`，不依赖虚拟环境 bin 目录是否在 PATH 中。
# 可用 `make PYTHON=.venv/bin/python test` 覆盖解释器路径。

PYTHON     ?= python
IMAGE      ?= tcp-probe-service:latest
SMOKE_ARGS ?=

.PHONY: install dev test run docker-build docker-up docker-down smoke load

# 安装运行时依赖
install:
	$(PYTHON) -m pip install -r requirements.txt

# 安装开发依赖（在运行时依赖之上追加 pytest / httpx）
dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

# 运行单元测试
test:
	$(PYTHON) -m pytest

# 本地前台启动服务（Ctrl+C 退出）
run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log

# 构建 Docker 镜像（镜像名与 docker-compose.yml 中的 image 保持一致）
docker-build:
	docker build -t $(IMAGE) .

# 后台启动容器（必要时自动重新构建）
docker-up:
	docker compose up -d --build

# 停止并移除容器
docker-down:
	docker compose down

# 端到端冒烟测试（要求服务已在运行；本地或容器方式均可）
# 服务在容器里时，容器内的 127.0.0.1 不是宿主机，需要：
#   make smoke SMOKE_ARGS="--target-host host.docker.internal"
smoke:
	$(PYTHON) -m scripts.smoke_test $(SMOKE_ARGS)

# 轻量压测（建议先以 RATE_LIMIT_ENABLED=false 启动服务）
load:
	$(PYTHON) scripts/load_test.py
