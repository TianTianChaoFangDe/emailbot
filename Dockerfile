# emailbot 云端运行镜像
FROM python:3.11-slim

WORKDIR /app

# apscheduler 按 "Asia/Shanghai" 解析时区需要系统 tzdata
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 默认走清华镜像源(国内服务器构建快), 海外构建可覆盖:
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 代码量小, 直接全量拷贝后非 editable 安装
COPY pyproject.toml ./
COPY emailbot/ ./emailbot/
COPY bot.py ./
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} .

# 容器内必须监听 0.0.0.0, 否则同网络的 napcat 容器连不上
ENV HOST=0.0.0.0
ENV TZ=Asia/Shanghai

CMD ["python", "bot.py"]
