"""xlsx 读写：按表头名定位列（兼容 7列/12列两种布局）。

表头匹配规则（容错所有已知变体）：
- 题目ID列：表头含"题目"且含"ID"
- 链接列：表头含"链接"
- 评分列：表头 == "阅卷人评分"（排除"抽查人评分"）
- 用户ID列：表头含"用户"且含"ID"
- 答卷ID列：表头含"答卷"且含"ID"

关键约束：禁止按固定列索引定位（7.12是7列，12.14/12.15是10-12列）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from grading_agent.models import TaskRow


@dataclass
class XlsxLayout:
    """xlsx 列布局：各关键列的 1-based 索引。"""
    link_col_idx: int
    score_col_idx: int
    task_id_col_idx: int
    answer_id_col_idx: Optional[int]
    user_id_col_idx: Optional[int]


def _header_matches(header: str, *keywords: str) -> bool:
    """表头是否包含所有关键词（忽略大小写、空格）。"""
    h = (header or "").strip()
    return all(k in h for k in keywords)


def detect_layout(headers: list[str]) -> XlsxLayout:
    """按表头关键字匹配列。找不到必需列时抛 ValueError。"""
    link_idx: Optional[int] = None
    score_idx: Optional[int] = None
    task_id_idx: Optional[int] = None
    answer_id_idx: Optional[int] = None
    user_id_idx: Optional[int] = None

    for idx, header in enumerate(headers, start=1):
        header_str = str(header) if header is not None else ""

        # 评分列：必须精确等于"阅卷人评分"（排除"抽查人评分"）
        if header_str.strip() == "阅卷人评分":
            score_idx = idx
            continue

        # 题目ID列：含"题目"且含"ID"
        if _header_matches(header_str, "题目", "ID") and task_id_idx is None:
            task_id_idx = idx
            continue

        # 链接列：含"链接"
        if _header_matches(header_str, "链接") and link_idx is None:
            link_idx = idx
            continue

        # 用户ID列：含"用户"且含"ID"
        if _header_matches(header_str, "用户", "ID") and user_id_idx is None:
            user_id_idx = idx
            continue

        # 答卷ID列：含"答卷"且含"ID"
        if _header_matches(header_str, "答卷", "ID") and answer_id_idx is None:
            answer_id_idx = idx
            continue

    # 必需列校验
    missing = []
    if link_idx is None:
        missing.append("链接列（表头含'链接'）")
    if score_idx is None:
        missing.append("评分列（表头=='阅卷人评分'）")
    if task_id_idx is None:
        missing.append("题目ID列（表头含'题目'和'ID'）")
    if missing:
        raise ValueError(
            "xlsx 列定位失败，缺少：" + "；".join(missing)
            + f"\n实际表头：{headers}"
        )

    return XlsxLayout(
        link_col_idx=link_idx,
        score_col_idx=score_idx,
        task_id_col_idx=task_id_idx,
        answer_id_col_idx=answer_id_idx,
        user_id_col_idx=user_id_idx,
    )


def read_rows(path: Path, skip_score: bool = True) -> tuple[list[TaskRow], XlsxLayout, "object"]:
    """读取 xlsx 全部数据行。

    Args:
        path: xlsx 路径
        skip_score: True 时不读评分列（一致性验证用，确保工具不看 G 列）。
                    生产模式下也默认 True——评分是输出，不需要读。

    Returns:
        (rows, layout, workbook)：返回行列表、列布局、已加载的 workbook（供后续写回）
    """
    from openpyxl import load_workbook

    wb = load_workbook(path)
    sheet = wb.active
    headers = [cell.value for cell in sheet[1]]
    layout = detect_layout(headers)

    rows: list[TaskRow] = []
    for row in range(2, sheet.max_row + 1):
        url = sheet.cell(row=row, column=layout.link_col_idx).value
        if not url:
            continue  # 空行跳过
        task_id = sheet.cell(row=row, column=layout.task_id_col_idx).value
        answer_id = (
            sheet.cell(row=row, column=layout.answer_id_col_idx).value
            if layout.answer_id_col_idx else None
        )
        user_id = (
            sheet.cell(row=row, column=layout.user_id_col_idx).value
            if layout.user_id_col_idx else None
        )

        # 收集 metadata（赛区ID 等其他列）
        metadata = {}
        for col_idx, header in enumerate(headers, start=1):
            if col_idx in (layout.link_col_idx, layout.score_col_idx,
                           layout.task_id_col_idx, layout.answer_id_col_idx,
                           layout.user_id_col_idx):
                continue
            val = sheet.cell(row=row, column=col_idx).value
            if val is not None and header:
                metadata[str(header)] = val

        rows.append(TaskRow(
            row_num=row,
            user_id=str(user_id) if user_id is not None else "",
            answer_id=str(answer_id) if answer_id is not None else "",
            task_id=str(task_id) if task_id is not None else "",
            url=str(url),
            metadata=metadata,
        ))

    return rows, layout, wb


def update_cell_in_memory(sheet, row_num: int, col_idx: int, value) -> None:
    """只修改内存中的 cell 值，不写盘。每行评分后调用。"""
    sheet.cell(row=row_num, column=col_idx).value = value


def save_workbook(wb, path: Path) -> Path:
    """持久化到磁盘。PermissionError 时另存为 <stem>_autosave.xlsx。

    返回实际保存路径（正常为原路径，冲突时为新路径）。
    """
    try:
        wb.save(str(path))
        return path
    except PermissionError:
        fallback = path.with_name(path.stem + "_autosave.xlsx")
        wb.save(str(fallback))
        return fallback


def read_scores_by_row(path: Path) -> dict[int, int | None]:
    """按行号读取评分列：{row_num: 分数或 None}。

    读取评分列的唯一入口——人工评分对比、复核界面的偏差列与"副本现值"都用它。
    写入本来也按 row_num 寻址（update_cell_in_memory(sheet, row_num, ...)），
    读写同口径。

    为什么不用 (answer_id, task_id) 联合主键：该键在真实数据里并非唯一
    （9.13 的 rows 849/850 同键、单元格分别是 12 / 13，塌成一条后两行会取到同一个
    值）；一致性对比还因此在并发下出现过错配——AI 侧按 jsonl 完成顺序取最后一条、
    人工侧按行序取最后一个同键行，实测 9.13 该键在 jsonl 里是 [850, 849]，两侧会
    分别落到 849 与 850。

    空格与非数字文本（如 '25（疑似AI）'）一律 → None。
    """
    from openpyxl import load_workbook

    wb = load_workbook(path)
    sheet = wb.active
    headers = [cell.value for cell in sheet[1]]
    layout = detect_layout(headers)

    result: dict[int, int | None] = {}
    for row in range(2, sheet.max_row + 1):
        result[row] = to_int_score(sheet.cell(row=row, column=layout.score_col_idx).value)
    return result


def to_int_score(v) -> int | None:
    """评分转换：处理字符串型数字，跳过非数字（如"打不开"）。

    '4'→4, '18.0'→18, '打不开'→None, None→None, ''→None。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None
