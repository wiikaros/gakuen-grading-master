"""阶段 B 题目确认弹窗（也复用于熔断询问）。

比 CLI 强的地方：左右并排显示题目描述与参考代码，切换候选即换参考代码，
判断依据一目了然（CLI 只能打印参考代码前 5 行）。

返回值与 CLI 的输入约定一致：'y' / 'n' / '1'..'N'，
由 identifier.confirm_with_user() 解析，因此不需要改动那边的逻辑。
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

from grading_agent.gui.highlight import configure_cpp_tags, highlight_cpp, set_plain_text


def _parse_choice_range(prompt: str) -> int:
    """从提示文本 '确认 [y=确认推荐 / n=拒绝 / 1-8=选其他候选]' 提取候选上限。"""
    m = re.search(r"1-(\d+)", prompt or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 0


def _is_circuit_prompt(prompt: str) -> bool:
    """熔断询问用 c/a/s，与题目确认的 y/n/数字不同。"""
    return "熔断" in (prompt or "") or "c=继续" in (prompt or "")


def ask_confirm(parent, prompt: str, *, stdlib=None,
                body_font=None, code_font=None,
                description: str = "", task_id: str = "",
                best_triple: str = "") -> str:
    """弹出确认对话框，阻塞直到用户选择。返回给 Orchestrator 的输入字符串。"""
    if _is_circuit_prompt(prompt):
        return _ask_circuit(parent, prompt, body_font)
    return _ask_identification(parent, prompt, stdlib, body_font, code_font,
                               description, task_id, best_triple)


def _ask_circuit(parent, prompt: str, body_font) -> str:
    """熔断询问：继续 / 中止 / 跳过剩余。"""
    dlg = tk.Toplevel(parent)
    dlg.title("熔断确认")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.resizable(False, False)

    result = {"value": "a"}  # 直接关窗默认中止（保守）

    frame = ttk.Frame(dlg, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=prompt.strip(), font=body_font,
              wraplength=460, justify="left").pack(anchor="w")

    btns = ttk.Frame(frame)
    btns.pack(anchor="e", pady=(16, 0))

    def choose(v: str) -> None:
        result["value"] = v
        dlg.destroy()

    ttk.Button(btns, text="继续", command=lambda: choose("c")).pack(side="left", padx=4)
    ttk.Button(btns, text="跳过剩余", command=lambda: choose("s")).pack(side="left", padx=4)
    ttk.Button(btns, text="中止", command=lambda: choose("a")).pack(side="left", padx=4)

    dlg.protocol("WM_DELETE_WINDOW", lambda: choose("a"))
    _center(dlg, parent)
    parent.wait_window(dlg)
    return result["value"]


def _ask_identification(parent, prompt: str, stdlib, body_font, code_font,
                        description: str, task_id: str, best_triple: str = "") -> str:
    """题目确认：候选单选 + 参考代码预览。"""
    triples = []
    if stdlib is not None:
        try:
            triples = stdlib.all_triples()
        except Exception:
            triples = []
    max_choice = _parse_choice_range(prompt) or len(triples)
    # 默认选中 LLM 推荐的最佳候选（best_triple），而非恒选第 1 个
    best_idx = 1
    for i, t in enumerate(triples, start=1):
        if str(t) == best_triple:
            best_idx = i
            break

    dlg = tk.Toplevel(parent)
    dlg.title("确认题目识别")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.geometry("1000x620")

    result = {"value": "n"}  # 直接关窗＝拒绝（该题目ID跳过评分）

    root = ttk.Frame(dlg, padding=12)
    root.pack(fill="both", expand=True)
    root.columnconfigure(0, weight=1)
    root.columnconfigure(1, weight=1)
    root.rowconfigure(1, weight=1)

    # 顶部：CLI 原始提示（含 LLM 推荐与置信度，已由 identifier 打印过）
    header = ttk.Label(root, text=prompt.strip(), font=body_font,
                       wraplength=950, justify="left")
    header.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

    # 左：题目描述
    left = ttk.LabelFrame(root, text=" 抓取到的题目描述 ", padding=6)
    left.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)
    desc_area = tk.Text(left, wrap="word", font=body_font, height=18)
    desc_area.grid(row=0, column=0, sticky="nsew")
    dsb = ttk.Scrollbar(left, orient="vertical", command=desc_area.yview)
    dsb.grid(row=0, column=1, sticky="ns")
    desc_area.configure(yscrollcommand=dsb.set)
    set_plain_text(desc_area, description or "(未提供题目描述，见日志区)")

    # 右：所选候选的参考代码
    right = ttk.LabelFrame(root, text=" 该候选的参考代码 ", padding=6)
    right.grid(row=1, column=1, sticky="nsew")
    right.rowconfigure(0, weight=1)
    right.columnconfigure(0, weight=1)
    code_area = tk.Text(right, wrap="none", font=code_font, height=18)
    code_area.grid(row=0, column=0, sticky="nsew")
    csb = ttk.Scrollbar(right, orient="vertical", command=code_area.yview)
    csb.grid(row=0, column=1, sticky="ns")
    code_area.configure(yscrollcommand=csb.set)
    configure_cpp_tags(code_area)

    # 候选单选
    choice_frame = ttk.LabelFrame(root, text=" 候选（选中即预览其参考代码） ", padding=6)
    choice_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    choice_var = tk.IntVar(value=best_idx)

    def show_code(idx: int) -> None:
        if stdlib is None or not triples or not (1 <= idx <= len(triples)):
            set_plain_text(code_area, "(无法预览：标准库不可用)")
            return
        try:
            bundle = stdlib.get(triples[idx - 1])
            highlight_cpp(code_area, bundle.reference_code)
        except Exception as exc:
            set_plain_text(code_area, f"(无参考代码: {exc})")

    if triples:
        cols = 4
        for i, t in enumerate(triples, start=1):
            ttk.Radiobutton(
                choice_frame, text=f"{i}. {t}", value=i, variable=choice_var,
                command=lambda i=i: show_code(i),
            ).grid(row=(i - 1) // cols, column=(i - 1) % cols, sticky="w", padx=6, pady=2)
        show_code(best_idx)
    else:
        ttk.Label(choice_frame,
                  text=f"标准库不可用，请直接输入 1-{max_choice} 或使用下方按钮",
                  foreground="#a60").pack(anchor="w")
        set_plain_text(code_area, "(标准库不可用)")

    # 操作按钮
    btns = ttk.Frame(root)
    btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))

    def choose(v: str) -> None:
        result["value"] = v
        dlg.destroy()

    ttk.Button(btns, text="确认推荐 (y)",
               command=lambda: choose("y")).pack(side="left", padx=4)
    ttk.Button(btns, text="用选中的候选",
               command=lambda: choose(str(choice_var.get()))).pack(side="left", padx=4)
    ttk.Button(btns, text="拒绝 / 跳过该题目ID",
               command=lambda: choose("n")).pack(side="left", padx=4)

    dlg.protocol("WM_DELETE_WINDOW", lambda: choose("n"))
    _center(dlg, parent)
    parent.wait_window(dlg)
    return result["value"]


def _center(dlg: tk.Toplevel, parent) -> None:
    """相对父窗口居中。"""
    try:
        dlg.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
    except Exception:
        pass
