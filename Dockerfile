FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 安装 uv（Astral 官方推荐方式）
RUN pip install --no-cache-dir uv

# 先复制依赖清单并安装，利用 Docker 层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 复制应用代码与配置
COPY app.py .
COPY config ./config

EXPOSE 18088

CMD ["uv", "run", "gunicorn", "app:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:18088", \
     "--workers", "2", \
     "--timeout", "30"]
