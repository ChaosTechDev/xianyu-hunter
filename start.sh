#!/usr/bin/env bash
#
# 闲鱼监控 - 本地启动脚本（不使用 Docker）
#
# 做四件事：检查环境依赖 → 装 Python 依赖 → 构建前端 → 启动后端。
# 前端产物固定输出到**仓库根目录**的 dist/（后端 src/app.py 从那里挂载静态
# 资源），这一点由 web-ui/vite.config.ts 的 outDir 决定，不要改成 web-ui/dist。
#
# 容器化部署请直接用：docker compose up -d
#
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}闲鱼监控系统 - 本地启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"

# ---------------------------------------------------------------------------
# [1/5] 环境与依赖检查
# ---------------------------------------------------------------------------
echo -e "\n${YELLOW}[1/5] 检查环境与依赖...${NC}"

MISSING_ITEMS=()

if ! command -v python3 >/dev/null 2>&1; then
    MISSING_ITEMS+=("python3 (需要 >= 3.10)")
elif ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    MISSING_ITEMS+=("python3 (需要 >= 3.10，当前 $(python3 -V 2>&1))")
fi

if ! command -v node >/dev/null 2>&1; then
    MISSING_ITEMS+=("node")
fi

if ! command -v npm >/dev/null 2>&1; then
    MISSING_ITEMS+=("npm")
fi

if [ "${#MISSING_ITEMS[@]}" -ne 0 ]; then
    echo -e "${RED}检测到缺失的环境依赖：${NC}"
    for item in "${MISSING_ITEMS[@]}"; do
        echo "  - $item"
    done
    echo ""
    echo "安装建议："
    echo "  macOS        brew install python@3.11 node"
    echo "  Debian/Ubuntu sudo apt-get install -y python3 python3-venv nodejs npm"
    echo "  RHEL/Fedora  sudo dnf install -y python3 nodejs npm"
    echo "  Arch         sudo pacman -S --noconfirm python nodejs npm"
    echo "  Windows      winget install Python.Python.3.11 OpenJS.NodeJS.LTS"
    exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo -e "${RED}Python 版本过低，需要 3.10 及以上${NC}"; exit 1; }

echo -e "${GREEN}环境检查通过${NC}"

# ---------------------------------------------------------------------------
# [2/5] 配置文件就绪
# ---------------------------------------------------------------------------
echo -e "\n${YELLOW}[2/5] 检查配置文件...${NC}"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${YELLOW}已从 .env.example 生成 .env，请填写 AI 与推送配置后重新运行${NC}"
    else
        echo -e "${RED}缺少 .env 与 .env.example${NC}"; exit 1
    fi
fi

# config.json 必须是**文件**：它是 legacy 任务配置的迁移来源，
# 若被创建成目录会让启动路径报 IsADirectoryError。
if [ -d "config.json" ]; then
    echo -e "${RED}config.json 是一个目录（应为文件），已删除重建为 []${NC}"
    rm -rf config.json
fi
[ -f "config.json" ] || echo '[]' > config.json

echo -e "${GREEN}配置文件就绪${NC}"

# ---------------------------------------------------------------------------
# [3/5] Python 依赖
# ---------------------------------------------------------------------------
echo -e "\n${YELLOW}[3/5] 安装 Python 依赖...${NC}"

if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}缺少 requirements.txt${NC}"; exit 1
fi

python3 -m pip install -r requirements.txt --quiet
echo -e "${GREEN}Python 依赖安装完成${NC}"

# ---------------------------------------------------------------------------
# [4/5] 前端构建
# ---------------------------------------------------------------------------
echo -e "\n${YELLOW}[4/5] 构建前端...${NC}"

# 旧产物先清掉，避免构建失败时误用陈旧 dist 启动。
if [ -d "dist" ]; then
    rm -rf dist
    echo "已清理旧的 dist/"
fi

if [ ! -d "web-ui" ]; then
    echo -e "${RED}缺少 web-ui 目录${NC}"; exit 1
fi

if [ ! -d "web-ui/node_modules" ]; then
    echo "首次运行，安装前端依赖..."
    (cd web-ui && npm install)
fi

(cd web-ui && npm run build)

# 构建产物必须落在仓库根 dist/（vite outDir 是 ../dist）。
# 这里用 `[ ! -d "dist" ]` 判断，而不是去 cp web-ui/dist，
# 因为产物路径只有一处，复制只会制造第二份来源。
if [ ! -d "dist" ]; then
    echo -e "${RED}前端构建失败：仓库根 dist/ 未生成${NC}"
    echo "请检查 web-ui/vite.config.ts 的 outDir 是否仍为 path.resolve(__dirname, '../dist')"
    exit 1
fi

echo -e "${GREEN}前端构建完成，产物位于仓库根 dist/${NC}"

# ---------------------------------------------------------------------------
# [5/5] 启动后端
# ---------------------------------------------------------------------------
echo -e "\n${YELLOW}[5/5] 启动后端服务...${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}访问地址: http://localhost:8000${NC}"
echo -e "${GREEN}API 文档: http://localhost:8000/docs${NC}"
echo -e "${GREEN}========================================${NC}\n"

exec python3 -m src.app
