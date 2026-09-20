# syntax=docker/dockerfile:1.7
#
# 闲鱼监控 自包含镜像
#
# 这份 Dockerfile 不再依赖外部基线镜像（原先是 ghcr.io/usagi-org/ai-goofish:latest
# 之上再挂 app-overrides/src 做叠加层）。上游后端基线已内化到本仓库根目录，
# 因此镜像可以独立、可复现地构建：源码进版本控制，构建结果只由本仓库内容决定。
#
# 三阶段：
#   1) frontend-builder —— Node 22 构建 Vue 3 前端
#   2) builder          —— 独立 venv 装 Python 运行时依赖
#   3) 运行镜像         —— 只拷必要产物，非 root 运行

# =============================================================================
# 阶段 1：前端构建
# =============================================================================
FROM node:22-alpine AS frontend-builder

# WORKDIR 必须是 /web-ui：web-ui/vite.config.ts 里构建输出目录写的是
#   outDir: path.resolve(__dirname, '../dist')
# 因此 __dirname=/web-ui 时产物落在容器根目录 /dist，而不是 /app/dist。
# tests/test_frontend_build_paths.py 会强制校验这条路径链路，
# 下面阶段 3 的 `COPY --from=frontend-builder /dist` 必须与之一致。
WORKDIR /web-ui

# 先只拷依赖清单，让 npm 依赖层可以独立命中缓存。
# 注意：web-ui/ 目录由前端同事重建中；若该目录暂时不存在或为空，
# 本阶段会失败 —— 此时可临时删掉本阶段，并在阶段 3 把 dist 改为直接
# COPY 仓库里已有的预构建产物（见阶段 3 的注释）。web-ui 恢复后即可改回。
COPY web-ui/package*.json ./

# 有 lockfile 用 npm ci 保证可复现；没有则退化为 npm install。
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY web-ui/ ./
RUN npm run build

# =============================================================================
# 阶段 2：Python 运行时依赖
# =============================================================================
FROM python:3.11-slim-bookworm AS builder

# PIP_INDEX_URL 可换源（国内网络构建时非常关键）：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
# docker-compose.yml 里也通过 build.args 透传，默认走清华源。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN python3 -m venv "$VIRTUAL_ENV"

# 只装运行时依赖（requirements.txt 里的 pytest/coverage 等开发依赖不进镜像）。
COPY requirements-runtime.txt /tmp/requirements-runtime.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url "$PIP_INDEX_URL" -r /tmp/requirements-runtime.txt

# =============================================================================
# 阶段 3：运行镜像
# =============================================================================
FROM python:3.11-slim-bookworm

WORKDIR /app

# PLAYWRIGHT_BROWSERS_PATH 必须在安装浏览器之前就生效，否则 playwright 会把
# Chromium 装到默认的用户级缓存目录，运行时找不到。
# RUNNING_IN_DOCKER=true 让 src/scraper.py 的 _resolve_browser_channel() 固定选
# chromium —— 镜像里没有 Chrome / Edge 内核，不设就会去启动不存在的通道而失败。
ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    RUNNING_IN_DOCKER=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}

# tini：作为 PID 1 负责转发信号、回收僵尸进程（爬虫子进程较多，init 很关键）。
# libzbar0：pyzbar 的运行时依赖，扫码登录会用到。
# playwright install --with-deps：安装 Chromium 本体 + 其系统库依赖。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        tini \
        libzbar0 \
    && playwright install --with-deps --no-shell chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home --shell /usr/sbin/nologin appuser

# 前端构建产物（来自阶段 1 的 /dist，见阶段 1 的路径说明）
COPY --from=frontend-builder /dist /app/dist

COPY src /app/src
COPY spider_v2.py /app/spider_v2.py
COPY prompts /app/prompts
COPY static /app/static
COPY config.json.example /app/config.json.example

# 规范化读权限。COPY 会保留构建上下文里的文件 mode，而某些 NAS / 共享文件系统
# （例如 fnOS 的 ACL）下源码文件可能是 0o000 —— 属主自己都读不了。此时镜像内
# /app/src/__init__.py 会变成 `----------`，非 root 的 appuser 一 import 就
# PermissionError，容器无限重启。这里显式补上读权限，让构建结果不依赖
# 宿主机的权限状态。
RUN chmod -R a+rX /app/src /app/spider_v2.py /app/prompts /app/static /app/dist

# 运行时目录。这些目录在 compose 里会被宿主机 bind mount 覆盖，
# 这里先建好是为了：a) 无挂载直接 docker run 也能跑；b) 保证属主是 appuser。
RUN mkdir -p /app/data /app/state /app/logs /app/images /app/jsonl /app/price_history \
    && chown -R appuser:appuser /app ${VIRTUAL_ENV} ${PLAYWRIGHT_BROWSERS_PATH}

EXPOSE 8000

# 非 root 运行。Chromium 在容器内以非 root 启动需要 --no-sandbox，
# src/scraper.py 的启动参数里已经带上了。
#
# 注意：compose 里 ./data-production、./state-production 等是宿主机 bind mount，
# 镜像内的 chown 管不到宿主机目录。在原生 Linux 上如果这些宿主机目录已存在且
# 属主不是 1000，容器内 appuser 会没有写权限，需要自行执行：
#   chown -R 1000:1000 data-production state-production logs-production \
#                      jsonl-production price-history-production images-production
USER appuser

# /health 由 src/app.py 提供，且认证中间件只拦 /api/*，所以健康检查无需登录。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# tini 作为 PID 1 转发信号并回收僵尸进程（爬虫会起较多子进程）。
# `-s` 让它以 subreaper 方式注册：这样在 compose 设置了 `init: true` 的场景下
# （此时 Docker 自己注入的 tini 才是 PID 1，本层 tini 不是），本层 tini 依然能
# 正常回收孙进程，且不会打印 "Tini is not running as PID 1" 的警告。
# 不加 -s 时，`docker run` 直接跑（本层 tini 是 PID 1）也没问题，两种入口都覆盖。
ENTRYPOINT ["tini", "-s", "--"]

CMD ["python", "-m", "src.app"]
