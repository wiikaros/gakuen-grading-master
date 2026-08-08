"""监控台 worker：把 Orchestrator.run() 放进后台线程，与 Tk 主线程通信。

线程模型（关键）：
- Playwright 同步 API 绑死创建它的线程 → Orchestrator.run() 必须整个跑在同一个 worker 线程内
- worker → 主线程：print_fn / event_fn 往 queue 塞消息，主线程 after(100ms) 轮询消费
- 主线程 → worker：input_fn 用 threading.Event 握手（worker 阻塞等待，主线程弹窗后回填）

为什么 input_fn 要握手而不是直接调 GUI：
Orchestrator 在阶段 B 需要用户确认题目，这是同步阻塞调用。若 worker 直接操作 Tk 控件
会破坏 Tk 的单线程约束；若主线程直接跑 Orchestrator 则弹窗时界面死锁。
握手把「阻塞」留在 worker、把「画界面」留在主线程，两边各司其职。
"""
from __future__ import annotations

import datetime as _dt
import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from grading_agent.config import Config
from grading_agent.agent.orchestrator import Orchestrator


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class InputRequest:
    """一次「向用户提问」的请求。worker 创建后阻塞等待主线程回填 answer。"""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.answer: str = ""
        self._done = threading.Event()

    def reply(self, answer: str) -> None:
        """主线程调用：回填答案并唤醒 worker。"""
        self.answer = answer
        self._done.set()

    def wait(self, timeout: float | None = None) -> str:
        """worker 调用：阻塞直到主线程回填。"""
        self._done.wait(timeout)
        return self.answer

    @property
    def answered(self) -> bool:
        return self._done.is_set()


class MonitorRunner:
    """跑批 worker 的生命周期管理。

    用法（主线程）：
        runner = MonitorRunner(task_dir, config, ...)
        runner.start()
        # 在 Tk 里定时调 runner.drain(on_log, on_event, on_input, on_finish)
        runner.request_stop()
    """

    def __init__(
        self,
        task_dir: Path,
        config: Config,
        *,
        max_rows: int | None = None,
        debug_fetch: bool = False,
        skip_identification: bool = False,
        verify: bool = False,
        output_root: Path | None = None,
    ) -> None:
        self.task_dir = task_dir
        self.config = config
        self.max_rows = max_rows
        self.debug_fetch = debug_fetch
        self.skip_identification = skip_identification
        self.verify = verify
        self.output_root = output_root

        self._msg_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_input: InputRequest | None = None
        self.orchestrator: Orchestrator | None = None
        self.report: Any = None
        self.error: str | None = None
        # run.log 落盘（worker 线程内独占）
        self._log_fp = None
        self.log_path: Path | None = None

    # ── 主线程调用 ──

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动 worker 线程。"""
        if self.running:
            raise RuntimeError("跑批已在进行中")
        self._stop.clear()
        self.report = None
        self.error = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """请求停止。Orchestrator 在阶段 C 循环首部检查该信号。

        若正卡在阶段 B 的确认弹窗上，同时回一个 'n' 让它跳过，避免 worker 永久阻塞。
        """
        self._stop.set()
        pending = self._pending_input
        if pending is not None and not pending.answered:
            pending.reply("n")

    def drain(
        self,
        on_log: Callable[[str], None],
        on_event: Callable[[dict], None],
        on_input: Callable[[InputRequest], None],
        on_finish: Callable[[Any, str | None], None],
    ) -> None:
        """主线程定时调用：消费队列里积压的消息。

        每次把队列排空，避免消息堆积导致界面滞后。
        """
        while True:
            try:
                kind, payload = self._msg_q.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "log":
                    on_log(payload)
                elif kind == "event":
                    on_event(payload)
                elif kind == "input":
                    self._pending_input = payload
                    on_input(payload)
                elif kind == "finish":
                    report, err = payload
                    self._pending_input = None
                    on_finish(report, err)
            except Exception:
                # 单条消息的 UI 处理异常不应中断整个 drain 循环
                traceback.print_exc()

    # ── worker 线程内部 ──

    def _worker(self) -> None:
        log_fp = None
        try:
            self.orchestrator = Orchestrator(
                task_dir=self.task_dir,
                config=self.config,
                max_rows=self.max_rows,
                debug_fetch=self.debug_fetch,
                skip_identification=self.skip_identification,
                verify=self.verify,
                input_fn=self._input_fn,
                print_fn=self._print_fn,
                event_fn=self._event_fn,
                should_stop=self._stop,
                output_root=self.output_root,
            )
            # 落盘 run.log：GUI 日志区只在内存里，跑完关窗就没了。
            # 以追加方式写，每次跑批前加一条分隔标头，便于回溯历次运行。
            try:
                self.log_path = self.orchestrator.output_dir / "run.log"
                log_fp = open(self.log_path, "a", encoding="utf-8")
                log_fp.write(f"\n{'=' * 60}\n[{_now()}] 开始跑批 "
                             f"task={self.task_dir.name} "
                             f"model={self.config.llm.provider}/{self.config.llm.model} "
                             f"max_rows={self.max_rows} verify={self.verify}\n")
                log_fp.flush()
                self._log_fp = log_fp
            except Exception:
                self._log_fp = None

            report = self.orchestrator.run()
            self.report = report
            self._msg_q.put(("finish", (report, None)))
        except Exception as exc:  # 任何异常都要送回主线程，不能让 worker 静默死掉
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self.error = err
            if self._log_fp is not None:
                try:
                    self._log_fp.write(f"[{_now()}] 异常终止: {err}\n")
                except Exception:
                    pass
            self._msg_q.put(("finish", (None, err)))
        finally:
            self._log_fp = None
            if log_fp is not None:
                try:
                    log_fp.write(f"[{_now()}] 跑批结束\n")
                    log_fp.close()
                except Exception:
                    pass

    def _print_fn(self, msg: str = "") -> None:
        text = str(msg)
        self._msg_q.put(("log", text))
        # 同步落盘。worker 线程内独占该句柄，无需加锁。
        fp = self._log_fp
        if fp is not None:
            try:
                fp.write(text + "\n")
                fp.flush()
            except Exception:
                pass

    def _event_fn(self, event: dict) -> None:
        self._msg_q.put(("event", event))

    def _input_fn(self, prompt: str = "") -> str:
        """worker 内阻塞等待主线程回填。

        停止已被请求时直接返回 'n'，不再弹窗（让 Orchestrator 尽快收尾）。
        """
        if self._stop.is_set():
            return "n"
        req = InputRequest(str(prompt))
        self._msg_q.put(("input", req))
        return req.wait()
