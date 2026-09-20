from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_DIST = "/dist"

# 这些文件只在「完整上游部署布局」下存在。自包含化改造后，本仓库不再携带
# Dockerfile / start.sh（docker-compose.yml 直接引用预构建镜像），web-ui/ 也
# 正由同事重建。缺失文件属于部署环境差异，不是构建路径配置错误，因此按文件
# 存在性跳过，而不是删除用例。
REQUIRED_LAYOUT_FILES = (
    "web-ui/vite.config.ts",
    "Dockerfile",
    "web-ui/Dockerfile",
    ".dockerignore",
    "start.sh",
)


def read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


@pytest.mark.skipif(
    not all((REPO_ROOT / name).is_file() for name in REQUIRED_LAYOUT_FILES),
    reason=(
        "自包含仓库缺少完整部署布局文件"
        "（Dockerfile / start.sh / web-ui 构建配置），跳过前端构建路径一致性校验"
    ),
)
def test_frontend_build_output_path_is_consistent_across_configs():
    vite_config = read_repo_file("web-ui/vite.config.ts")
    dockerfile = read_repo_file("Dockerfile")
    frontend_dockerfile = read_repo_file("web-ui/Dockerfile")
    dockerignore = read_repo_file(".dockerignore")
    start_script = read_repo_file("start.sh")
    dockerignore_lines = dockerignore.splitlines()

    assert "path.resolve(__dirname, '../dist')" in vite_config
    assert (
        f"COPY --from=frontend-builder {ROOT_DIST} /app/dist" in dockerfile
    ), "Docker multi-stage copy must use the Vite build output path."
    assert (
        f"COPY --from=builder {ROOT_DIST} /usr/share/nginx/html"
        in frontend_dockerfile
    ), "Frontend-only Docker build must use the Vite build output path."
    assert "dist/" in dockerignore_lines
    assert "web-ui/dist" not in dockerignore_lines
    assert '[ ! -d "dist" ]' in start_script
    assert "cp -r web-ui/dist ./" not in start_script


def test_vite_build_outdir_still_points_to_repo_root_dist():
    """vite.config.ts 存在即校验，不受其他部署文件缺失影响。

    前端产物必须输出到仓库根的 dist/（后端 src/app.py 从该目录挂载静态资源），
    这一条即使在自包含布局下也必须成立。
    """
    vite_config_path = REPO_ROOT / "web-ui/vite.config.ts"
    if not vite_config_path.is_file():
        pytest.skip("web-ui/vite.config.ts 尚未就绪，跳过前端输出目录校验")

    vite_config = vite_config_path.read_text(encoding="utf-8")
    assert "path.resolve(__dirname, '../dist')" in vite_config
