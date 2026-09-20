# 闲鱼智能监控（xianyu-hunter）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT) [![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue.svg)](https://www.python.org/) [![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](https://www.docker.com/)

闲鱼（goofish）商品监控系统：用 Playwright 采集搜索结果，按关键词规则与 AI 多模态分析筛选商品，对关注的商品做降价 / 售罄 / 下架监控，并通过 7 类渠道推送通知，附带 Vue 3 Web 控制台。

本仓库自包含：源码、`Dockerfile`、`docker-compose.yml`、依赖清单与 CI 全部在本仓库内，构建结果只由本仓库内容决定。上游基线已内化到仓库根目录，不存在只读覆盖层。

内化的上游基线为 `AAtomical/ai-goofish-monitor` 的
`f85d140b6b45029d9a0925feb96dad733b41396d`（2026-05-18）。自包含化之后**不再自动跟随上游更新**，
如需同步上游新改动，以该 commit 为基线做一次人工 diff 评审后再合并，不要直接覆盖本仓库文件
（本仓库对上游的多处缺陷做了修复，直接覆盖会丢改动）。

## 核心能力

| 模块 | 解决的问题 |
|---|---|
| 商品采集 | `spider_v2.py` + `src/scraper.py`，Playwright 驱动 Chromium 抓取搜索结果，支持关键词、价格区间、包邮、地区、个人卖家、页数上限等筛选 |
| AI 分析 | `src/services/ai_service.py`、`src/infrastructure/external/ai_client.py` 走 OpenAI 兼容接口，按 Prompt 模板对商品（含商品图）打分并给出推荐理由 |
| 关键词规则 | `src/keyword_rule_engine.py` 单组 OR 逻辑，纯英数字关键词按完整词边界匹配，避免 `Q1` 误命中 `Q1R5` |
| 评分桥接 | `src/services/scoring_service.py` + `analysis_scoring_bridge.py` 把 AI 分与规则分归一，避免单维度否决 |
| 关注与行情监控 | `src/services/watch_service.py`、`watch_state.py`、`liveness_service.py`：跨次比价捕获降价，并读取闲鱼原生的「收藏后降价」累计额（可覆盖首次观测之前发生的降价）；粘性状态机判定售罄 / 下架 / 重上架 |
| 协议层 | `src/services/xy_protocol/`：mtop 签名（`signer.py`）、`ret` 码存活判定（`status.py`）、错误分类（`errors.py`），全部为纯函数，可离线验证 |
| 通知推送 | `src/infrastructure/external/notification_clients/` 支持 Bark、ntfy、Gotify、企业微信机器人、Telegram、通用 Webhook、邮件（SMTP）共 7 类渠道，可同时启用并发推送 |
| 通知聚合与去重 | `src/ai_handler.py` 的 `AGGREGATE_WINDOW_SECONDS = 25` 合并批量推荐；`notification_dedup_service.py` 做跨任务去重（默认窗口 3600s） |
| 行情日报 | `src/services/daily_report_service.py` 按配置时间统计关注关键词的均价 / 最低 / 最高 / 涨跌并推送，配置存 SQLite 的 `app_metadata` 表，改动即时生效 |
| 账号管理 | `src/api/routes/accounts.py` 管理多个登录态 JSON，支持一键检测与定时保活（默认 4 小时），失效自动通知 |
| 账号 / 代理轮换 | `src/rotation.py` + `account_strategy_service.py`：按任务或按失败轮换账号与代理，降低单号风控风险 |
| 任务失败保护 | `src/failure_guard.py`：连续失败达阈值自动暂停任务，避免持续无效请求 |
| 任务调度 | `src/services/scheduler_service.py` + `src/core/cron_utils.py`，基于 APScheduler 解析任务 cron 并按采集间隔触发子进程 |
| 数据持久化 | 任务与结果存 SQLite（默认 `data/app.sqlite3`）；启动时把 legacy 的 `config.json`、`jsonl/`、`price_history/` 一次性导入（`sqlite_bootstrap.py`） |
| Web 控制台 | `web-ui/` Vue 3 + Vite + Tailwind，页面含 Dashboard、关注、任务、账号、结果、日志、设置；WebSocket 实时推送任务状态 |
| 登录认证 | 登录接口 `POST /auth/status` 签发 HttpOnly Cookie 会话（HMAC-SHA256），中间件保护全部 `/api/*`，`/health` 免认证 |

