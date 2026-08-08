"""页签2：结果复核（左右分割）。

左侧 6 列窄表格（行号/答卷ID尾6位/AI分/人工分/偏差/状态），右侧详情自上而下：
题目描述 → 学生代码(高亮) → 评分明细(维度条形图) → 改分与重评操作区。

左右布局的选择理由：学生代码能显示约 25 行、表格能显示约 30 行（上下分割各只有一半），
而复核时视线主要在详情上、且常需在一批相似问题里找模式。

数据来源三方合并（见 override_io.build_row_views）：
grading_detail.jsonl（AI 原始输出）+ human_override.jsonl（人工改动）+ xlsx（人工原始评分）
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from grading_agent.gui.highlight import configure_cpp_tags, highlight_cpp, set_plain_text
from grading_agent.gui.override_io import (
    OVERRIDE_FILENAME,
    RowView,
    append_override,
    build_row_views,
)

BUSY_OWNER = "review"
DRAIN_INTERVAL_MS = 150

FILTER_ALL = "全部"
FILTER_PENDING = "待复核"
FILTER_FAILED = "抓取/解析失败"
FILTER_DIFF3 = "偏差≥3"
FILTER_OVERRIDDEN = "已人工改分"


class ReviewView(ttk.Frame):
    def __init__(self, master, app) -> None:
        super().__init__(master, padding=8)
        self.app = app
        self.task_dir: Path | None = None
        self.output_dir: Path | None = None
        self.rows: list[RowView] = []
        self.visible: list[RowView] = []
        self.current: RowView | None = None
        self.regrade_worker = None
        self._pending_regrade: dict | None = None  # 待采纳的重评结果
        self._draining = False
        self._stdlib = None
        self._url_by_row: dict[int, str] = {}

        self._build_ui()
        self.app.add_busy_listener(self._on_busy_change)

    # ── 界面 ──

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # 顶部工具条
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.task_var = tk.StringVar(value="未加载任务")
        ttk.Label(bar, textvariable=self.task_var).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left", padx=(10, 4))

        ttk.Label(bar, text="筛选:").pack(side="left", padx=(12, 2))
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        fbox = ttk.Combobox(bar, textvariable=self.filter_var, width=14, state="readonly",
                            values=[FILTER_ALL, FILTER_PENDING, FILTER_FAILED,
                                    FILTER_DIFF3, FILTER_OVERRIDDEN])
        fbox.pack(side="left")
        fbox.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        ttk.Label(bar, text="题目ID:").pack(side="left", padx=(10, 2))
        self.task_id_var = tk.StringVar(value=FILTER_ALL)
        self.task_id_box = ttk.Combobox(bar, textvariable=self.task_id_var, width=10,
                                        state="readonly", values=[FILTER_ALL])
        self.task_id_box.pack(side="left")
        self.task_id_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        ttk.Label(bar, text="搜索答卷ID:").pack(side="left", padx=(10, 2))
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=16)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self._apply_filter())

        self.summary_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.summary_var, foreground="#444").pack(side="right")

        # 左右分割
        self.splitter = ttk.PanedWindow(self, orient="horizontal")
        self.splitter.grid(row=1, column=0, sticky="nsew")

        self._build_table(self.splitter)
        self._build_detail(self.splitter)

    def _build_table(self, parent) -> None:
        wrap = ttk.Frame(parent)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        parent.add(wrap, weight=0)

        cols = ("row", "answer", "ai", "human", "diff", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        headings = {
            "row": ("行", 46), "answer": ("答卷ID", 92), "ai": ("AI", 42),
            "human": ("人工", 46), "diff": ("Δ", 42), "status": ("状态", 62),
        }
        for c in cols:
            text, width = headings[c]
            self.tree.heading(c, text=text, command=lambda c=c: self._sort_by(c))
            anchor = "w" if c == "answer" else "center"
            self.tree.column(c, width=width, anchor=anchor, stretch=(c == "answer"))
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        # 行着色：黄=待复核，红=失败，橙=偏差大，蓝=已人工改分
        self.tree.tag_configure("pending", background="#FFF8DC")
        self.tree.tag_configure("failed", background="#FFE4E1")
        self.tree.tag_configure("bigdiff", background="#FFEBCD")
        self.tree.tag_configure("overridden", background="#E6F3FF")

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _build_detail(self, parent) -> None:
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)   # 描述
        outer.rowconfigure(2, weight=2)   # 代码占更多
        outer.rowconfigure(3, weight=2)   # 明细
        parent.add(outer, weight=1)

        # 标题栏：行号/答卷ID/三元组 + 打开网页
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.head_var = tk.StringVar(value="（左侧选择一行查看详情）")
        ttk.Label(head, textvariable=self.head_var, font=self.app.body_font).pack(side="left")
        self.open_btn = ttk.Button(head, text="🌐 打开网页", command=self._open_url, state="disabled")
        self.open_btn.pack(side="right")
        self.override_flag = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.override_flag,
                  foreground="#0b6bcb").pack(side="right", padx=8)

        # 题目描述
        df = ttk.LabelFrame(outer, text=" 题目描述 ", padding=4)
        df.grid(row=1, column=0, sticky="nsew")
        df.rowconfigure(0, weight=1)
        df.columnconfigure(0, weight=1)
        self.desc_area = tk.Text(df, wrap="word", height=5, font=self.app.body_font)
        self.desc_area.grid(row=0, column=0, sticky="nsew")
        dsb = ttk.Scrollbar(df, orient="vertical", command=self.desc_area.yview)
        dsb.grid(row=0, column=1, sticky="ns")
        self.desc_area.configure(yscrollcommand=dsb.set, state="disabled")

        # 学生代码
        code_title = " 学生代码 (C++) "
        if not getattr(self.app, "code_font_is_cjk_safe", True):
            # 当前字体不含中文字形，中文由 Tk 回退渲染：能正常显示、英文与缩进也正常，
            # 只是中文宽度未必恰为英文两倍。装「更纱黑体 Sarasa Mono SC」可完美对齐。
            code_title = " 学生代码 (C++) — 中文用系统回退字体显示 "
        cf = ttk.LabelFrame(outer, text=code_title, padding=4)
        cf.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)
        self.code_area = tk.Text(cf, wrap="none", height=12, font=self.app.code_font)
        self.code_area.grid(row=0, column=0, sticky="nsew")
        cvsb = ttk.Scrollbar(cf, orient="vertical", command=self.code_area.yview)
        cvsb.grid(row=0, column=1, sticky="ns")
        chsb = ttk.Scrollbar(cf, orient="horizontal", command=self.code_area.xview)
        chsb.grid(row=1, column=0, sticky="ew")
        self.code_area.configure(yscrollcommand=cvsb.set, xscrollcommand=chsb.set,
                                 state="disabled")
        configure_cpp_tags(self.code_area)

        # 评分明细
        gf = ttk.LabelFrame(outer, text=" 评分明细 ", padding=4)
        gf.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        gf.rowconfigure(0, weight=1)
        gf.columnconfigure(0, weight=1)
        self.detail_area = tk.Text(gf, wrap="word", height=10, font=self.app.detail_font,
                                   spacing1=1, spacing3=2)
        self.detail_area.grid(row=0, column=0, sticky="nsew")
        gsb = ttk.Scrollbar(gf, orient="vertical", command=self.detail_area.yview)
        gsb.grid(row=0, column=1, sticky="ns")
        # 用制表位按像素定位列，而非空格填充——中文是双宽字符，
        # 按字符数填充（f"{name:<18}"）在中英混排时必然错位。
        em = self.app.detail_font.measure("中")
        self.detail_area.configure(
            yscrollcommand=gsb.set, state="disabled",
            tabs=(em * 9, em * 12, em * 14),
        )
        self.detail_area.tag_configure("full", foreground="#1a7f37")
        self.detail_area.tag_configure("zero", foreground="#c00")
        self.detail_area.tag_configure("partial", foreground="#a60")
        self.detail_area.tag_configure("head", foreground="#0b6bcb")
        self.detail_area.tag_configure("muted", foreground="#666")
        self.detail_area.tag_configure("bar", font=self.app.code_font)

        self._build_actions(outer)

    def _build_actions(self, outer) -> None:
        act = ttk.LabelFrame(outer, text=" 改分 / 重评 ", padding=6)
        act.grid(row=4, column=0, sticky="ew", pady=(6, 0))

        self.score_line = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.score_line,
                  font=self.app.body_font).grid(row=0, column=0, columnspan=8, sticky="w")

        ttk.Label(act, text="改为:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.new_score_var = tk.StringVar()
        self.score_entry = ttk.Entry(act, textvariable=self.new_score_var, width=6)
        self.score_entry.grid(row=1, column=1, padx=(4, 8), pady=(6, 0))

        ttk.Label(act, text="备注:").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.note_var = tk.StringVar()
        ttk.Entry(act, textvariable=self.note_var, width=34).grid(
            row=1, column=3, padx=(4, 8), pady=(6, 0))

        self.save_btn = ttk.Button(act, text="保存改分", command=self._on_save, state="disabled")
        self.save_btn.grid(row=1, column=4, padx=2, pady=(6, 0))
        self.undo_btn = ttk.Button(act, text="撤销改分", command=self._on_undo, state="disabled")
        self.undo_btn.grid(row=1, column=5, padx=2, pady=(6, 0))
        self.regrade_btn = ttk.Button(act, text="🔄 重新评分", command=self._on_regrade,
                                      state="disabled")
        self.regrade_btn.grid(row=1, column=6, padx=(12, 2), pady=(6, 0))
        self.adopt_btn = ttk.Button(act, text="采纳重评结果", command=self._on_adopt,
                                    state="disabled")
        self.adopt_btn.grid(row=1, column=7, padx=2, pady=(6, 0))

        self.regrade_status = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.regrade_status,
                  foreground="#0b6bcb").grid(row=2, column=0, columnspan=8,
                                             sticky="w", pady=(6, 0))

    # ── 数据加载 ──

    def load_task(self, task_dir: Path) -> None:
        """切换任务并加载历史结果。监控台选完文件夹/跑完批后调用。"""
        self.task_dir = task_dir
        self.output_dir = self.app.output_root / task_dir.name
        self.task_var.set(f"任务: {task_dir.name}")
        self._stdlib = None
        self.reload()

    def reload(self) -> None:
        """重新读三方数据并刷新表格。纯读文件，不花钱。"""
        if self.task_dir is None or self.output_dir is None:
            return

        detail_path = self.output_dir / self.app.config_obj.output.detail_log
        override_path = self.output_dir / OVERRIDE_FILENAME

        human_scores: dict = {}
        self._url_by_row = {}
        try:
            from grading_agent.agent.orchestrator import _find_xlsx
            from grading_agent.xlsx_io import read_rows, read_scores_by_key

            xlsx_path = _find_xlsx(self.task_dir)
            human_scores = read_scores_by_key(xlsx_path)
            rows, _layout, _wb = read_rows(xlsx_path, skip_score=True)
            self._url_by_row = {r.row_num: r.url for r in rows}
        except Exception:
            # xlsx 不可读时仍展示 AI 结果（人工分列显示 —）
            pass

        self.rows = build_row_views(detail_path, override_path, human_scores, self._url_by_row)

        task_ids = sorted({r.task_id for r in self.rows if r.task_id})
        self.task_id_box.configure(values=[FILTER_ALL] + task_ids)
        if self.task_id_var.get() not in ([FILTER_ALL] + task_ids):
            self.task_id_var.set(FILTER_ALL)

        self._apply_filter()

    # ── 筛选与表格 ──

    def _matches(self, r: RowView) -> bool:
        threshold = self.app.config_obj.grading.confidence_threshold
        f = self.filter_var.get()
        if f == FILTER_PENDING and r.status(threshold) != "待复核":
            return False
        if f == FILTER_FAILED and r.parse_ok:
            return False
        if f == FILTER_DIFF3:
            d = r.diff
            if d is None or abs(d) < 3:
                return False
        if f == FILTER_OVERRIDDEN and not r.is_overridden:
            return False

        tid = self.task_id_var.get()
        if tid != FILTER_ALL and r.task_id != tid:
            return False

        kw = self.search_var.get().strip()
        if kw and kw not in r.answer_id:
            return False
        return True

    def _apply_filter(self) -> None:
        threshold = self.app.config_obj.grading.confidence_threshold
        self.visible = [r for r in self.rows if self._matches(r)]

        self.tree.delete(*self.tree.get_children())
        for r in self.visible:
            eff = r.effective_score
            d = r.diff
            tags = []
            st = r.status(threshold)
            if not r.parse_ok:
                tags.append("failed")
            elif st == "待复核":
                tags.append("pending")
            elif d is not None and abs(d) >= 3:
                tags.append("bigdiff")
            if r.is_overridden:
                tags.append("overridden")

            self.tree.insert("", "end", iid=str(r.row_num), tags=tuple(tags), values=(
                r.row_num,
                r.answer_id[-6:] if r.answer_id else "—",
                "—" if eff is None else eff,
                "—" if r.human_score is None else r.human_score,
                "—" if d is None else f"{d:+d}",
                ("✎" if r.is_overridden else "") + st,
            ))

        pending = sum(1 for r in self.rows if r.status(threshold) == "待复核")
        failed = sum(1 for r in self.rows if not r.parse_ok)
        changed = sum(1 for r in self.rows if r.is_overridden)
        self.summary_var.set(
            f"共 {len(self.rows)} 行 │ 显示 {len(self.visible)} │ "
            f"待复核 {pending} │ 失败 {failed} │ 已改分 {changed}"
        )

    def _sort_by(self, col: str) -> None:
        """点表头排序。偏差列按绝对值降序（最该看的排最前）。"""
        keymap = {
            "row": lambda r: r.row_num,
            "answer": lambda r: r.answer_id,
            "ai": lambda r: (r.effective_score is None, r.effective_score or 0),
            "human": lambda r: (r.human_score is None, r.human_score or 0),
            "diff": lambda r: (r.diff is None, -abs(r.diff or 0)),
            "status": lambda r: r.status(self.app.config_obj.grading.confidence_threshold),
        }
        key = keymap.get(col)
        if key is None:
            return
        self.rows.sort(key=key)
        self._apply_filter()

    # ── 详情渲染 ──

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        row_num = int(sel[0])
        row = next((r for r in self.rows if r.row_num == row_num), None)
        if row is None:
            return
        self.current = row
        self._pending_regrade = None
        self.adopt_btn.configure(state="disabled")
        self.regrade_status.set("")
        self._render_detail(row)

    def _render_detail(self, r: RowView) -> None:
        self.head_var.set(f"行{r.row_num} · 答卷ID {r.answer_id or '—'} · "
                          f"题目ID {r.task_id or '—'} · {r.triple or '—'}")
        self.override_flag.set(
            f"✎ 已人工改分 {r.override_score}（{r.override_time}）" if r.is_overridden else "")
        self.open_btn.configure(state="normal" if r.url else "disabled")

        # 题目描述与学生代码：新版 jsonl 已存，旧记录才需重抓
        if r.has_content:
            set_plain_text(self.desc_area, r.description or "(空)")
            highlight_cpp(self.code_area, r.student_code or "")
        else:
            set_plain_text(self.desc_area,
                           "（这条是旧记录，未存题目描述；点「🔄 重新评分」可重新抓取）")
            set_plain_text(self.code_area,
                           "（这条是旧记录，未存学生代码；点「🔄 重新评分」可重新抓取）")

        self._render_scores(r)

        eff = r.effective_score
        parts = [f"AI {r.ai_score if r.ai_score is not None else '—'}"]
        if r.is_overridden:
            parts.append(f"人工改为 {r.override_score}")
        parts.append(f"人工原始 {r.human_score if r.human_score is not None else '—'}")
        if r.diff is not None:
            parts.append(f"偏差 {r.diff:+d}")
        if r.confidence is not None:
            parts.append(f"置信度 {r.confidence:.2f}")
        self.score_line.set(" │ ".join(parts))

        self.new_score_var.set("" if eff is None else str(eff))
        self.note_var.set(r.override_note if r.is_overridden else "")
        self.save_btn.configure(state="normal")
        self.undo_btn.configure(state="normal" if r.is_overridden else "disabled")
        self.regrade_btn.configure(
            state="normal" if (r.url and not self.app.is_busy(BUSY_OWNER)) else "disabled")

    def _render_scores(self, r: RowView, new_result=None) -> None:
        """维度明细。new_result 非空时并排显示「原→新」对比。

        列对齐用 Text 的 tabs（像素定位），不用空格填充：
        维度名多为中文（双宽字符），按字符数填充会错位。
        """
        area = self.detail_area
        area.configure(state="normal")
        area.delete("1.0", "end")

        if not r.dimension_scores and new_result is None:
            area.insert("end", r.ai_reasoning or "（无评分明细）")
            area.configure(state="disabled")
            return

        def bar(awarded: int, maxs: int) -> str:
            maxs = max(maxs, 1)
            filled = int(round(awarded / maxs * 5))
            filled = max(0, min(5, filled))
            return "█" * filled + "░" * (5 - filled)

        def tag_for(awarded: int, maxs: int) -> str:
            if awarded >= maxs:
                return "full"
            return "zero" if awarded <= 0 else "partial"

        new_dims = None
        if new_result is not None:
            new_dims = {d.name: d for d in new_result.dimension_scores}
            area.insert("end", "维度\t原评分\t\t新评分\n", "head")

        for d in r.dimension_scores:
            name = str(d.get("name", ""))
            aw = int(d.get("awarded") or 0)
            mx = int(d.get("max_score") or 5)

            # 维度名过长则截断，保证制表位不被撑开
            area.insert("end", name if len(name) <= 9 else name[:8] + "…")
            area.insert("end", "\t")
            area.insert("end", bar(aw, mx), ("bar", tag_for(aw, mx)))
            area.insert("end", f" {aw}/{mx}", tag_for(aw, mx))

            if new_dims is not None:
                nd = new_dims.get(name)
                if nd is not None:
                    area.insert("end", "\t→\t")
                    area.insert("end", bar(nd.awarded, nd.max_score),
                                ("bar", tag_for(nd.awarded, nd.max_score)))
                    area.insert("end", f" {nd.awarded}/{nd.max_score}",
                                tag_for(nd.awarded, nd.max_score))
                area.insert("end", "\n")
            else:
                area.insert("end", "\n")
                reason = str(d.get("reasoning", "")).strip()
                if reason:
                    # 理由单独成行并缩进，长中文不再被截断
                    area.insert("end", f"\t{reason}\n", "muted")

        cats = r.error_categories
        area.insert("end", f"\n语法扣分：{('、'.join(cats)) if cats else '无'}"
                           f"  (-{r.total_deduction})\n")
        area.insert("end", f"小计 {r.subtotal} → 总分 {r.ai_score}\n")
        if r.ai_reasoning:
            area.insert("end", f"\n整体理由：{r.ai_reasoning}\n", "muted")

        if new_result is not None:
            area.insert("end", "\n── 重评结果 ──\n", "head")
            new_cats = new_result.syntax_penalty.error_categories
            area.insert("end", f"语法扣分：{('、'.join(new_cats)) if new_cats else '无'}"
                               f"  (-{new_result.syntax_penalty.total_deduction})\n")
            area.insert("end", f"小计 {new_result.subtotal} → "
                               f"总分 {new_result.total_score}"
                               f"  (置信度 {new_result.confidence:.2f})\n")
            if new_result.reasoning:
                area.insert("end", f"理由：{new_result.reasoning}\n", "muted")

        area.configure(state="disabled")

    def _open_url(self) -> None:
        if self.current and self.current.url:
            webbrowser.open(self.current.url)

    # ── 改分写入 ──

    def _write_xlsx_score(self, row_num: int, score: int) -> Path | None:
        """把分数写进 **AI 评分副本** 的评分列，原始 xlsx 只读。

        副本位于 output/<任务>/<原名>_AI评分.xlsx，不存在时从原始表复制。
        绝不写 tasks/ 下的原始表——那里存着真人评分，既是交付基准，
        也是 read_scores_by_key() 读取的一致性对比来源（写了会让偏差恒为 0）。
        """
        from grading_agent.agent.orchestrator import _find_xlsx
        from grading_agent.ai_xlsx import ensure_ai_xlsx
        from grading_agent.xlsx_io import read_rows, save_workbook, update_cell_in_memory

        assert self.task_dir is not None and self.output_dir is not None
        source = _find_xlsx(self.task_dir)
        target = ensure_ai_xlsx(source, self.output_dir)
        _rows, layout, wb = read_rows(target, skip_score=True)
        update_cell_in_memory(wb.active, row_num, layout.score_col_idx, score)
        return save_workbook(wb, target)

    def _commit_override(self, r: RowView, new_score: int, note: str) -> None:
        """写 human_override.jsonl + xlsx。grading_detail.jsonl 绝不改动。"""
        assert self.output_dir is not None
        override_path = self.output_dir / OVERRIDE_FILENAME
        old = r.effective_score
        source = "human" if r.is_overridden else "ai"

        try:
            saved_to = self._write_xlsx_score(r.row_num, new_score)
        except Exception as exc:
            messagebox.showerror("写入 xlsx 失败", f"{type(exc).__name__}: {exc}")
            return

        append_override(
            override_path,
            row_num=r.row_num, answer_id=r.answer_id, task_id=r.task_id,
            old_score=old, new_score=new_score, source=source, note=note,
        )

        tip = f"行{r.row_num} 已改为 {new_score} 分"
        if saved_to is not None and saved_to.name.endswith("_autosave.xlsx"):
            tip += f"（原文件被占用，已另存为 {saved_to.name}）"
        self.app.set_status(tip)

        self.reload()
        self._reselect(r.row_num)

    def _on_save(self) -> None:
        r = self.current
        if r is None:
            return
        raw = self.new_score_var.get().strip()
        try:
            new_score = int(raw)
        except ValueError:
            messagebox.showerror("输入错误", "分数必须是整数")
            return
        max_score = self.app.config_obj.grading.max_score
        if not (0 <= new_score <= max_score):
            if not messagebox.askyesno(
                    "分数超范围",
                    f"{new_score} 不在 0-{max_score} 范围内，仍要写入吗？"):
                return
        if new_score == r.effective_score:
            self.app.set_status("分数未变化，未写入")
            return
        self._commit_override(r, new_score, self.note_var.get().strip())

    def _on_undo(self) -> None:
        """撤销 = 追加一条反向记录（回到 AI 原始分），历史完整保留。"""
        r = self.current
        if r is None or not r.is_overridden:
            return
        if r.ai_score is None:
            messagebox.showwarning("无法撤销", "该行没有 AI 原始评分可回退。")
            return
        if not messagebox.askyesno("撤销改分",
                                   f"将行{r.row_num} 恢复为 AI 原始分 {r.ai_score}？"):
            return
        assert self.output_dir is not None
        try:
            saved_to = self._write_xlsx_score(r.row_num, r.ai_score)
        except Exception as exc:
            messagebox.showerror("写入 xlsx 失败", f"{type(exc).__name__}: {exc}")
            return
        append_override(
            self.output_dir / OVERRIDE_FILENAME,
            row_num=r.row_num, answer_id=r.answer_id, task_id=r.task_id,
            old_score=r.override_score, new_score=r.ai_score,
            source="human", note="撤销人工改分，恢复 AI 原始分",
        )
        self.app.set_status(f"行{r.row_num} 已恢复为 AI 原始分 {r.ai_score}")
        self.reload()
        self._reselect(r.row_num)

    def _reselect(self, row_num: int) -> None:
        iid = str(row_num)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    # ── 单行重评 ──

    def _get_stdlib(self):
        """懒建标准库（重评要用）。"""
        if self._stdlib is not None:
            return self._stdlib
        from grading_agent.agent.orchestrator import _find_docx
        from grading_agent.standard_library import StandardLibrary

        assert self.task_dir is not None
        self._stdlib = StandardLibrary(
            _find_docx(self.task_dir, "一级"),
            _find_docx(self.task_dir, "二级"),
            _find_docx(self.task_dir, "参考代码"),
        )
        return self._stdlib

    def _on_regrade(self) -> None:
        r = self.current
        if r is None or not r.url:
            return
        if not self.app.acquire_busy(BUSY_OWNER):
            messagebox.showwarning("忙碌中", "监控台正在跑批，请等待完成后再重评。")
            return

        from grading_agent.gui.regrade_worker import RegradeWorker

        triple = self._parse_triple(r.triple)
        if triple is None:
            messagebox.showerror("无法重评", f"该行三元组无法解析：{r.triple!r}")
            self.app.release_busy(BUSY_OWNER)
            return

        if self.regrade_worker is None:
            assert self.output_dir is not None
            self.regrade_worker = RegradeWorker(
                self.app.config_obj, self._get_stdlib,
                render_dumps_dir=self.output_dir / self.app.config_obj.output.render_dumps_subdir,
            )
            self.regrade_worker.start()

        self.regrade_btn.configure(state="disabled")
        self.regrade_status.set("正在重新抓取并评分…")
        self.regrade_worker.submit(row_num=r.row_num, url=r.url, triple=triple)
        self._schedule_drain()

    @staticmethod
    def _parse_triple(s: str):
        from grading_agent.agent.orchestrator import _parse_triple_str
        return _parse_triple_str(s or "")

    def _schedule_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self._drain_tick()

    def _drain_tick(self) -> None:
        w = self.regrade_worker
        if w is None:
            self._draining = False
            return
        w.drain(self._on_regrade_result, self._on_regrade_error, self._on_regrade_status)
        if w.busy:
            self.after(DRAIN_INTERVAL_MS, self._drain_tick)
        else:
            w.drain(self._on_regrade_result, self._on_regrade_error, self._on_regrade_status)
            self._draining = False

    def _on_regrade_status(self, msg: str) -> None:
        if msg:
            self.regrade_status.set(msg)

    def _on_regrade_result(self, row_num: int, result, fetch_result) -> None:
        """重评完成：只显示，不写文件。用户点「采纳」才落盘。"""
        self.app.release_busy(BUSY_OWNER)
        self.regrade_btn.configure(state="normal")

        r = self.current
        if r is None or r.row_num != row_num:
            self.regrade_status.set(f"行{row_num} 重评完成（已切换选中行，未显示）")
            return

        # 抓到的描述与代码：旧记录借此补上内容，新记录则刷新为最新抓取结果
        r.description = fetch_result.description or r.description
        r.student_code = fetch_result.code or r.student_code
        set_plain_text(self.desc_area, r.description or "(空)")
        highlight_cpp(self.code_area, r.student_code or "")

        if not result.parse_ok:
            self.regrade_status.set(f"⚠ 重评失败：{result.reasoning}")
            return

        self._pending_regrade = {"row_num": row_num, "result": result}
        self._render_scores(r, new_result=result)
        old = r.effective_score
        delta = "" if old is None else f"（原 {old} → 新 {result.total_score}）"
        self.regrade_status.set(
            f"✓ 重评完成 {delta}，确认无误后点「采纳重评结果」写入")
        self.new_score_var.set(str(result.total_score))
        self.adopt_btn.configure(state="normal")

    def _on_regrade_error(self, row_num: int, err: str) -> None:
        self.app.release_busy(BUSY_OWNER)
        self.regrade_btn.configure(state="normal")
        self.regrade_status.set(f"✗ 行{row_num} 重评异常")
        messagebox.showerror("重评失败", err.splitlines()[0] if err else "未知错误")

    def _on_adopt(self) -> None:
        """采纳重评结果：写 override + xlsx。"""
        pend = self._pending_regrade
        r = self.current
        if not pend or r is None or pend["row_num"] != r.row_num:
            return
        result = pend["result"]
        self._commit_override(r, result.total_score,
                              self.note_var.get().strip() or "采纳重评结果")
        self._pending_regrade = None
        self.adopt_btn.configure(state="disabled")

    # ── 生命周期 ──

    def shutdown(self) -> None:
        if self.regrade_worker is not None:
            self.regrade_worker.shutdown()

    def _on_busy_change(self, owner: str | None) -> None:
        can = owner is None and self.current is not None and bool(self.current.url)
        self.regrade_btn.configure(state="normal" if can else "disabled")
