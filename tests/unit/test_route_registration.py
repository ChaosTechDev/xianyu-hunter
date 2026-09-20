"""路由注册的完整性测试。

守住一个真实发生过的缺陷：``src/api/routes/search.py`` 定义了完整的
``/api/search/items`` 接口，前端 ``web-ui/src/api/search.ts`` 也在调用它，
但 ``src/app.py`` 的 ``include_router`` 列表里漏了它——于是这个功能在运行时
彻底不可达，且**不会报任何错**：前端只是永远拿到 404。

这类「代码写好了但没接上」的缺陷靠人工 review 极易漏掉，所以用测试钉住：
在 ``openapi()`` 里逐个断言关键路径存在。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

os.environ.setdefault(
    "APP_DATABASE_FILE", os.path.join(tempfile.mkdtemp(prefix="routes_"), "a.sqlite3")
)

from src.app import app  # noqa: E402


@pytest.fixture(scope="module")
def registered_paths() -> set[str]:
    return set(app.openapi().get("paths", {}).keys())


class TestEveryRouteModuleIsRegistered:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/search/items",
            "/api/storage/usage",
            "/api/storage/retention/plan",
            "/api/storage/retention/execute",
            "/api/tasks/",
            "/api/results/files",
            "/api/watchlist",
        ],
    )
    def test_critical_path_is_registered(self, registered_paths, path):
        assert path in registered_paths, f"{path} 未注册到 app，功能不可达"

    def test_search_route_is_reachable(self, registered_paths):
        """回归用例：``search`` 路由曾定义了却未在 app.py 注册。"""
        assert "/api/search/items" in registered_paths

    def test_no_route_module_is_left_unregistered(self, registered_paths):
        """扫描 ``src/api/routes/`` 下的路由模块，确认每个都在 app 里注册。

        ``websocket`` 走 WebSocket 协议不出现在 openapi 的 paths 里，因此单独放行；
        其余模块都必须能在 openapi 中找到对应前缀。
        """
        routes_dir = repo_root / "src" / "api" / "routes"
        module_prefixes: dict[str, str] = {}
        for file in sorted(routes_dir.glob("*.py")):
            if file.name == "__init__.py":
                continue
            text = file.read_text(encoding="utf-8")
            # 从 APIRouter(prefix="...") 里取出前缀
            marker = 'APIRouter(prefix="'
            if marker not in text:
                continue
            start = text.index(marker) + len(marker)
            prefix = text[start : text.index('"', start)]
            module_prefixes[file.stem] = prefix

        # WebSocket 路由不在 openapi 中，属已知例外
        exempt = {"websocket"}
        missing = []
        for module, prefix in module_prefixes.items():
            if module in exempt:
                continue
            if not any(p.startswith(prefix) for p in registered_paths):
                missing.append(f"{module} ({prefix})")

        assert not missing, f"以下路由模块定义了但未注册到 app: {missing}"
