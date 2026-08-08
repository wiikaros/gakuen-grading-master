"""GUI 主窗口：Tk root + 高 DPI + 字体 + Notebook 双页签 + 全局 busy 互斥。

高 DPI 处理沿用 OneByOne/onebyone.py:49-51 的做法（Windows shcore）。

busy 互斥的作用：监控台跑批与复核页签的单行重评都要用 Playwright 和写 xlsx，
两者同时跑会导致浏览器实例打架、xlsx 并发写冲突。故用一个全局标志互相置灰按钮。
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

from grading_agent.config import load_config


APP_TITLE = "学园阅卷大师"
DEFAULT_GEOMETRY = "1500x950"


def _enable_high_dpi(root: tk.Tk) -> None:
    """Windows 高 DPI 支持。失败静默（非 Windows 或旧系统）。

    原样沿用 onebyone.py:49-51。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        scale_factor = ctypes.windll.shcore.GetScaleFactorForDevice(0)
        root.tk.call("tk", "scaling", scale_factor / 75)
    except Exception:
        pass


class GradingGuiApp(tk.Tk):
    """主窗口：两个页签共享 config 与 busy 状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(DEFAULT_GEOMETRY)

        _enable_high_dpi(self)

        self.config_obj = load_config()
        self.output_root = Path("output")

        # 全局互斥：谁在用 Playwright / 写 xlsx
        self._busy_owner: str | None = None
        self._busy_listeners: list = []

        # 代码区字体是否含中文字形（_setup_fonts 内赋值，界面据此决定是否提示）
        self.code_font_is_cjk_safe: bool = True

        self._setup_fonts()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_fonts(self) -> None:
        """字体：正文/代码/明细/日志四类。

        中文兼容要点：Consolas、Cascadia Mono 都不含中文字形，遇到中文会 fallback 到
        系统字体，导致同一行内中英文行高与基线不一致、显示发虚。因此：
        - detail_font（评分明细，中文为主）用「Microsoft YaHei UI」等含中文字形的字体
        - code_font（学生代码，含中文注释/字符串）必须「等宽 且 含中文字形」，见 _pick_code_font
        - log_font（日志，纯 ASCII）才用纯英文等宽字体
        另见 review_view._render_scores：列对齐改用 Text 的 tabs（按像素），
        不用空格填充——中文是双宽字符，按字符数填充必然错位。
        """
        self.body_font = tkfont.Font(family=self._pick_font(
            ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei"], "TkDefaultFont"), size=10)
        # 学生代码：等宽 + 含中文字形（注释与字符串常含中文）
        self.code_font = tkfont.Font(family=self._pick_code_font(), size=10)
        # 评分明细：中文为主，必须用含中文字形的字体
        self.detail_font = tkfont.Font(family=self._pick_font(
            ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei"], "TkDefaultFont"), size=10)
        # 表格：数字为主但含中文状态列，同样用中文字体保证行高一致
        self.mono_small = tkfont.Font(family=self._pick_font(
            ["Microsoft YaHei UI", "Microsoft YaHei"], "TkDefaultFont"), size=9)
        # 日志区：纯 ASCII 为主，等宽更易读
        self.log_font = tkfont.Font(family=self._pick_font(
            ["Consolas", "Cascadia Mono", "Courier New"], "TkFixedFont"), size=9)
        try:
            style = ttk.Style(self)
            style.configure("Treeview", font=self.mono_small, rowheight=24)
            style.configure("Treeview.Heading", font=self.body_font)
        except Exception:
            pass

    def _pick_font(self, candidates: list[str], fallback: str) -> str:
        """返回系统中第一个可用的字体族，都没有则用 Tk 内置族。

        避免在缺字体的机器上 Tk 静默回退到难看的默认字体。
        """
        try:
            available = set(tkfont.families(self))
        except Exception:
            return fallback
        for name in candidates:
            if name in available:
                return name
        return fallback

    # 代码区候选字体，按优先级。
    # 前三款「等宽且含中文字形」，中英严格 2:1，最理想；
    # 其后是纯英文等宽字体——中文靠 Tk 自身的字体回退显示，
    # 虽然中文宽度未必恰好 2 倍，但英文/缩进/语法高亮都正常，代码可读性最重要。
    #
    # 注意：不要把「新宋体」「SimSun」列进来。它们是**比例字体**，
    # 只有 NSimSun 才是等宽变体；误用会让整个代码区失去等宽，比 fallback 更糟。
    _CODE_FONT_CANDIDATES = [
        "Sarasa Mono SC",        # 更纱黑体，中英 2:1，最佳
        "Sarasa Fixed SC",
        "Microsoft YaHei Mono",
        "NSimSun",               # 宋体的等宽变体（注意：非「新宋体」）
        "Cascadia Mono",         # 以下纯英文等宽，中文走 Tk 回退
        "Cascadia Code",
        "Consolas",
        "Courier New",
    ]
    # 含中文字形的那部分（用于判断是否需要提示用户）
    _CJK_CAPABLE_CODE_FONTS = {
        "Sarasa Mono SC", "Sarasa Fixed SC", "Microsoft YaHei Mono", "NSimSun",
    }

    def _pick_code_font(self) -> str:
        """为学生代码区选字体：**等宽是硬要求**，含中文字形是加分项。

        取候选表中第一个「实测等宽」的字体。实测而非按名字猜——
        字体族名不可靠（如「新宋体」名字像等宽实则是比例字体，
        选它会让缩进和语法高亮全乱）。

        选不到含中文字形的等宽字体时，中文由 Tk 字体回退渲染：
        中文宽度可能不是英文的整两倍，但英文、缩进、高亮均正常，
        对读代码而言这个取舍是对的。
        """
        try:
            available = set(tkfont.families(self))
        except Exception:
            self.code_font_is_cjk_safe = False
            return "TkFixedFont"

        for name in self._CODE_FONT_CANDIDATES:
            if name not in available:
                continue
            if not self._is_monospaced(name):
                continue  # 名字像等宽但实测不是（如「新宋体」），跳过
            self.code_font_is_cjk_safe = name in self._CJK_CAPABLE_CODE_FONTS
            return name

        self.code_font_is_cjk_safe = False
        return "TkFixedFont"

    def _is_monospaced(self, family: str) -> bool:
        """实测字体是否等宽：窄字符 'i' 与宽字符 'W'、'm' 宽度一致。"""
        try:
            f = tkfont.Font(family=family, size=10)
            w = f.measure("i")
            return w > 0 and w == f.measure("W") == f.measure("m")
        except Exception:
            return False

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        # 延迟导入避免循环依赖
        from grading_agent.gui.monitor_view import MonitorView
        from grading_agent.gui.review_view import ReviewView

        self.monitor_view = MonitorView(self.notebook, self)
        self.review_view = ReviewView(self.notebook, self)

        self.notebook.add(self.monitor_view, text="  跑批监控台  ")
        self.notebook.add(self.review_view, text="  结果复核  ")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Label(self, textvariable=self.status_var,
                           relief="sunken", anchor="w", padding=(8, 3))
        status.grid(row=1, column=0, sticky="ew")

    # ── busy 互斥 ──

    @property
    def busy_owner(self) -> str | None:
        return self._busy_owner

    def is_busy(self, exclude: str | None = None) -> bool:
        """是否有任务占用。exclude 为自己的名字时忽略自身占用。"""
        if self._busy_owner is None:
            return False
        return self._busy_owner != exclude

    def acquire_busy(self, owner: str) -> bool:
        """尝试占用。已被他人占用则返回 False。"""
        if self._busy_owner is not None and self._busy_owner != owner:
            return False
        self._busy_owner = owner
        self._notify_busy()
        return True

    def release_busy(self, owner: str) -> None:
        if self._busy_owner == owner:
            self._busy_owner = None
            self._notify_busy()

    def add_busy_listener(self, callback) -> None:
        """注册 busy 变化回调，页签用它置灰/恢复按钮。"""
        self._busy_listeners.append(callback)

    def _notify_busy(self) -> None:
        for cb in list(self._busy_listeners):
            try:
                cb(self._busy_owner)
            except Exception:
                pass

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _on_close(self) -> None:
        """关闭前清理 worker（尤其是常驻浏览器）。"""
        try:
            self.review_view.shutdown()
        except Exception:
            pass
        try:
            self.monitor_view.shutdown()
        except Exception:
            pass
        self.destroy()


def main() -> int:
    app = GradingGuiApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