## 快速开始

### 本地运行

```bash
# 1. 生成配置文件
cp .env.example .env

# 2. 一键启动（检查依赖 → 装 Python 依赖 → 构建前端 → 启动后端）
./start.sh
```

`start.sh` 要求 Python >= 3.10、Node.js 与 npm 已安装；前端产物输出到**仓库根** `dist/`。启动后访问 `http://localhost:8000`，API 文档在 `http://localhost:8000/docs`。

Windows 或不使用 `start.sh` 时手动执行等价步骤：

```bash
python -m pip install -r requirements.txt
cd web-ui && npm install && npm run build && cd ..
python -m src.app
```

### Docker 运行

```bash
# 1. 配置文件（compose 通过 env_file 读取，必须存在）
cp .env.example .env

# 2. config.json 必须先作为「文件」存在，否则 Docker 会把挂载源建成目录导致启动失败
echo '[]' > config.json

# 3. 运行时数据目录
mkdir -p data-production state-production logs-production jsonl-production price-history-production images-production

# 4. 构建并启动
docker compose up -d
```

访问 `http://localhost:8787`（容器内监听 8000，compose 映射为 `8787:8000`）。

原生 Linux 上若上述宿主机目录已存在且属主不是 UID 1000，容器内非 root 的 `appuser` 会写不进去：

```bash
chown -R 1000:1000 data-production state-production logs-production jsonl-production price-history-production images-production
```

## 配置说明

**所有用户配置只写 `.env`。** Python 侧已将 `.env` 确立为唯一配置真源（`.env` 优先于进程环境变量）；`docker-compose.yml` 的 `environment` 块刻意只保留系统级键，不要往里添加 `OPENAI_*` / `BARK_URL` / `WEB_USERNAME` 等用户配置项。

| 变量 | 是否必需 | 说明 |
|---|---|---|
| `WEB_USERNAME` | 可选 | Web 登录用户名，默认 `admin` |
| `WEB_PASSWORD` | 可选 | Web 登录密码，默认 `admin123`；生产环境务必修改，含 `your_` 的值会被判定为未配置 |
| `WEB_SESSION_SECRET` | 可选 | 会话签名密钥；留空时自动生成并持久化到 `data/.session_secret`，重启不失效 |
| `SESSION_TTL_HOURS` | 可选 | 登录会话有效期（小时），默认 72 |
| `OPENAI_API_KEY` | 可选 | AI 分析用；不填则退化为纯关键词模式 |
| `OPENAI_BASE_URL` | 可选 | OpenAI 兼容接口地址，例如 `https://api.openai.com/v1/` |
| `OPENAI_MODEL_NAME` | 可选 | 模型名，必须是支持图片输入的多模态模型（商品图分析依赖此能力） |
| `AI_IMAGE_MODE` | 可选 | 图片分析模式：`auto`（默认）/ `on` / `off` |
| `SKIP_AI_ANALYSIS` | 可选 | 设为 `true` 则只走关键词规则，不调用模型，默认 `false` |
| `AI_ANALYSIS_CONCURRENCY` | 可选 | AI 分析并发数，默认 2 |
| `BARK_URL` | 可选 | Bark 推送，格式 `https://api.day.app/你的key` |
| `NTFY_TOPIC_URL` | 可选 | ntfy 推送，例如 `https://ntfy.sh/你的主题名` |
| `GOTIFY_URL` + `GOTIFY_TOKEN` | 可选 | Gotify 推送，需成对配置 |
| `WX_BOT_URL` | 可选 | 企业微信群机器人 Webhook |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | 可选 | Telegram 推送，需成对配置 |
| `WEBHOOK_URL` | 可选 | 通用 Webhook 推送，配合 `WEBHOOK_METHOD` / `WEBHOOK_HEADERS` / `WEBHOOK_BODY` 等 |
| `RUN_HEADLESS` | 可选 | 无头模式，默认 `true`；Docker 部署必须为 `true` |
| `ACCOUNT_STATE_DIR` | 可选 | 账号登录态目录，默认 `state` |
| `ACCOUNT_CHECK_INTERVAL_HOURS` | 可选 | 账号保活检测间隔（小时），默认 4 |
| `ACCOUNT_ROTATION_ENABLED` | 可选 | 是否启用账号轮换，默认 `false` |
| `PROXY_ROTATION_ENABLED` + `PROXY_POOL` | 可选 | 代理轮换开关与代理列表（逗号分隔） |
| `TASK_FAILURE_THRESHOLD` | 可选 | 连续失败几次后暂停任务，默认 3 |
| `TASK_DEFAULT_COLLECTION_INTERVAL_MINUTES` | 可选 | 新建任务默认采集间隔（分钟），默认 30 |
| `TASK_LOG_RETENTION_DAYS` | 可选 | 任务日志保留天数，默认 7 |

