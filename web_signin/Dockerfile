FROM python:3.12-slim

# 基础环境设置
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先复制依赖清单，便于源码更新时复用安装缓存。
COPY web_signin/requirements.txt /app/web_signin/requirements.txt

# 安装 Web 版所需依赖
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --index-url "$PIP_INDEX_URL" --upgrade pip && \
    pip install --index-url "$PIP_INDEX_URL" -r web_signin/requirements.txt

# 拷贝源码，复用根目录下的 utils 等模块。
COPY . /app

# 容器默认端口为 9988，Compose 默认映射到宿主机 3155。
EXPOSE 9988

# 容器启动命令
CMD ["python", "web_signin/run.py"]

