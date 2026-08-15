"""页签1：批量跑批监控台。

布局（自上而下）：
  任务选择 + 预检结果
  参数区（provider/model/阈值/前N行/三个开关）
  进度区（进度条 + 统计条）
  日志区（占垂直空间大头）

预检：选完文件夹立即读 xlsx 检列布局 + 建标准库。不花钱、不抓网页，
失败当场红字报错——比 CLI（跑起来才暴露）体验好一截。
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from grading_agent.gui.runner import InputRequest, MonitorRunner

BUSY_OWNER = "monitor"
DRAIN_INTERVAL_MS = 100


class MonitorView(ttk.Frame):
    def __init__(self, master, app) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self.runner: MonitorRunner | None = None
        self.task_dir: Path | None = None
        self._start_time: float = 0.0
        self._done_count: int = 0
        self._total: int = 0
        self._draining = False
        self._last_identify_desc: str = ""
        self._last_identify_triple: str = ""

        self._build_ui()
        self.app.add_busy_listener(self._on_busy_change)

    # ── 界面 ──

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)  # 日志区可拉伸

        # ── 任务选择 ──
        top = ttk.LabelFrame(self, text=" 任务 ", padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="任务文件夹:").grid(row=0, column=0, sticky="w")
        self.dir_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.dir_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=6)
        self.browse_btn = ttk.Button(top, text="浏览…", command=self._choose_dir)
        self.browse_btn.grid(row=0, column=2)

        self.precheck_var = tk.StringVar(value="请选择任务文件夹（含 xlsx + 3 个 docx）")
        self.precheck_label = ttk.Label(top, textvariable=self.precheck_var,
                                        foreground="#666", justify="left")
        self.precheck_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ── 参数 ──
        params = ttk.LabelFrame(self, text=" 参数 ", padding=8)
        params.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        params.columnconfigure(0, weight=1)

        cfg = self.app.config_obj

        # 第一行：全部输入框（模型 / 置信度阈值 / 并发数 / 只跑前N行）
        row0 = ttk.Frame(params)
        row0.grid(row=0, column=0, sticky="w")

        ttk.Label(row0, text="模型:").pack(side="left", padx=(0, 4))
        self.provider_var = tk.StringVar(value=cfg.llm.provider)
        ttk.Combobox(row0, textvariable=self.provider_var, width=10,
                     values=["deepseek", "openai", "claude"],
                     state="readonly").pack(side="left")
        self.model_var = tk.StringVar(value=cfg.llm.model)
        ttk.Entry(row0, textvariable=self.model_var, width=16).pack(side="left", padx=(4, 24))

        ttk.Label(row0, text="置信度阈值:").pack(side="left", padx=(0, 4))
        self.threshold_var = tk.StringVar(value=str(cfg.grading.confidence_threshold))
        ttk.Entry(row0, textvariable=self.threshold_var, width=5).pack(side="left", padx=(0, 24))

        ttk.Label(row0, text="并发数:").pack(side="left", padx=(0, 4))
        self.concurrency_var = tk.StringVar(value=str(cfg.grading.concurrency))
        ttk.Entry(row0, textvariable=self.concurrency_var, width=5).pack(side="left", padx=(0, 24))

        ttk.Label(row0, text="只跑前N行:").pack(side="left", padx=(0, 4))
        self.max_rows_var = tk.StringVar()
        ttk.Entry(row0, textvariable=self.max_rows_var, width=6).pack(side="left")

        # 第二行：全部复选框
        row1 = ttk.Frame(params)
        row1.grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.verify_var = tk.BooleanVar(value=False)
        self.skip_id_var = tk.BooleanVar(value=False)
        self.debug_fetch_var = tk.BooleanVar(value=False)
        self.force_rerun_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="验证模式（不写回 xlsx）",
                        variable=self.verify_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row1, text="跳过题目识别",
                        variable=self.skip_id_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row1, text="每次抓取写转储",
                        variable=self.debug_fetch_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row1, text="忽略已完成行（强制重跑）",
                        variable=self.force_rerun_var).pack(side="left")

        # 开始/停止按钮（右侧，垂直居中于两行）
        btns = ttk.Frame(params)
        btns.grid(row=0, column=1, rowspan=2, padx=(24, 0))
        self.start_btn = ttk.Button(btns, text="▶ 开始", command=self._on_start, state="disabled")
        self.start_btn.grid(row=0, column=0, padx=2)
        self.stop_btn = ttk.Button(btns, text="■ 停止", command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=2)

        # ── 进度 ──
        prog = ttk.LabelFrame(self, text=" 进度 ", padding=8)
        prog.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        prog.columnconfigure(1, weight=1)

        self.phase_var = tk.StringVar(value="—")
        ttk.Label(prog, textvariable=self.phase_var, width=14).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=8)
        self.progress_text = tk.StringVar(value="0/0")
        ttk.Label(prog, textvariable=self.progress_text, width=14).grid(row=0, column=2)

        self.stats_var = tk.StringVar(value="已写回 0 │ 待复核 0 │ 抓取失败 0")
        ttk.Label(prog, textvariable=self.stats_var, foreground="#444").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ── 日志 ──
        logf = ttk.LabelFrame(self, text=" 日志 ", padding=6)
        logf.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.log_area = scrolledtext.ScrolledText(logf, wrap="word", height=16,
                                                  font=self.app.log_font)
        self.log_area.grid(row=0, column=0, sticky="nsew")
        self.log_area.configure(state="disabled")

    # ── 任务选择与预检 ──

    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择任务文件夹")
        if not chosen:
            return
        self.task_dir = Path(chosen)
        self.dir_var.set(chosen)
        self._precheck()

    def _precheck(self) -> None:
        """阶段 A 前两步：读 xlsx + 建标准库。不花钱不抓网页。"""
        from grading_agent.standard_library import StandardLibrary, StandardLibraryError, find_task_docxs
        from grading_agent.xlsx_io import read_rows
        from grading_agent.agent.orchestrator import _find_xlsx

        assert self.task_dir is not None
        try:
            xlsx_path = _find_xlsx(self.task_dir)
            standards_map, ref = find_task_docxs(self.task_dir)
        except FileNotFoundError as e:
            self._precheck_fail(str(e))
            return

        try:
            rows, _layout, _wb = read_rows(xlsx_path, skip_score=True)
        except ValueError as e:
            self._precheck_fail(f"xlsx 列定位失败：{e}")
            return

        try:
            stdlib = StandardLibrary(standards_map, ref)
        except StandardLibraryError as e:
            self._precheck_fail(f"标准库构建失败：{e}")
            return

        task_ids = sorted({r.task_id for r in rows})
        gradable = stdlib.gradable_triples()
        self.precheck_label.configure(foreground="#1a7f37")
        self.precheck_var.set(
            f"✓ {xlsx_path.name}：{len(rows)} 行，{len(task_ids)} 个题目ID {task_ids}\n"
            f"✓ 标准库：{len(stdlib.all_triples())} 道参考题，{len(gradable)} 条可评标准"
        )
        self.start_btn.configure(state="normal")
        self._sync_review_tab()

    def _sync_review_tab(self) -> None:
        """让复核页签加载同一个任务的历史结果。

        review_view 在 app 里晚于 monitor_view 构造，故用 getattr 明确处理未就绪的情况，
        而不是靠捕获异常兜底（那样会连真正的加载失败一起吞掉）。
        """
        if self.task_dir is None:
            return
        review = getattr(self.app, "review_view", None)
        if review is None:
            return
        try:
            review.load_task(self.task_dir)
        except Exception as exc:
            self._append_log(f"⚠ 复核页签加载历史结果失败: {type(exc).__name__}: {exc}\n")

    def _precheck_fail(self, msg: str) -> None:
        self.precheck_label.configure(foreground="#c00")
        self.precheck_var.set(f"✗ {msg}")
        self.start_btn.configure(state="disabled")

    # ── 跑批控制 ──

    def _on_start(self) -> None:
        if self.task_dir is None:
            return
        if not self.app.acquire_busy(BUSY_OWNER):
            messagebox.showwarning("忙碌中", "复核页签正在重新评分，请等待完成后再跑批。")
            return

        cfg = self.app.config_obj
        cfg.llm.provider = self.provider_var.get().strip() or cfg.llm.provider
        cfg.llm.model = self.model_var.get().strip() or cfg.llm.model
        try:
            cfg.grading.confidence_threshold = float(self.threshold_var.get())
        except ValueError:
            pass
        try:
            cfg.grading.concurrency = max(1, int(self.concurrency_var.get()))
        except ValueError:
            pass

        max_rows = None
        raw = self.max_rows_var.get().strip()
        if raw:
            try:
                max_rows = int(raw)
            except ValueError:
                messagebox.showerror("参数错误", "「只跑前N行」必须是整数")
                self.app.release_busy(BUSY_OWNER)
                return

        # API key 校验（与 cli.py 一致）
        key_map = {
            "deepseek": cfg.deepseek_api_key,
            "openai": cfg.openai_api_key,
            "claude": cfg.anthropic_api_key,
        }
        if not key_map.get(cfg.llm.provider):
            messagebox.showerror("缺少 API Key",
                                 f"未配置 {cfg.llm.provider} 的 API key，请在 .env 中填写。")
            self.app.release_busy(BUSY_OWNER)
            return

        # 强制重跑会对已评过的行重复调用 LLM，先让用户确认
        if self.force_rerun_var.get() and not self.verify_var.get():
            if not messagebox.askyesno(
                "确认强制重跑",
                "已勾选「忽略已完成行」。\n\n"
                "历史评分记录将被忽略，所有目标行都会重新调用 LLM 评分，"
                "产生额外费用。旧记录不会被删除，界面与验证报告以最新一次为准。\n\n"
                "确定继续？",
            ):
                self.app.release_busy(BUSY_OWNER)
                return

        self._clear_log()
        self._done_count = 0
        self._total = 0
        self._start_time = time.time()
        self.progress.configure(value=0, maximum=100)
        self.progress_text.set("0/0")
        self.stats_var.set("已写回 0 │ 待复核 0 │ 抓取失败 0")

        self.runner = MonitorRunner(
            self.task_dir, cfg,
            max_rows=max_rows,
            debug_fetch=self.debug_fetch_var.get(),
            skip_identification=self.skip_id_var.get(),
            verify=self.verify_var.get(),
            force_rerun=self.force_rerun_var.get(),
            output_root=self.app.output_root,
        )
        self.runner.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.app.set_status("跑批进行中…")
        self._schedule_drain()

    def _on_stop(self) -> None:
        if self.runner is not None:
            self.runner.request_stop()
            self._append_log("\n⚠ 已发送停止请求，等待当前行完成…\n")
            self.stop_btn.configure(state="disabled")

    def shutdown(self) -> None:
        if self.runner is not None and self.runner.running:
            self.runner.request_stop()

    # ── 队列消费 ──

    def _schedule_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self._drain_tick()

    def _drain_tick(self) -> None:
        runner = self.runner
        if runner is None:
            self._draining = False
            return
        runner.drain(self._on_log, self._on_event, self._on_input, self._on_finish)
        if runner.running:
            self.after(DRAIN_INTERVAL_MS, self._drain_tick)
        else:
            # 再排空一次，确保 finish 消息不遗漏
            runner.drain(self._on_log, self._on_event, self._on_input, self._on_finish)
            self._draining = False

    def _on_log(self, msg: str) -> None:
        self._append_log(msg + "\n" if not msg.endswith("\n") else msg)

    def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "phase":
            self.phase_var.set(f"阶段{event.get('phase')} {event.get('label','')}")
        elif etype == "identify_ready":
            # 缓存题目描述 + 最佳候选，供随后的确认弹窗显示（左栏题目、右栏默认选中的参考代码）
            self._last_identify_desc = str(event.get("description") or "")
            self._last_identify_triple = str(event.get("best_triple") or "")
        elif etype == "grading_start":
            self._total = int(event.get("total") or 0)
            self.progress.configure(maximum=max(self._total, 1), value=0)
            self.progress_text.set(f"0/{self._total}")
        elif etype == "row_done":
            self._done_count += 1
            idx = int(event.get("index") or self._done_count)
            total = int(event.get("total") or self._total or 1)
            self.progress.configure(maximum=max(total, 1), value=idx)
            stats = event.get("stats") or {}
            elapsed = time.time() - self._start_time
            eta = ""
            if self._done_count > 0 and idx < total:
                per = elapsed / self._done_count
                remain = per * (total - idx)
                eta = f" │ 剩约 {int(remain // 60)}:{int(remain % 60):02d}"
            self.progress_text.set(f"{idx}/{total}")
            self.stats_var.set(
                f"已写回 {stats.get('high_confidence_written', 0)} │ "
                f"待复核 {stats.get('low_confidence_queued', 0)} │ "
                f"抓取失败 {stats.get('fetch_failed', 0)} │ "
                f"用时 {int(elapsed // 60)}:{int(elapsed % 60):02d}{eta}"
            )
        elif etype == "stopped":
            self.phase_var.set("已停止")

    def _on_input(self, req: InputRequest) -> None:
        """阶段 B 确认：弹窗，用户选完回填唤醒 worker。"""
        from grading_agent.gui.confirm_dialog import ask_confirm

        runner = self.runner
        orch = runner.orchestrator if runner else None
        stdlib = getattr(orch, "stdlib", None) if orch else None
        answer = ask_confirm(self, req.prompt, stdlib=stdlib,
                             body_font=self.app.body_font,
                             code_font=self.app.code_font,
                             description=self._last_identify_desc,
                             best_triple=self._last_identify_triple)
        req.reply(answer)

    def _on_finish(self, report, err: str | None) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.app.release_busy(BUSY_OWNER)
        if err:
            self._append_log(f"\n❌ 跑批异常终止:\n{err}\n")
            self.app.set_status("跑批失败")
            messagebox.showerror("跑批失败", err.splitlines()[0] if err else "未知错误")
        else:
            self.app.set_status("跑批完成")
            self.phase_var.set("完成")
        # 刷新复核页签
        self._sync_review_tab()

    # ── 日志区 ──

    def _append_log(self, text: str) -> None:
        self.log_area.configure(state="normal")
        self.log_area.insert("end", text)
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")

    # ── busy 联动 ──

    def _on_busy_change(self, owner: str | None) -> None:
        """复核页签占用时禁用开始按钮。"""
        if owner is None:
            if self.task_dir is not None:
                self.start_btn.configure(state="normal")
        elif owner != BUSY_OWNER:
            self.start_btn.configure(state="disabled")
