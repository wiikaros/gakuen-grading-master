"""复核页签 worker：单行重新评分。

为什么需要独立线程（不能复用监控台的）：
1. Playwright 同步 API 绑死创建它的线程
2. 监控台跑完批后 UrlFetcher 随 with 块退出、浏览器已关闭

因此复核页签持有自己的常驻线程 + 独立 UrlFetcher，**懒启动**：
首次点「重新评分」才开浏览器，之后复用；页签关闭时才 close。

重评结果只回传给 UI 显示，**不写任何文件**——由用户看过新旧对比后点「采纳」
才落盘（写 human_override.jsonl + xlsx）。这样试错不留垃圾数据。
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from grading_agent.config import Config
from grading_agent.models import GradeResult, FetchResult, TaskTriple
from grading_agent.standard_library import StandardLibrary
from grading_agent.url_fetcher import UrlFetcher
from grading_agent.agent.grader import Grader
from grading_agent.llm.langchain_backend import LangChainBackend, make_llm


class RegradeWorker:
    """常驻线程：接收重评任务，回传 (GradeResult, FetchResult)。

    用法（主线程）：
        w = RegradeWorker(config, stdlib_provider)
        w.start()
        w.submit(row_num=3, url=..., triple=...)
        # 定时调 w.drain(on_result, on_error)
        w.shutdown()
    """

    def __init__(
        self,
        config: Config,
        stdlib_provider: Callable[[], StandardLibrary],
        *,
        render_dumps_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.stdlib_provider = stdlib_provider
        self.render_dumps_dir = render_dumps_dir

        self._task_q: "queue.Queue[dict | None]" = queue.Queue()
        self._result_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()

        # 懒启动的重资源（仅 worker 线程内访问）
        self._fetcher: UrlFetcher | None = None
        self._grader: Grader | None = None

    # ── 主线程调用 ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def busy(self) -> bool:
        """是否有重评任务正在执行。"""
        return self._busy.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, *, row_num: int, url: str, triple: TaskTriple) -> None:
        """提交一个重评任务。"""
        if not self.running:
            self.start()
        self._busy.set()
        self._task_q.put({"row_num": row_num, "url": url, "triple": triple})

    def drain(
        self,
        on_result: Callable[[int, GradeResult, FetchResult], None],
        on_error: Callable[[int, str], None],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """主线程定时调用：消费重评结果。"""
        while True:
            try:
                kind, payload = self._result_q.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "result":
                    row_num, result, fetch_result = payload
                    on_result(row_num, result, fetch_result)
                elif kind == "error":
                    row_num, err = payload
                    on_error(row_num, err)
                elif kind == "status" and on_status is not None:
                    on_status(payload)
            except Exception:
                traceback.print_exc()

    def shutdown(self) -> None:
        """关闭 worker 与浏览器。"""
        if self.running:
            self._task_q.put(None)

    # ── worker 线程内部 ──

    def _worker(self) -> None:
        try:
            while True:
                task = self._task_q.get()
                if task is None:
                    break
                try:
                    self._handle(task)
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                    self._result_q.put(("error", (task.get("row_num", -1), err)))
                finally:
                    self._busy.clear()
        finally:
            self._close_resources()

    def _ensure_resources(self) -> Grader:
        """懒启动浏览器与 LLM。首次重评时才付启动成本。"""
        if self._grader is not None:
            return self._grader

        self._status("正在启动浏览器…")
        self._fetcher = UrlFetcher(
            headless=self.config.playwright.headless,
            timeout_ms=self.config.playwright.timeout_ms,
            debug_dumps_dir=self.render_dumps_dir,
            always_dump=False,
        )
        self._fetcher._init_browser()

        llm = make_llm(
            self.config.llm.provider,
            self.config.llm.model,
            self.config,
            self.config.llm.temperature,
        )
        backend = LangChainBackend(llm, self.config)
        self._grader = Grader(self._fetcher, backend, self.stdlib_provider())
        return self._grader

    def _handle(self, task: dict) -> None:
        row_num = task["row_num"]
        url = task["url"]
        triple = task["triple"]

        grader = self._ensure_resources()
        self._status(f"行{row_num} 重新抓取并评分…")
        result, fetch_result = grader.fetch_and_grade(url, triple, row_id=f"regrade_{row_num}")
        self._result_q.put(("result", (row_num, result, fetch_result)))
        self._status("")

    def _status(self, msg: str) -> None:
        self._result_q.put(("status", msg))

    def _close_resources(self) -> None:
        if self._fetcher is not None:
            try:
                self._fetcher.close()
            except Exception:
                pass
        self._fetcher = None
        self._grader = None
