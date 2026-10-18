"""docx 通用读取 + 一级/二级评分标准解析（场次自适应）。

解析算法（按段落顺序状态机，见 PRD §6.3）：
1. 维护 current_session（默认 None）、current_question（默认 None）
2. 逐段扫描：
   - 含"上午场"→current_session="上午"；含"下午场"→"下午"
   - 匹配 第X题 开头 → 开始新题维度收集
   - 含"特别说明" → 后续段落归入当前题特别说明原文，直到下一题标题
   - 匹配 (N分) → 提取维度名+分值
   - 含"语法错误"且含"扣1分" → 归入当前题语法扣分规则
3. 题目结束：有 session 建一条；无 session 标记"通用"，最后复制到上下午

关键约束：
- 级别由文件名判定（一级评分标准.docx），不靠内容标题（7.12标题是"评分标准"）
- 特别说明原文必须完整保留，不强行解析成结构化规则
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from grading_agent.models import Dimension, DocxContent, StandardEntry, TaskTriple


def read_docx(path: Path) -> DocxContent:
    """读取 docx 的段落（带 style/bold）和表格列表。

    使用 python-docx。每段 dict: {text, style, bold}。
    表格：list[list[list[str]]]，每个表格是行列二维字符串数组。
    """
    from docx import Document

    doc = Document(str(path))
    paragraphs = []
    for p in doc.paragraphs:
        text = p.text or ""
        # bold：python-docx 的 bold 在 Run 级别；取首个 bold run 或段落样式判断
        bold = False
        if p.runs:
            bold = any(run.bold for run in p.runs if run.bold is not None)
        paragraphs.append({"text": text, "style": p.style.name if p.style else "", "bold": bold})

    tables = []
    for t in doc.tables:
        rows = []
        for row in t.rows:
            rows.append([cell.text or "" for cell in row.cells])
        tables.append(rows)

    return DocxContent(paragraphs=paragraphs, tables=tables)


# ── 维度提取正则 ──
# 匹配 "维度名(5分...)"：左括号后紧跟数字+"分"，括号内可能有额外说明（如"(5分，若缺少得3分)"）
# 用 (\d+)\s*分 匹配第一个出现的"N分"，维度名为左括号前的文本。
# 括号同时接受半角 () 与全角 （）——评分标准由人工录入，中文输入法下极易混入全角括号，
# 只认半角会让整条维度被静默丢弃（26.10.18 一级题2 的"程序框架正确（2分）"）。
_LPAREN = r"[（(]"
_DIMENSION_RE = re.compile(rf"^(.+?){_LPAREN}(\d+)\s*分")
_QUESTION_RE = re.compile(r"^第[一二三四五六七八九十]+题")
# 用于检测段落是否含分值标记
_SCORE_RE = re.compile(rf"{_LPAREN}(\d+)\s*分")
# 题目标题里的声明总分："第一题 25分" / "第二题（25分）" → 25
_DECLARED_TOTAL_RE = re.compile(r"第[一二三四五六七八九十]+题\s*[（(]?\s*(\d+)\s*分")

# 中文数字→阿拉伯数字（题号）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_question_no(text: str) -> int | None:
    """从"第一题"/"第二题"提取题号 1/2。"""
    m = re.search(r"第([一二三四五六七八九十]+)题", text)
    if not m:
        return None
    cn = m.group(1)
    if cn == "十":
        return 10
    if cn.startswith("十"):
        return 10 + _CN_NUM.get(cn[1], 0)
    if cn.endswith("十"):
        return _CN_NUM.get(cn[0], 1) * 10
    if len(cn) == 1:
        return _CN_NUM.get(cn)
    # 复合如"二十一"
    result = 0
    for ch in cn:
        if ch in _CN_NUM:
            result += _CN_NUM[ch]
    return result or None


def _parse_declared_total(text: str) -> int | None:
    """从题目标题提取声明总分（"第一题 25分"→25）。无声明返回 None。

    用于校验解析出的维度合计是否与文档声明一致——不一致通常意味着有维度
    没被识别（例如括号用全角、分值写法不规范），必须让人看见而不是静默少分。
    """
    m = _DECLARED_TOTAL_RE.search(text)
    return int(m.group(1)) if m else None


def _extract_dimensions_and_special_notes(paragraphs: list[dict]) -> tuple[list[Dimension], str, str]:
    """从属于"当前题"的段落块中提取维度列表、特别说明原文、语法扣分规则。

    在调用方已经把"属于这道题"的段落切出来之后调用。
    """
    dimensions: list[Dimension] = []
    special_notes_lines: list[str] = []
    syntax_penalty_rule = ""
    in_special_notes = False

    for p in paragraphs:
        text = (p.get("text") or "").strip()
        if not text:
            continue

        # 进入特别说明区
        if "特别说明" in text:
            in_special_notes = True
            # 特别说明标题本身也加入
            special_notes_lines.append(text)
            continue

        # 语法扣分规则
        if "语法错误" in text and "扣" in text and "分" in text:
            syntax_penalty_rule = text
            # 语法扣分规则也可能出现在特别说明区，归入两边
            if in_special_notes:
                special_notes_lines.append(text)
            continue

        if in_special_notes:
            # 特别说明区内的所有段落原样保留
            special_notes_lines.append(text)
            continue

        # 维度提取：匹配 "维度名(5分)"
        m = _DIMENSION_RE.search(text)
        if m:
            name = m.group(1).strip()
            try:
                max_score = int(m.group(2))
            except ValueError:
                max_score = 5
            dimensions.append(Dimension(name=name, max_score=max_score, description=text))
        elif _SCORE_RE.search(text) and len(text) < 100:
            # 短句含分值但格式不标准，整段作维度描述
            # 尝试提取分值
            sm = _SCORE_RE.search(text)
            max_score = int(sm.group(1)) if sm else 5
            dimensions.append(Dimension(name=text, max_score=max_score, description=text))

    return dimensions, "\n".join(special_notes_lines).strip(), syntax_penalty_rule


def _build_entries_by_session(
    question_blocks: dict[tuple[str | None, int], list[dict]],
    level: str,
    declared_totals: dict[tuple[str | None, int], int] | None = None,
) -> list[StandardEntry]:
    """把按 (session, question) 分组的段落块构建成 StandardEntry 列表。

    若某题有明确 session，按 session 建；若 session 为 None（未分场次），
    复制成上下午两条（同内容）。

    declared_totals：题目标题声明的总分（"第一题 25分"）。解析出的维度合计与之
    不符时打印警告——曾因全角括号静默丢掉整条维度，导致总分上限被压低却无提示。
    """
    entries: list[StandardEntry] = []
    declared_totals = declared_totals or {}

    for (session, question), paras in question_blocks.items():
        dimensions, special_notes, syntax_rule = _extract_dimensions_and_special_notes(paras)
        declared = declared_totals.get((session, question))
        where = f"{level}/{session or '未分场次'}/题{question}"

        if not dimensions:
            if declared is not None:
                print(
                    f"⚠ 警告：{where} 未解析到任何评分维度，但题目标题声明 {declared} 分"
                    f"——该题将被跳过，请检查文档格式",
                    file=sys.stderr,
                )
            continue  # 无维度，跳过（可能是标题段）

        total_max = sum(d.max_score for d in dimensions)
        if declared is not None and declared != total_max:
            names = "、".join(f"{d.name}({d.max_score}分)" for d in dimensions)
            print(
                f"⚠ 警告：{where} 维度合计 {total_max} 分，与题目标题声明的 {declared} 分不符"
                f"（已识别 {len(dimensions)} 条：{names}）——可能有维度未被识别",
                file=sys.stderr,
            )

        if session is None:
            # 未分场次：复制成上下午两条
            for sess in ("上午", "下午"):
                entries.append(StandardEntry(
                    triple=TaskTriple(level=level, session=sess, question=question),
                    total_max=total_max,
                    dimensions=dimensions,
                    special_notes=special_notes,
                    syntax_penalty_rule=syntax_rule or "语法错误/拼写错误每处扣1分",
                ))
        else:
            entries.append(StandardEntry(
                triple=TaskTriple(level=level, session=session, question=question),
                total_max=total_max,
                dimensions=dimensions,
                special_notes=special_notes,
                syntax_penalty_rule=syntax_rule or "语法错误/拼写错误每处扣1分",
            ))

    return entries


def parse_standards(path: Path, level: str) -> list[StandardEntry]:
    """解析评分标准 docx，返回 StandardEntry 列表。

    Args:
        path: docx 路径
        level: 任意级别，如 "一级"/"三级"（由调用方按文件名判定传入）
    """
    content = read_docx(path)
    paragraphs = content.paragraphs

    # 状态机遍历，按 (session, question) 分块
    current_session: str | None = None
    current_question: int | None = None
    blocks: dict[tuple[str | None, int], list[dict]] = {}
    declared_totals: dict[tuple[str | None, int], int] = {}

    for p in paragraphs:
        text = (p.get("text") or "").strip()
        if not text:
            continue

        # 场次切换
        if "上午场" in text:
            current_session = "上午"
            continue
        if "下午场" in text:
            current_session = "下午"
            continue

        # 题目切换
        qno = _parse_question_no(text)
        if qno is not None and _QUESTION_RE.match(text):
            current_question = qno
            key = (current_session, qno)
            blocks.setdefault(key, [])
            declared = _parse_declared_total(text)
            if declared is not None:
                declared_totals[key] = declared
            # 题目标题段本身不入维度，跳过
            continue

        # 维度/特别说明/语法规则段落：归入当前题
        if current_question is not None:
            key = (current_session, current_question)
            blocks.setdefault(key, []).append(p)

    return _build_entries_by_session(blocks, level=level, declared_totals=declared_totals)
