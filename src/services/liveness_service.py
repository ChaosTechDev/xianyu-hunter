"""商品存活探活服务：用详情接口的权威 ``ret`` 码判定商品是否还在售。

**为什么需要这个模块**

关注列表原本只能靠「这一轮采集没看到该商品」来判断下架，但这是**弱信号**：
闲鱼翻页不稳定、风控会吞掉部分结果，同一件在售商品可能这一轮采到、下一轮没采到。
仅凭缺失判死会误杀在售商品——用户错过好货，代价高于漏掉一个死链。

详情接口返回的 ``ret`` 码是唯一跨版本稳定的权威判据（见
:mod:`src.services.xy_protocol.status`）：``SUCCESS`` 表示在售，
``FAIL_BIZ_ITEM_DEL_NOT_FOUND`` 表示已删除。因此在对某商品准备判死之前，
先精确探活一次，能显著降低误杀率。

**为什么不直接用 HTTP 客户端**

闲鱼详情接口 ``mtop.taobao.idle.pc.detail`` 需要 ``_m_h5_tk`` 签名 cookie 与
完整的浏览器指纹头。项目已有 Playwright + 登录态文件这套可靠链路
（``src/scraper.py`` 也是这么取详情的），复用它可以避免维护第二套签名/风控逻辑，
也天然共享登录态。代价是每次探活要起一个页面，因此本模块：

- **只对通过初筛的候选做探活**，不做全量
- 内置**每轮探活次数上限**，防止一次扫描触发大量请求
- 内置**最小请求间隔**，避免触发风控
- 严格遵循**保守原则**：风控、超时、拿不到 ret 一律返回 ``alive=True``
"""
from __future__ import annotations

import glob
import os
import shutil
import time

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.config import DETAIL_API_URL_PATTERN
from src.services.xy_protocol import ItemStatus, judge_status
from src.services.xy_protocol.errors import CLASS_RISK_CONTROL, classify_error

#: 单轮扫描最多探活多少次。超出后本轮剩余商品回退到「连续缺失」兜底判定。
#: 探活要起浏览器页面，成本远高于读一次采集结果，必须有上限。
DEFAULT_MAX_PROBES_PER_SCAN = 20

#: 两次探活之间的最小间隔（秒）。宁可慢，也不要因为密集请求被风控。
DEFAULT_MIN_INTERVAL_SECONDS = 1.5


def _find_browser_executable() -> str | None:
    """定位 Chromium 可执行文件（与 official_search_service 保持同一套探测顺序）。"""
    configured = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH") or os.getenv("BROWSER_EXECUTABLE_PATH")
    candidates = [configured] if configured else []
    candidates.extend(
        [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
        ]
    )
    browser_roots = [
        os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
        "/ms-playwright",
        "/root/.cache/ms-playwright",
    ]
    browser_patterns = [
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ]
    for root in browser_roots:
        if not root:
            continue
        for pattern in browser_patterns:
            candidates.extend(glob.glob(os.path.join(root, pattern)))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_item_link(item_id: str) -> str:
    """由商品 ID 拼出详情页链接。"""
    return f"https://www.goofish.com/item?id={item_id}"


async def probe_item_alive(
    item_id: str,
    *,
    storage_state: str | None = None,
    timeout_ms: int = 25000,
) -> ItemStatus:
    """探活单个商品，返回带权威判定依据的 :class:`ItemStatus`。

    本函数**不抛异常**：任何失败都转成 ``alive=True`` 的保守结论，
    让调用方可以无脑使用，不必再包一层 try。
    """
    if not item_id:
        return ItemStatus(item_id="", alive=True, reason="商品 ID 为空，保守判活")

    try:
        async with async_playwright() as playwright:
            executable_path = _find_browser_executable()
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
            if executable_path:
                browser = await playwright.chromium.launch(
                    headless=True, executable_path=executable_path, args=launch_args
                )
            else:
                browser = await playwright.chromium.launch(headless=True, args=launch_args)

            context = (
                await browser.new_context(storage_state=storage_state)
                if storage_state and os.path.isfile(storage_state)
                else await browser.new_context()
            )
            page = await context.new_page()
            try:
                async with page.expect_response(
                    lambda r: DETAIL_API_URL_PATTERN in r.url, timeout=timeout_ms
                ) as response_info:
                    await page.goto(
                        build_item_link(item_id),
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                response = await response_info.value
                if not response.ok:
                    return ItemStatus(
                        item_id=item_id,
                        alive=True,
                        reason=f"详情接口 HTTP {response.status}，保守判活",
                        error=f"HTTP {response.status}",
                    )
                payload = await response.json()
            except PlaywrightTimeoutError as exc:
                return ItemStatus(
                    item_id=item_id,
                    alive=True,
                    reason="等待详情接口超时，保守判活",
                    error=f"PlaywrightTimeoutError: {exc}",
                )
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        return ItemStatus(
            item_id=item_id,
            alive=True,
            reason="探活过程异常，保守判活",
            error=f"{type(exc).__name__}: {exc}",
        )

    # 判定交给权威实现；这里只负责取数据
    status = judge_status(item_id, payload)

    # 风控信号要显式标注出来，方便上游决定退避或换账号
    if status.error:
        classification = classify_error(status.raw_ret or status.error)
        if classification == CLASS_RISK_CONTROL:
            status.reason = "触发风控，保守判活"
    return status


class LivenessProbe:
    """带配额与限速的探活器，供一次扫描周期内复用。

    - ``max_probes``：本轮最多真正发起多少次探活，用完即返回 ``None``，
      让调用方回退到「连续缺失」兜底判定（``None`` 表示「未探活」，
      与「探活结果=活」语义不同，不可混淆）。
    - ``min_interval``：两次探活之间的最小间隔，防止密集请求触发风控。

    另有一个**短时结果缓存**：同一轮里同一个商品被查两次没必要打两次网络。
    """

    def __init__(
        self,
        *,
        storage_state: str | None = None,
        max_probes: int = DEFAULT_MAX_PROBES_PER_SCAN,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
    ):
        self.storage_state = storage_state
        self.max_probes = max(0, int(max_probes))
        self.min_interval = max(0.0, float(min_interval))
        self.probes_used = 0
        self._cache: dict[str, ItemStatus] = {}
        self._last_probe_at = 0.0

    @property
    def exhausted(self) -> bool:
        return self.probes_used >= self.max_probes

    async def __call__(self, item_id: str) -> ItemStatus | None:
        """探活入口。配额用尽时返回 ``None``（表示未探活，而非判活）。"""
        if not item_id:
            return None
        if item_id in self._cache:
            return self._cache[item_id]
        if self.exhausted:
            return None

        elapsed = time.monotonic() - self._last_probe_at
        if self._last_probe_at and elapsed < self.min_interval:
            import asyncio

            await asyncio.sleep(self.min_interval - elapsed)

        status = await probe_item_alive(item_id, storage_state=self.storage_state)
        self._last_probe_at = time.monotonic()
        self.probes_used += 1
        self._cache[item_id] = status
        return status
