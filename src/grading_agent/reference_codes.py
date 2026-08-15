"""参考代码 docx 解析：状态机遍历段落+表格，产出 C++ 代码（任意级别）。

文档结构（以三级为例，任意级别通用）：
  段落"周六上午场参考代码：" → current_session="上午"
    段落"一级：" → current_level="一级"
      表格1 → (一级,上午,题1) 代码
      表格2 → (一级,上午,题2) 代码
    段落"二级：" → current_level="二级"，重置题号计数
      表格3 → (二级,上午,题1) 代码
    段落"三级：" → current_level="三级"，重置题号计数
      表格... → (三级,上午,题N) 代码
  段落"周六下午场参考代码：" → current_session="下午"
    ... 同上

代码后处理：HTML实体反转(&lt;→<) + 每行rstrip + strip。
"""
from __future__ import annotations

import html
from pathlib import Path

from grading_agent.models import TaskTriple, extract_level
from grading_agent.docx_parser import read_docx


def _detect_session(text: str) -> str | None:
    """从段落文本识别场次。'上午场'→'上午'，'下午场'→'下午'。"""
    if "上午场" in text or "上午" in text:
        return "上午"
    if "下午场" in text or "下午" in text:
        return "下午"
    return None


def _detect_level(text: str) -> str | None:
    """从段落文本识别级别。'三级:'/'3级' → '三级'（归一化为中文，支持任意级别）。"""
    return extract_level(text.strip())


def _clean_code(raw: str) -> str:
    """代码后处理：HTML实体反转 + 每行rstrip + 整体strip。"""
    if not raw:
        return ""
    # HTML 实体反转（docx 表格里 < > & 可能被转义）
    decoded = html.unescape(raw)
    lines = [line.rstrip() for line in decoded.splitlines()]
    return "\n".join(lines).strip()


def parse_reference_codes(path: Path) -> dict[TaskTriple, str]:
    """解析参考代码 docx，返回 {(级别,场次,题号): 代码}。

    状态机遍历段落+表格：
    - 段落含场次关键词 → 更新 current_session
    - 段落含级别关键词 → 更新 current_level，重置 current_question 计数
    - 遇到表格 → 取表格[0][0] 文本作代码，current_question 按"当前level下第N个表"递增
    """
    content = read_docx(path)

    current_session: str | None = None
    current_level: str | None = None
    question_counter: int = 0  # 当前 level 下遇到的表格数

    result: dict[TaskTriple, str] = {}

    # python-docx 的段落和表格是分开的两个列表，但文档里它们是交错的。
    # 要按文档真实顺序遍历，需要用 iter_body_items（docx 0.1.x+）或遍历 XML。
    # 这里用 document.element.body 的子元素来保证顺序。
    from docx import Document
    doc = Document(str(path))

    def iter_body_items():
        """按文档顺序遍历段落和表格。"""
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        body = doc.element.body
        for child in body.iterchildren():
            if child.tag.endswith("}p"):
                yield Paragraph(child, doc)
            elif child.tag.endswith("}tbl"):
                yield Table(child, doc)

    for item in iter_body_items():
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        if isinstance(item, Paragraph):
            text = (item.text or "").strip()
            if not text:
                continue
            # 检测场次（"上午"/"下午"）与级别（"X级"），两者相互独立。
            # 兼容两种标题格式：
            # 1. 分开：场次标题（"周六上午场参考代码："）+ 级别标题（"一级："）
            # 2. 合并：级别+场次同一段（"一级（上午）："）
            sess = _detect_session(text)
            if sess:
                current_session = sess
            lvl = _detect_level(text)
            if lvl:
                current_level = lvl
                question_counter = 0  # 换 level 重置题号
            # 不 continue：合并格式下同一段可能同时含场次和级别
        elif isinstance(item, Table):
            if current_level is None or current_session is None:
                # 还没确定 level/session 就遇到表格，跳过（异常情况）
                continue
            question_counter += 1
            # 取表格 [0][0] 文本作为代码
            try:
                raw_code = item.rows[0].cells[0].text or ""
            except Exception:
                raw_code = ""
            code = _clean_code(raw_code)
            triple = TaskTriple(
                level=current_level,  # type: ignore[arg-type]
                session=current_session,  # type: ignore[arg-type]
                question=question_counter,  # type: ignore[arg-type]
            )
            result[triple] = code

    return result