上表之外，`.env.example` 还有 AI 行为微调、用量成本计价、轮换细节、自动咨询话术、调试开关等完整分组，均有注释说明。

系统级键不要写进 `.env`（会破坏容器结构）：`SERVER_PORT`、`RUNNING_IN_DOCKER`、`PLAYWRIGHT_BROWSERS_PATH`、`APP_DATABASE_FILE`。这些由镜像与 compose 固定。日报的开关与推送时间由网页「设置 - 行情日报」控制（存 SQLite），`DAILY_REPORT_ENABLED` / `DAILY_REPORT_HOUR` 两个环境变量当前不参与运行逻辑。

## 原生筛选的实现方式

闲鱼原生筛选（个人闲置 / 包邮 / 全新 / 区域 / 价格区间 / 发布时间）**不走 URL query 参数**。
页面加载后由前端 JS 自己发一个 POST 到 `h5api.m.goofish.com`，业务参数放在 body 的
`data` 字段里。因此「往搜索 URL 上拼 `&personal_only=true`」是无效的——服务端只会忽略
不认识的键，表现为**筛选静默失效**。

`src/services/search_filter_injection.py` 的做法是拦截浏览器自己发出的那个 POST，
把筛选合并进 `data` 后用请求自带的 `t` 与 cookie 里的 `_m_h5_tk` 重算签名再放行。
它不引用任何 CSS 选择器，所以平台改前端样式不会让它失效。

三个必须留意的细节（都有测试钉住）：

- **失败即放行。** 注入是纯优化：payload 不认识、token 取不到、Playwright 抛错，
  任何一条路径都必须原样放行原请求，绝不能让采集链路因此中断。
- **不碰页码。** 只覆盖 `propValueStr.searchFilter` 与 `extraFilterValue`，
  `pageNumber` / `sortValue` 保持浏览器原值。翻页由页面驱动，注入把页码改回 1 会让每页都抓第一页。
- **保留原 body 的风控字段。** 真实请求体除 `data` 之外还有平台注入的
  `bx-ua` / `bx-umidtoken` / `bx_et`，重建 body 时必须保留（含字段顺序），否则发出的请求与浏览器不一致。

`searchFilter` 的取值与拼串规则、`quickFilter` 的 8 项对照表（注意 **`inspectedPhone` 是「严选」而不是「验货宝」**，
后者是 `filterAppraise`）、以及 `divisionList` 的结构（**一个元素承载一条完整地域路径**，
`province`/`city`/`area` 为平级字段），均已对齐闲鱼自己的前端实现。

## 测试与 CI

