# tcp-probe-service 容器镜像
# 构建：docker build -t tcp-probe-service:latest .
# 运行：docker run --rm -p 8000:8000 tcp-probe-service:latest
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE：不生成 .pyc，只读文件系统也能正常运行
# PYTHONUNBUFFERED：stdout/stderr 实时输出，容器日志不滞留
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# PyPI 镜像源：默认使用清华镜像（国内构建更快、避免直连 pypi.org 超时）；
# 海外/内网环境可覆盖：docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple .
# 镜像不可达时（如 GitHub 云端 runner 访问清华镜像偶发超时）自动回退官方 PyPI，
# 首次尝试收紧重试参数（--retries 1 --timeout 10），让回退尽快发生。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先只拷依赖清单并安装：requirements.txt 不变时可命中缓存层，
# 后续改业务代码不会触发依赖重装
COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 1 --timeout 10 -i "${PIP_INDEX_URL}" -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

# 非 root 用户运行（固定 uid 10001，便于权限审计与宿主机目录映射）
RUN useradd --create-home --uid 10001 appuser

COPY app ./app

USER 10001

EXPOSE 8000

# 存活探针：仅用标准库请求 /healthz；连接失败或非 2xx 时 urlopen 抛异常，
# 进程退出码非 0，容器被标记为 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/healthz', timeout=3)"]

# 固定监听 8000；如需换端口，请同步调整 EXPOSE、compose 端口映射与健康检查
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
