"""URL 抓取工具：从 onebyone.py 抽取 Playwright 抓取逻辑，去 GUI 依赖。

复用已验证的选择器/iframe 定位逻辑，包装成上下文管理器，返回结构化 FetchResult。

关键复用来源（onebyone.py 行号）：
- CSS_SELECTOR_TEXT/CODE/PLAYWRIGHT_TIMEOUT_MS（第18-20行）
- _locate_target_frame（第346行）
- page.goto wait_until="networkidle"（第310行）
- _normalize_description（第161行）
- 代码后处理 rstrip+strip（第331行）
- _save_render（第387行，失败静默）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from grading_agent.models import FetchResult


# ── 常量（原样来自 onebyone.py 第18-20行）──

CSS_SELECTOR_TEXT = "#view > div > div.app-ide-content > div.style_task-target-panel_1rAvY > div.style_app-ide-console-page_3pXSQ"
CSS_SELECTOR_CODE = "#ace-editor > div.ace_scroller > div > div.ace_layer.ace_text-layer"
PLAYWRIGHT_TIMEOUT_MS = 20000


def _normalize_description(text: str) -> str:
    """题目描述后处理：换行统一、压缩多余空行、`: \n\n`→`: \n`。

    原样来自 onebyone.py 第161行。
    """
    if not text:
        return text
    cleaned = text.replace("\r\n", "\n")
    cleaned = cleaned.replace(": \n\n", ": \n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _locate_target_frame(page):
    """遍历 page.frames，定位含题目内容的 iframe。

    匹配条件（任一）：
    1. iframe URL 含 "match-online-judge-ide"
    2. iframe body.className == "ide-iframe"
    否则 fallback 到 page.main_frame。

    原样来自 onebyone.py 第346行。
    """
    for frame in page.frames:
        url = frame.url or ""
        if "match-online-judge-ide" in url:
            return frame
        try:
            if frame.evaluate("document.body.className") == "ide-iframe":
                return frame
        except Exception:
            # 某些 frame（如 about:blank）无法 evaluate，跳过
            continue
    return page.main_frame


def _save_render(page, row_id: str, debug_dumps_dir: Path,
                 original_html: Optional[str] = None) -> None:
    """写渲染HTML + 原始HTML + 全页截图到 debug_dumps_dir。失败静默。

    文件命名：render{row_id}.html / original{row_id}.html / render{row_id}.png
    原样来自 onebyone.py 第387行。
    """
    try:
        debug_dumps_dir.mkdir(parents=True, exist_ok=True)
        render_path = debug_dumps_dir / f"render{row_id}.html"
        render_path.write_text(page.content(), encoding="utf-8")
        if original_html:
            orig_path = debug_dumps_dir / f"original{row_id}.html"
            orig_path.write_text(original_html, encoding="utf-8")
        screenshot_path = debug_dumps_dir / f"render{row_id}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        # 静默失败：调试用，不影响主流程
        pass


class UrlFetcher:
    """Playwright 抓取器，上下文管理器管理生命周期。

    单 context 复用，每题新建 page 后关闭。headless 模式。
    用法：
        with UrlFetcher(debug_dumps_dir=Path("output/7.12/render_dumps")) as fetcher:
            result = fetcher.fetch(url, row_id="2")
    """

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS,
        debug_dumps_dir: Optional[Path] = None,
        always_dump: bool = False,
    ) -> None:
        """
        Args:
            headless: 无头模式
            timeout_ms: goto 超时（毫秒）
            debug_dumps_dir: 转储目录；None 时不写任何转储
            always_dump: True 时每次抓取都写转储；False 时仅失败时写
        """
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.debug_dumps_dir = debug_dumps_dir
        self.always_dump = always_dump

        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    def __enter__(self) -> "UrlFetcher":
        self._init_browser()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _init_browser(self) -> None:
        """启动 playwright + browser + 单 context。"""
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()

    def close(self) -> None:
        """关闭 context/browser/playwright，失败静默。"""
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        finally:
            self.context = None
            self.browser = None
            self.playwright = None

    def fetch(self, url: str, row_id: str = "") -> FetchResult:
        """抓取单个 URL，返回 FetchResult。

        流程：new_page → goto(networkidle) → 定位 iframe → query_selector → 提取文本 → 后处理。
        超时重试1次（延至 30s）。失败时若 debug_dumps_dir 非空则写转储。
        """
        if not self.context:
            self._init_browser()
        if not self.context:
            return FetchResult(
                description="", code="", raw_html=None, ok=False,
                error="浏览器未初始化", row_id=row_id,
            )

        page = None
        last_error: Optional[str] = None
        # 重试1次：第一次用默认超时，第二次延至 30s
        for attempt, timeout in enumerate([self.timeout_ms, 30000], start=1):
            try:
                page = self.context.new_page()
                res = page.goto(url, wait_until="networkidle", timeout=timeout)

                # 原始响应文本仅 --debug-fetch 转储时才需要（传给 _save_render 的 original_html）。
                # 默认模式下不下载，避免每行白拉几十~几百 KB 的响应体。
                response_text: Optional[str] = None
                if res is not None and self.debug_dumps_dir and self.always_dump:
                    try:
                        response_text = res.text()
                    except Exception:
                        try:
                            response_text = res.body().decode("utf-8", errors="ignore")
                        except Exception:
                            response_text = None

                target_frame = _locate_target_frame(page)
                text_el = target_frame.query_selector(CSS_SELECTOR_TEXT) if target_frame else None
                code_el = target_frame.query_selector(CSS_SELECTOR_CODE) if target_frame else None

                if text_el is None:
                    raise RuntimeError("未找到题目描述元素(iframe渲染后)")
                if code_el is None:
                    raise RuntimeError("未找到代码元素(iframe渲染后)")

                text_part = _normalize_description((text_el.inner_text() or "").strip())
                code_raw = code_el.inner_text() or ""
                lines = [line.rstrip() for line in code_raw.splitlines()]
                code_part = "\n".join(lines).strip()

                # 成功：按配置决定是否写转储
                if self.debug_dumps_dir and self.always_dump:
                    _save_render(page, row_id, self.debug_dumps_dir, original_html=response_text)

                try:
                    page.close()
                except Exception:
                    pass

                return FetchResult(
                    description=text_part,
                    code=code_part,
                    raw_html=response_text,
                    ok=True,
                    error=None,
                    row_id=row_id,
                )

            except PlaywrightTimeoutError as exc:
                last_error = f"页面加载超时: {exc}"
                # 关闭本次 page，准备重试
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                page = None
                if attempt == 1:
                    continue  # 重试一次
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                page = None
                break

        # 失败：写转储（如有配置）
        if self.debug_dumps_dir and page:
            _save_render(page, row_id, self.debug_dumps_dir)
            try:
                page.close()
            except Exception:
                pass

        return FetchResult(
            description="", code="", raw_html=None, ok=False,
            error=last_error or "未知抓取失败", row_id=row_id,
        )