```bash
# 安装开发依赖（在 requirements.txt 之上追加 pytest 版本下限与 pytest-cov）
python -m pip install -r requirements-dev.txt

# 运行离线测试集
python -m pytest -q -m "not live and not live_slow"
```

本机实测结果：`1154 passed, 3 deselected in 21.60s`（Python 3.13.15 / Windows；CI 在 Python 3.11 上运行同一命令）。

pytest 配置见 `pyproject.toml`：`testpaths = ["tests"]`，默认 `addopts = "-v --tb=short"`，并定义两个 marker：`live`（需要真实凭据与外部服务的真实流量冒烟测试）与 `live_slow`（更慢的可选用例，如真实 AI 任务生成）。live 用例默认关闭，需显式设置 `RUN_LIVE_TESTS=1` 才会执行：

```bash
RUN_LIVE_TESTS=1 pytest tests/live -m live -v
```

另有协议模块的离线自检脚本（纯函数断言，不发网络请求、不读 cookie、不连数据库）：

```bash
python scripts/check_protocol.py
```

CI（`.github/workflows/ci.yml`）在 push 与 pull_request 时跑三个 job：

- `backend-tests`：Python 3.11，`pytest -m "not live and not live_slow" --cov=src`，上传 `coverage.xml` 产物
- `frontend-build`：Node 20，`npm ci`（无 lockfile 时 `npm install`）后 `npm run build`，并断言产物落在仓库根 `dist/index.html`；`web-ui/package.json` 不存在时整个 job 跳过
- `compose-config`：`docker compose config --quiet`，只做语法与插值校验，不启动容器

## 项目结构

```
xianyu-hunter/
├── src/
│   ├── app.py                  # FastAPI 入口：路由注册、认证中间件、lifespan
│   ├── scraper.py              # Playwright 采集主逻辑
│   ├── ai_handler.py           # AI 分析编排与通知聚合（25s 窗口）
│   ├── keyword_rule_engine.py  # 关键词规则引擎
│   ├── config.py               # legacy 配置兼容层
│   ├── rotation.py             # 账号 / 代理轮换
│   ├── failure_guard.py        # 任务失败保护
│   ├── api/
│   │   ├── auth.py             # HMAC-SHA256 会话 token
│   │   └── routes/             # 路由模块（tasks/dashboard/watchlist/accounts/search/storage/...）
│   ├── core/                   # cron 解析等通用工具
│   ├── domain/                 # 领域模型与仓储接口
│   ├── infrastructure/
│   │   ├── config/             # settings.py（Pydantic）、env_manager.py
│   │   ├── external/           # AI 客户端与 7 类通知客户端
│   │   └── persistence/        # SQLite 连接、schema、legacy 数据迁移
│   └── services/               # 42 个业务服务模块 + xy_protocol/ 协议支撑层
├── web-ui/                     # Vue 3 + Vite + Tailwind 前端源码
├── tests/                      # pytest：unit / integration / live
├── scripts/check_protocol.py   # mtop 协议离线自检
├── prompts/                    # AI Prompt 模板（基础模板 + 商品类目标准）
├── static/                     # 图片等静态资源
├── spider_v2.py                # 采集 CLI 入口
├── start.sh                    # 本地一键启动脚本
├── Dockerfile                  # 三阶段构建：前端 → Python 依赖 → 非 root 运行镜像
├── docker-compose.yml          # 自包含部署编排
├── pyproject.toml              # pytest 与覆盖率配置
├── requirements{,-dev,-runtime}.txt
└── .env.example                # 配置模板（唯一配置入口）
```

前端构建产物固定输出到仓库根 `dist/`（由 `web-ui/vite.config.ts` 的 `outDir` 决定），后端从该目录挂载静态资源，改这个路径会导致前端 500。

## 免责声明

本工具仅供学习与技术研究使用。请遵守目标网站的 `robots.txt` 与服务条款，不要高频请求；合理控制采集频率与并发，避免对平台造成压力。恶意抓取、骚扰卖家等行为造成的后果由使用者自行承担。
