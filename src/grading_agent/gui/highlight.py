"""C++ 语法高亮：Pygments token → Tk Text tag。

思路沿用 OneByOne/onebyone.py 的做法（CppLexer + 按 token 类型建 tag），
但改成可复用函数：先 configure_cpp_tags(text) 建标签，再 highlight_cpp(text) 上色。

实现要点（2026-08-08 重写）：
早期版本只手工列了 9 种 token 前缀，导致 Token.Name（变量名）、Token.Punctuation
（括号分号）、Token.Comment.PreprocFile（#include <iostream> 的尖括号部分）等
常见类型全都匹配不到 tag，大部分代码没有颜色，看起来就像"没有语法高亮"。
现在改为遍历 Pygments 内置样式表，为**每个** token 类型建 tag，并利用
token 类型自带的 parent 链做回退，覆盖完整。

Pygments 不可用时静默降级为纯文本（GUI 仍可用，只是没颜色）。
"""
from __future__ import annotations

import tkinter as tk

try:
    from pygments import lex
    from pygments.lexers import CppLexer
    from pygments.styles import get_style_by_name
    _PYGMENTS_OK = True
except Exception:  # pragma: no cover - 缺依赖时降级
    _PYGMENTS_OK = False


# 用哪套配色。friendly 是浅底浅色主题，与 Tk 默认白底搭配良好。
_STYLE_NAME = "friendly"

# token 类型 → tag 名的缓存（避免每次插入都重算 parent 链）
_TAG_CACHE: dict = {}


def available() -> bool:
    """Pygments 是否可用。"""
    return _PYGMENTS_OK


def _tag_name(token_type) -> str:
    """token 类型 → 合法的 Tk tag 名。'Token.Keyword.Type' → 'tok_Keyword_Type'。"""
    return "tok_" + str(token_type).replace("Token.", "").replace(".", "_")


def configure_cpp_tags(widget: tk.Text) -> None:
    """在 Text 控件上为所有 token 类型建立 tag。只需调用一次。

    只设前景色与粗/斜体，不设 font family——字体由控件自身的 font 决定。
    注释与字符串常含中文，若在此覆盖 family 会破坏调用方选好的字体。
    """
    if not _PYGMENTS_OK:
        return
    try:
        style = get_style_by_name(_STYLE_NAME)
    except Exception:
        return

    for token_type, spec in style:
        name = _tag_name(token_type)
        opts: dict = {}
        if spec.get("color"):
            opts["foreground"] = "#" + spec["color"]
        if spec.get("bgcolor"):
            opts["background"] = "#" + spec["bgcolor"]
        if spec.get("underline"):
            opts["underline"] = True
        if not opts:
            continue
        try:
            widget.tag_configure(name, **opts)
        except Exception:
            continue


def _resolve_tag(widget: tk.Text, token_type) -> str | None:
    """找该 token 类型对应的 tag；没有就沿 parent 链回退。

    例：Token.Comment.PreprocFile 没单独配色时，回退到 Token.Comment.Preproc，
    再回退到 Token.Comment——保证注释类 token 总能上色。

    缓存按 (widget, token_type) 键控：tag 是每个 Text 控件各自的，
    只按 token_type 缓存会让某个控件的解析结果错误地套用到另一个控件上。
    """
    key = (str(widget), token_type)
    cached = _TAG_CACHE.get(key)
    if cached is not None:
        return cached or None

    existing = set(widget.tag_names())
    t = token_type
    while t is not None:
        name = _tag_name(t)
        if name in existing:
            _TAG_CACHE[key] = name
            return name
        t = getattr(t, "parent", None)
    _TAG_CACHE[key] = ""
    return None


def highlight_cpp(widget: tk.Text, code: str, *, readonly: bool = True) -> None:
    """把 code 填入 widget 并做 C++ 高亮。

    Args:
        widget: 目标 Text 控件（须先调用过 configure_cpp_tags）
        code: C++ 源码
        readonly: 填完后置为 disabled（复核页签只读展示）
    """
    widget.configure(state="normal")
    widget.delete("1.0", "end")

    if not code:
        widget.insert("1.0", "(空)")
        if readonly:
            widget.configure(state="disabled")
        return

    if not _PYGMENTS_OK:
        widget.insert("1.0", code)
        if readonly:
            widget.configure(state="disabled")
        return

    try:
        for token_type, value in lex(code, CppLexer()):
            if not value:
                continue
            tag = _resolve_tag(widget, token_type)
            if tag:
                widget.insert("end", value, tag)
            else:
                widget.insert("end", value)
    except Exception:
        # 词法分析异常时退化为纯文本，不让高亮失败挡住代码展示
        widget.delete("1.0", "end")
        widget.insert("1.0", code)

    if readonly:
        widget.configure(state="disabled")


def set_plain_text(widget: tk.Text, text: str, *, readonly: bool = True) -> None:
    """填纯文本（题目描述等，不高亮）。"""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text or "(空)")
    if readonly:
        widget.configure(state="disabled")
