"""一致性验证：智能体评分 vs 人工评分。

PRD §13 核心原则：智能体不看 G 列（TaskRow 不含 score，read_rows(skip_score=True)）。
验证流程跑完后，才读人工评分做对比。

对比按 (answer_id, task_id) 联合主键 join——answer_id 在 xlsx 中并非每行唯一
（一份答卷含 2 道题时出现 2 行），联合 task_id 才能唯一定位行。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from grading_agent.xlsx_io import read_scores_by_key, to_int_score


@dataclass
class VerificationReport:
    """一致性验证报告。"""
    total_comparable: int = 0         # 能转成数字的行数（排除"打不开"等非数字）
    within_2_points: int = 0          # |AI - 人工| <= 2 的行数
    within_2_rate: float = 0.0
    exact_match: int = 0              # AI == 人工
    exact_rate: float = 0.0
    mean_abs_error: float = 0.0       # 平均绝对偏差
    max_abs_error: int = 0
    bias_distribution: dict[int, int] = field(default_factory=dict)  # {偏差值: 行数}
    by_task_id: dict[str, dict] = field(default_factory=dict)  # 按题目ID分组
    by_score_band: dict[str, dict] = field(default_factory=dict)  # 按人工评分分档
    fetch_failed: int = 0
    llm_failed: int = 0
    ai_only: int = 0     # AI 评了但人工评分缺失/非数字
    human_only: int = 0  # 人工有评分但 AI 没评（抓取/LLM 失败）


def _score_band(score: int) -> str:
    """人工评分分档：0-5/6-12/13-18/19-24。"""
    if score <= 5:
        return "0-5分"
    if score <= 12:
        return "6-12分"
    if score <= 18:
        return "13-18分"
    return "19-24分"


def run_consistency_check(
    task_dir: Path,
    detail_log_path: Path,
    output_dir: Path,
    print_fn=print,
) -> VerificationReport:
    """跑一致性对比，打印报告 + 写 verification_report.txt。

    Args:
        task_dir: 任务文件夹（含 xlsx）
        detail_log_path: 阶段 C 产出的 grading_detail.jsonl
        output_dir: 输出目录（写 verification_report.txt）
    """
    import json

    # 找 xlsx
    xlsx_candidates = [p for p in task_dir.glob("*.xlsx") if "_autosave" not in p.name and not p.name.startswith("~$")]
    if not xlsx_candidates:
        print_fn("❌ 任务文件夹未找到 xlsx")
        return VerificationReport()
    xlsx_path = xlsx_candidates[0]

    # 读人工评分（此时才读 G 列）：{(answer_id, task_id): score}
    human_scores = read_scores_by_key(xlsx_path)

    # 读 AI 评分明细 JSONL
    ai_scores: dict[tuple[str, str], dict] = {}
    fetch_failed = 0
    llm_failed = 0
    if detail_log_path.exists():
        with open(detail_log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (str(rec.get("answer_id", "")), str(rec.get("task_id", "")))
                ai_scores[key] = rec
                if not rec.get("parse_ok", True):
                    llm_failed += 1

    # 对比
    report = VerificationReport(fetch_failed=fetch_failed, llm_failed=llm_failed)
    abs_errors: list[int] = []
    by_task_accum: dict[str, list[int]] = defaultdict(list)
    by_band_accum: dict[str, list[int]] = defaultdict(list)

    all_keys = set(human_scores.keys()) | set(ai_scores.keys())
    for key in all_keys:
        ai_rec = ai_scores.get(key)
        human_score = human_scores.get(key)

        if ai_rec is None:
            # 人工有评分但 AI 没评
            report.human_only += 1
            continue
        if not ai_rec.get("parse_ok", True):
            # AI 评分失败，跳过对比
            continue

        ai_score = to_int_score(ai_rec.get("total_score"))
        if human_score is None:
            # AI 评了但人工评分缺失/非数字
            report.ai_only += 1
            continue
        if ai_score is None:
            continue

        # 可对比行
        report.total_comparable += 1
        diff = ai_score - human_score
        abs_err = abs(diff)
        abs_errors.append(abs_err)
        report.bias_distribution[diff] = report.bias_distribution.get(diff, 0) + 1
        report.max_abs_error = max(report.max_abs_error, abs_err)
        if abs_err <= 2:
            report.within_2_points += 1
        if diff == 0:
            report.exact_match += 1

        # 按题目ID分组
        task_id = key[1]
        by_task_accum[task_id].append(abs_err)
        # 按人工评分分档
        band = _score_band(human_score)
        by_band_accum[band].append(abs_err)

    # 汇总指标
    if report.total_comparable:
        report.within_2_rate = report.within_2_points / report.total_comparable
        report.exact_rate = report.exact_match / report.total_comparable
        report.mean_abs_error = sum(abs_errors) / len(abs_errors)

    # 按题目ID 统计
    for tid, errs in by_task_accum.items():
        n = len(errs)
        w2 = sum(1 for e in errs if e <= 2)
        report.by_task_id[tid] = {
            "count": n,
            "within_2_rate": w2 / n if n else 0.0,
            "mean_abs_error": sum(errs) / n if n else 0.0,
        }

    # 按分档统计
    band_order = ["0-5分", "6-12分", "13-18分", "19-24分"]
    for band in band_order:
        errs = by_band_accum.get(band, [])
        if not errs:
            continue
        n = len(errs)
        w2 = sum(1 for e in errs if e <= 2)
        report.by_score_band[band] = {
            "count": n,
            "within_2_rate": w2 / n if n else 0.0,
        }

    # 输出报告
    _print_report(report, task_dir.name, output_dir, print_fn)
    return report


def _print_report(report: VerificationReport, task_name: str, output_dir: Path, print_fn) -> None:
    """打印报告到控制台 + 写 verification_report.txt。"""
    lines: list[str] = []
    lines.append(f"========== 一致性验证报告 ({task_name}) ==========")
    lines.append(f"可对比行数: {report.total_comparable} (AI有评分且人工评分可解析为数字的行)")
    lines.append(f"±2分内一致率: {report.within_2_rate*100:.1f}%  ({report.within_2_points}/{report.total_comparable})")
    lines.append(f"完全一致率:  {report.exact_rate*100:.1f}%  ({report.exact_match}/{report.total_comparable})")
    lines.append(f"平均绝对偏差: {report.mean_abs_error:.2f} 分")
    lines.append(f"最大偏差:    {report.max_abs_error} 分")
    lines.append(f"AI有评分但人工评分缺失/非数字: {report.ai_only} 行")
    lines.append(f"人工有评分但AI未评: {report.human_only} 行")
    lines.append(f"LLM解析失败: {report.llm_failed} 行")
    lines.append("")
    lines.append("偏差分布 (AI - 人工):")
    if report.bias_distribution:
        for diff in sorted(report.bias_distribution.keys()):
            lines.append(f"  {diff:+d}: {report.bias_distribution[diff]}")
    lines.append("")
    lines.append("按题目ID:")
    for tid, stats in sorted(report.by_task_id.items()):
        lines.append(f"  {tid} ({stats['count']}行): ±2一致率 {stats['within_2_rate']*100:.1f}%, 平均偏差 {stats['mean_abs_error']:.2f}")
    lines.append("")
    lines.append("按人工评分分档:")
    for band in ["0-5分", "6-12分", "13-18分", "19-24分"]:
        if band in report.by_score_band:
            stats = report.by_score_band[band]
            lines.append(f"  {band} ({stats['count']}行): ±2一致率 {stats['within_2_rate']*100:.1f}%")
    lines.append("")
    lines.append("可用性判定门槛:")
    if report.within_2_rate >= 0.85:
        lines.append(f"  ✓ ±2一致率 {report.within_2_rate*100:.1f}% ≥ 85%，可投入生产")
    elif report.within_2_rate >= 0.80:
        lines.append(f"  △ ±2一致率 {report.within_2_rate*100:.1f}% 在 80-85%，需调 prompt 或降阈值")
    else:
        lines.append(f"  ✗ ±2一致率 {report.within_2_rate*100:.1f}% < 80%，评分 prompt 或标准库解析有问题")

    text = "\n".join(lines)
    print_fn(text)

    # 写文件
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "verification_report.txt"
    report_path.write_text(text, encoding="utf-8")
    print_fn(f"\n报告已写入: {report_path}")
