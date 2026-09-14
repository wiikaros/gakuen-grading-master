"""主编排器：阶段 A-D 状态机。

PRD §4:
- 阶段 A：读 xlsx + 构建标准库 + 冒烟测试
- 阶段 B：每个新题目ID 抓首行 → LLM 分类 → CLI 确认 → 缓存（仅进程内）
- 阶段 C：遍历每行 → 抓取 → 评分 → 置信度分流 → JSONL 增量 + xlsx 定期 save
- 阶段 D：保存 xlsx + 导出清单

含熔断（连续 N 次同类失败暂停）和断点续跑（从 JSONL 跳过已完成行）。
"""
from __future__ import annotations

import datetime as _dt
import queue as _queue
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from grading_agent.config import Config
from grading_agent.ai_xlsx import ensure_ai_xlsx
from grading_agent.logging_utils import (
    read_completed_rows,
    read_triple_map_from_detail,
    write_grade_detail,
    write_review_queue,
)
from grading_agent.models import (
    FetchResult,
    GradeResult,
    RunReport,
    TaskRow,
    TaskTriple,
    level_sort_key,
)
from grading_agent.standard_library import StandardLibrary, StandardLibraryError, find_task_docxs
from grading_agent.xlsx_io import (
    read_rows,
    save_workbook,
    update_cell_in_memory,
)
from grading_agent.agent.grader import Grader
from grading_agent.agent.identifier import ProblemIdentifier
from grading_agent.llm.langchain_backend import LangChainBackend, make_llm
from grading_agent.url_fetcher import UrlFetcher


# ── 任务文件夹定位辅助 ──

def _find_xlsx(task_dir: Path) -> Path:
    """在任务文件夹找 xlsx 文件。"""
    candidates = list(task_dir.glob("*.xlsx"))
    candidates = [c for c in candidates if not c.name.startswith("~$") and "_autosave" not in c.name]
    if not candidates:
        raise FileNotFoundError(f"任务文件夹 {task_dir} 未找到 xlsx 文件")
    return candidates[0]


def _parse_triple_str(s: str) -> TaskTriple | None:
    """解析 '一级/上午/题1' 字符串为 TaskTriple。"""
    try:
        parts = s.split("/")
        if len(parts) != 3:
            return None
        level = parts[0]
        session = parts[1]
        qno = int(parts[2].replace("题", ""))
        return TaskTriple(level=level, session=session, question=qno)  # type: ignore[arg-type]
    except Exception:
        return None


@dataclass
class _RunState:
    """运行期可变状态。"""
    fetch_failed_streak: int = 0
    llm_failed_streak: int = 0
    fetched_ok: int = 0
    fetch_failed: int = 0
    high_confidence_written: int = 0
    low_confidence_queued: int = 0
    low_confidence_written: int = 0
    review_rows: list[dict] = field(default_factory=list)
    aborted: bool = False


class Orchestrator:
    """主编排器。"""

    def __init__(
        self,
        task_dir: Path,
        config: Config,
        *,
        max_rows: int | None = None,
        debug_fetch: bool = False,
        skip_identification: bool = False,
        verify: bool = False,
        force_rerun: bool = False,
        input_fn: Callable = input,
        print_fn: Callable = print,
        event_fn: Callable[[dict], None] | None = None,
        should_stop: "threading.Event | None" = None,
        output_root: Path | None = None,
        write_low_confidence: bool = False,
    ) -> None:
        self.task_dir = task_dir
        self.config = config
        self.max_rows = max_rows
        self.debug_fetch = debug_fetch
        self.skip_identification = skip_identification
        self.verify = verify
        self.force_rerun = force_rerun
        # 低置信度（待复核）行是否也写回 xlsx。默认 False = 保持原行为：
        # 不写回、只进 review_queue，等人工复核后再落盘。
        # 勾选后这些行的分数会随跑批一并写入副本，但仍标记为"待复核"。
        self.write_low_confidence = write_low_confidence
        self.input_fn = input_fn
        self.print_fn = print_fn
        self.event_fn = event_fn
        self.should_stop = should_stop
        # 阶段 A 构建后暴露给 GUI（确认弹窗要显示候选与参考代码）
        self.stdlib: StandardLibrary | None = None

        # 输出目录
        task_name = task_dir.name
        root = output_root if output_root is not None else Path("output")
        self.output_dir = root / task_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 明细日志路径：验证模式写独立文件，与生产结果隔离。
        # 原因：验证模式不做断点续跑（见阶段C），每轮都把全部行重评一遍并追加。
        # 若与生产 jsonl 共用，多轮调 prompt 回归会把生产文件撑到几十 MB，
        # 且两类结果混在一起——生产 jsonl 应当只反映正式跑批的评分。
        prod_detail = self.output_dir / config.output.detail_log
        if verify:
            # grading_detail.jsonl → grading_detail.verify.jsonl
            self.detail_log_path = prod_detail.with_suffix(".verify" + prod_detail.suffix)
            # 每轮验证从零开始，避免上一轮结果混入本轮报告
            try:
                if self.detail_log_path.exists():
                    self.detail_log_path.unlink()
            except Exception:
                pass
        else:
            self.detail_log_path = prod_detail
        # 生产 jsonl 路径始终可用：验证模式下 --skip-identification 要从它恢复题目映射
        self.prod_detail_log_path = prod_detail

        self.review_queue_path = self.output_dir / config.output.review_queue
        self.render_dumps_dir = self.output_dir / config.output.render_dumps_subdir
        # 启动时清空 render_dumps（清空重来）：避免跨次跑批的历史转储残留
        # 只清转储目录，不影响 JSONL/CSV/报告等跨次累积产物
        self._clear_render_dumps()

    def _clear_render_dumps(self) -> None:
        """清空 render_dumps 目录，避免跨次跑批的转储文件残留。

        默认模式只在抓取失败时写转储，清空后本次跑批的失败转储干净独立，便于排查。
        --debug-fetch 模式下清空后重新累积，不会和历史残留混淆。
        """
        try:
            if self.render_dumps_dir.exists():
                shutil.rmtree(self.render_dumps_dir)
            self.render_dumps_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # 清理失败不阻断主流程（转储是调试辅助）
            pass

    def _log(self, msg: str) -> None:
        self.print_fn(msg)

    def _emit(self, event_type: str, **payload: Any) -> None:
        """发结构化事件给 GUI。event_fn 未注入时（CLI）静默跳过。

        与 _log 并存：_log 面向人读的文本，_emit 面向程序消费的 dict。
        GUI 用 _emit 驱动进度条/统计，不必正则解析日志文本。
        """
        if self.event_fn is None:
            return
        try:
            self.event_fn({"type": event_type, **payload})
        except Exception:
            # GUI 回调异常不得影响跑批主流程
            pass

    def _stop_requested(self) -> bool:
        """GUI 是否请求停止。CLI 下 should_stop 为 None，恒返回 False。"""
        return self.should_stop is not None and self.should_stop.is_set()

    def run(self) -> RunReport:
        """执行阶段 A-D。"""
        # ── 阶段 A：启动准备 ──
        self._log("[阶段A] 启动准备")
        self._emit("phase", phase="A", label="启动准备")
        try:
            xlsx_path = _find_xlsx(self.task_dir)
            standards_map, ref_path = find_task_docxs(self.task_dir)
        except FileNotFoundError as e:
            self._log(f"❌ {e}")
            return self._fail_report()

        self._log(f"  xlsx: {xlsx_path.name}")
        for level in sorted(standards_map.keys(), key=level_sort_key):
            self._log(f"  {level}标准: {standards_map[level].name}")
        self._log(f"  参考代码: {ref_path.name}")

        # 读 xlsx。注意：读原始表拿行数据，但**写入目标是 output 下的 AI 评分副本**，
        # 原始表全程只读——它含真人评分，既是交付物也是一致性验证的基准。
        try:
            rows, layout, wb = read_rows(xlsx_path, skip_score=True)
        except ValueError as e:
            self._log(f"❌ xlsx 列定位失败: {e}")
            return self._fail_report()

        # 准备 AI 评分副本（verify 模式不写盘，故不需要）
        ai_xlsx: Path | None = None
        if not self.verify:
            ai_xlsx = ensure_ai_xlsx(xlsx_path, self.output_dir)
            # 续跑时应基于副本已有内容，而不是拿原始表覆盖回去
            try:
                _rows2, layout, wb = read_rows(ai_xlsx, skip_score=True)
            except ValueError as e:
                self._log(f"❌ AI 评分副本列定位失败: {e}")
                return self._fail_report()
            self._log(f"  AI 评分写入: {ai_xlsx}")
            self._log(f"  原始 xlsx 只读，不会被修改")
            if not self.write_low_confidence:
                # 只影响本次写回，不会清理副本里的既有内容：上次勾选跑批留下的分仍在，
                # 交付前需知道"空单元格 ≠ 一定未复核"。
                self._log("  低置信度行不写回（勾选「写入待复核评分」可改变）；"
                          "副本中这些行可能保留上次写入的分")

        # 收集题目ID
        task_ids = []
        seen = set()
        for r in rows:
            if r.task_id not in seen:
                seen.add(r.task_id)
                task_ids.append(r.task_id)
        self._log(f"  读取 {len(rows)} 行，{len(task_ids)} 个题目ID: {task_ids}")
        self._emit("xlsx_loaded", total_rows=len(rows), task_ids=list(task_ids),
                   xlsx_name=xlsx_path.name)

        # 构建标准库
        try:
            stdlib = StandardLibrary(standards_map, ref_path)
        except StandardLibraryError as e:
            self._log(f"❌ 标准库构建失败: {e}")
            return self._fail_report()
        gradable = stdlib.gradable_triples()
        self.stdlib = stdlib
        self._log(f"  构建标准库: {len(stdlib.all_triples())} 道参考题, {len(gradable)} 条可评标准")

        # 启动浏览器和 LLM（提前到冒烟之前：冒烟/识别共用同一个浏览器实例）
        fetcher = UrlFetcher(
            headless=self.config.playwright.headless,
            timeout_ms=self.config.playwright.timeout_ms,
            debug_dumps_dir=self.render_dumps_dir,  # 失败时始终写（dir 非空）
            always_dump=self.debug_fetch,
        )
        llm = make_llm(self.config.llm.provider, self.config.llm.model, self.config, self.config.llm.temperature)
        backend = LangChainBackend(llm, self.config)

        with fetcher:
            # 冒烟测试：每个题目ID 抽 1 个 URL 预抓取，验证连通性与页面结构。
            # 注意它先于阶段 B 执行，所以即便勾了「跳过题目识别」也会抓这几个 URL——
            # 这是有意的（早失败早报错，避免批量阶段才发现大面积抓取失败），
            # 但它只抓不评分、不调用 LLM、不弹窗。
            # 抓取结果缓存下来供阶段 B 识别复用，避免同一 URL 抓两次。
            smoke_cache = self._smoke_test(fetcher, rows, task_ids)
            if smoke_cache is None:
                return self._fail_report()

            # ── 阶段 B：题目识别 ──
            self._log("\n[阶段B] 题目识别")
            self._emit("phase", phase="B", label="题目识别")
            triple_map: dict[str, TaskTriple] = {}

            if self.skip_identification:
                # 从已有 JSONL 恢复。始终读**生产** jsonl：验证模式的 detail 文件
                # 每轮开跑前会被清空，从它恢复必然为空。题目映射与模式无关，
                # 复用生产跑批的识别结果即可。
                restore_src = self.prod_detail_log_path
                triple_str_map = read_triple_map_from_detail(restore_src)
                for tid, tstr in triple_str_map.items():
                    t = _parse_triple_str(tstr)
                    if t:
                        triple_map[tid] = t
                self._log(f"  --skip-identification: 从 JSONL 恢复 {len(triple_map)} 个映射")
                for tid, t in triple_map.items():
                    self._log(f"    {tid} → {t}")

                # 恢复不全时明确告警：否则会静默退回逐个识别（既花钱又弹窗），
                # 用户以为勾选没生效，实际是恢复源缺失。
                missing = [t for t in task_ids if t not in triple_map]
                if missing:
                    if not triple_map:
                        self._log(f"  ⚠ 未能从 {restore_src} 恢复任何映射")
                        self._log(f"    该文件不存在或为空——本任务此前没跑过，无历史可复用。")
                    else:
                        self._log(f"  ⚠ 有 {len(missing)} 个题目ID 无历史映射: {missing}")
                    self._log(f"    这些题目ID 仍需识别（会调用 LLM 并弹确认框）。")

            # 阶段 B：未覆盖的题目ID 做识别
            identifier = ProblemIdentifier(fetcher, backend, stdlib)
            for tid in task_ids:
                if tid in triple_map:
                    continue
                # 取该题目ID 的首行 URL
                first_row = next((r for r in rows if r.task_id == tid), None)
                if not first_row:
                    continue
                self._log(f"\n  处理题目ID={tid}")
                # 先识别（不交互），把题目描述发给 GUI 供确认弹窗显示，再走确认。
                # 冒烟已抓过该题目ID 的首行 URL 则直接复用，不再重抓。
                id_result, fetch_result = identifier.identify(
                    tid, first_row.url, cached_fetch=smoke_cache.get(tid),
                )
                self._emit("identify_ready", task_id=tid,
                           description=fetch_result.description or "",
                           code=fetch_result.code or "",
                           best_triple=str(id_result.best_triple),
                           confidence=id_result.confidence,
                           reasoning=id_result.reasoning,
                           fetch_ok=fetch_result.ok)
                confirmed = identifier.confirm_with_user(
                    tid, id_result, fetch_result, self.input_fn, self.print_fn,
                )
                if confirmed is None:
                    triple_map[tid] = None  # type: ignore[assignment]
                else:
                    triple_map[tid] = confirmed
                    self._log(f"  ✓ 已缓存: 题目ID {tid} → {confirmed}")

        # with 块结束：主 fetcher 关闭。阶段 C 的并发 worker 会各自自建浏览器。

        # ── 阶段 C：批量评分 ──
        self._log("\n[阶段C] 批量评分")
        self._emit("phase", phase="C", label="批量评分")
        state = _RunState()

        # 断点续跑：跳过 JSONL 中已完成的行。
        # 两种情况关闭：验证模式（每轮都要全量重评，否则对比的是老结果）；
        # force_rerun（用户显式要求忽略历史，重评全部目标行）。
        skip_resume = self.verify or self.force_rerun
        completed_rows = set() if skip_resume else read_completed_rows(self.prod_detail_log_path)
        if completed_rows:
            self._log(f"  断点续跑: 跳过 {len(completed_rows)} 个已完成行")
        elif self.force_rerun and not self.verify:
            self._log("  强制重跑: 忽略已完成行，全部目标行重新评分（会产生新的 LLM 调用费用）")

        target_rows = rows
        if self.max_rows is not None:
            target_rows = rows[:self.max_rows]
            self._log(f"  --max-rows: 仅处理前 {self.max_rows} 行")
        target_total = len(target_rows)

        # 分拣：已完成跳过；未识别题目ID直接进待复核；其余进待评分队列
        pending: list[TaskRow] = []
        for row in target_rows:
            if row.row_num in completed_rows:
                continue
            if triple_map.get(row.task_id) is None:
                state.review_rows.append({
                    "row_num": row.row_num, "answer_id": row.answer_id,
                    "task_id": row.task_id, "url": row.url,
                    "confidence": 0, "ai_total_score": "",
                    "reason": "identification_rejected", "reasoning": "题目ID未识别",
                })
                state.low_confidence_queued += 1
                continue
            pending.append(row)

        total = len(pending)
        self._emit("grading_start", total=total, skipped=len(completed_rows))
        if total == 0:
            self._log("  无待评分行")
        else:
            self._run_phase_c_concurrent(pending, triple_map, stdlib, wb, layout, ai_xlsx, state)

        # ── 阶段 D：收尾 ──
        self._log("\n[阶段D] 收尾")
        self._emit("phase", phase="D", label="收尾")
        output_xlsx_path = ai_xlsx if ai_xlsx is not None else xlsx_path
        if not self.verify and ai_xlsx is not None:
            try:
                output_xlsx_path = save_workbook(wb, ai_xlsx)
                self._log(f"  AI 评分 xlsx 已保存: {output_xlsx_path}")
            except Exception as e:
                self._log(f"  ❌ 保存 xlsx 失败: {e}")

        # 排序后导出待复核清单（并发下完成顺序不定，按行号排序保持整洁）
        state.review_rows.sort(key=lambda r: r["row_num"])
        write_review_queue(self.review_queue_path, state.review_rows)
        self._log(f"  待复核: {state.low_confidence_queued} 行 → {self.review_queue_path}")
        self._log(f"  评分明细: {self.detail_log_path}")

        report = RunReport(
            total_rows=target_total,
            fetched_ok=state.fetched_ok,
            fetch_failed=state.fetch_failed,
            high_confidence_written=state.high_confidence_written,
            low_confidence_queued=state.low_confidence_queued,
            detail_log_path=self.detail_log_path,
            review_queue_path=self.review_queue_path,
            output_xlsx_path=output_xlsx_path,
        )
        self._log(f"\n  已写回评分: {report.high_confidence_written} 行 (高置信度)")
        self._log(f"  待复核: {report.low_confidence_queued} 行")
        if state.low_confidence_written:
            self._log(f"  其中已写回: {state.low_confidence_written} 行 "
                      f"(低置信度，仍需人工复核)")
        self._log(f"  抓取失败: {report.fetch_failed} 行")
        self._emit("finished",
                   total_rows=report.total_rows,
                   fetched_ok=report.fetched_ok,
                   fetch_failed=report.fetch_failed,
                   high_confidence_written=report.high_confidence_written,
                   low_confidence_queued=report.low_confidence_queued,
                   detail_log_path=str(report.detail_log_path),
                   review_queue_path=str(report.review_queue_path),
                   output_xlsx_path=str(report.output_xlsx_path),
                   stopped=self._stop_requested())
        return report

    def _record_grade_result(
        self,
        row: TaskRow,
        result: GradeResult,
        fetch_result: FetchResult,
        state: _RunState,
        stdlib: StandardLibrary,
        wb,
        layout,
        done: int,
        total: int,
    ) -> None:
        """记录一行的评分结果：更新统计 + 写 JSONL + 写 xlsx 内存 + 发事件。

        只在主线程调用（并发版由结果收集循环调用），因此 state/wb 无需加锁。
        """
        if not fetch_result.ok:
            state.fetch_failed += 1
            state.fetch_failed_streak += 1
            state.llm_failed_streak = 0
            state.review_rows.append({
                "row_num": row.row_num, "answer_id": row.answer_id,
                "task_id": row.task_id, "url": row.url,
                "confidence": 0, "ai_total_score": "",
                "reason": "fetch_failed", "reasoning": fetch_result.error or "",
            })
            state.low_confidence_queued += 1
            self._log(f"  [{done}/{total}] 行{row.row_num} 题目ID={row.task_id} 抓取✗ → 留空")
            self._emit("row_done", index=done, total=total, row_num=row.row_num,
                       task_id=row.task_id, answer_id=row.answer_id,
                       outcome="fetch_failed", written=False,
                       total_score=None, confidence=0.0,
                       error=fetch_result.error or "",
                       stats=self._stats(state))
        elif not result.parse_ok:
            state.fetch_failed_streak = 0
            state.llm_failed_streak += 1
            state.review_rows.append({
                "row_num": row.row_num, "answer_id": row.answer_id,
                "task_id": row.task_id, "url": row.url,
                "confidence": result.confidence, "ai_total_score": result.total_score,
                "reason": "llm_parse_failed", "reasoning": result.reasoning,
            })
            state.low_confidence_queued += 1
            self._log(f"  [{done}/{total}] 行{row.row_num} 题目ID={row.task_id} LLM✗ → 留空")
            self._emit("row_done", index=done, total=total, row_num=row.row_num,
                       task_id=row.task_id, answer_id=row.answer_id,
                       outcome="llm_parse_failed", written=False,
                       total_score=None, confidence=result.confidence,
                       error=result.reasoning,
                       stats=self._stats(state))
        else:
            state.fetched_ok += 1
            state.fetch_failed_streak = 0
            state.llm_failed_streak = 0
            high = Grader.is_high_confidence(result, self.config.grading.confidence_threshold)
            # 取该题总分上限用于进度显示
            try:
                bundle_total_max = stdlib.get(result.triple).standard.total_max
            except KeyError:
                bundle_total_max = "?"
            # 写 JSONL（无论是否写回 xlsx，都记明细；含题目描述与学生代码供复核离线查看）
            write_grade_detail(
                self.detail_log_path, row, result, _timestamp(),
                description=fetch_result.description or "",
                student_code=fetch_result.code or "",
            )
            if high and not self.verify:
                update_cell_in_memory(wb.active, row.row_num, layout.score_col_idx, result.total_score)
                state.high_confidence_written += 1
                self._log(f"  [{done}/{total}] 行{row.row_num} 题目ID={row.task_id} "
                          f"评分: {result.total_score}/{bundle_total_max} (conf {result.confidence:.2f}) → 写回")
                _outcome, _written = "written", True
            elif high and self.verify:
                state.high_confidence_written += 1
                self._log(f"  [{done}/{total}] 行{row.row_num} 题目ID={row.task_id} "
                          f"评分: {result.total_score} (conf {result.confidence:.2f}) → [verify] 不写回")
                _outcome, _written = "verify_ok", False
            else:
                state.low_confidence_queued += 1
                # review_queue 记录始终保留：它才是复核工作清单，
                # 与"分数是否已写进副本"是两件事。
                state.review_rows.append({
                    "row_num": row.row_num, "answer_id": row.answer_id,
                    "task_id": row.task_id, "url": row.url,
                    "confidence": result.confidence, "ai_total_score": result.total_score,
                    "reason": "low_confidence", "reasoning": result.reasoning,
                })
                write_low = self.write_low_confidence and not self.verify
                if write_low:
                    update_cell_in_memory(wb.active, row.row_num,
                                          layout.score_col_idx, result.total_score)
                    state.low_confidence_written += 1
                tail = "待复核（已写回）" if write_low else "留空"
                self._log(f"  [{done}/{total}] 行{row.row_num} 题目ID={row.task_id} "
                          f"评分: {result.total_score} (conf {result.confidence:.2f}) → ⚠ {tail}")
                _outcome, _written = "low_confidence", write_low

            self._emit("row_done", index=done, total=total, row_num=row.row_num,
                       task_id=row.task_id, answer_id=row.answer_id,
                       outcome=_outcome, written=_written,
                       total_score=result.total_score,
                       confidence=result.confidence,
                       error="", stats=self._stats(state))

    def _run_phase_c_concurrent(
        self,
        pending: list[TaskRow],
        triple_map: dict[str, TaskTriple],
        stdlib: StandardLibrary,
        wb,
        layout,
        ai_xlsx: Path | None,
        state: _RunState,
    ) -> None:
        """阶段 C 并发执行：N 个 worker 线程并行评分，主线程收集结果并统一落盘。

        线程模型：
        - worker 线程：各自在本线程创建独立 UrlFetcher + LLM backend
          （Playwright 同步 API 绑死创建线程，严禁跨线程共享 fetcher）；
          只做"抓取+评分"，不碰任何共享状态。
        - 主线程（本方法调用方）：从 result_q 收结果，更新 state、写 JSONL、
          写 xlsx 内存、发事件、做熔断。因此 state/wb/JSONL 全程无需锁。
        """
        n_workers = max(1, min(self.config.grading.concurrency, len(pending)))
        total = len(pending)

        work_q: "_queue.Queue[TaskRow | None]" = _queue.Queue()
        for row in pending:
            work_q.put(row)
        for _ in range(n_workers):
            work_q.put(None)  # 每个 worker 一个哨兵，取到即退出

        result_q: "_queue.Queue[tuple[TaskRow, GradeResult, FetchResult]]" = _queue.Queue()
        stop_workers = threading.Event()

        def worker_loop(worker_id: int) -> None:
            fetcher = UrlFetcher(
                headless=self.config.playwright.headless,
                timeout_ms=self.config.playwright.timeout_ms,
                debug_dumps_dir=self.render_dumps_dir,
                always_dump=self.debug_fetch,
            )
            try:
                fetcher._init_browser()  # 必须在本线程内启动浏览器
                llm = make_llm(self.config.llm.provider, self.config.llm.model,
                               self.config, self.config.llm.temperature)
                backend = LangChainBackend(llm, self.config)
                grader = Grader(fetcher, backend, stdlib)
                while True:
                    if stop_workers.is_set() or self._stop_requested():
                        return
                    row = work_q.get()
                    if row is None:
                        return
                    triple = triple_map[row.task_id]
                    try:
                        result, fetch_result = grader.fetch_and_grade(
                            row.url, triple, row_id=str(row.row_num))
                    except Exception as exc:
                        # worker 级异常（如浏览器崩溃）：构造失败结果上交，不丢行。
                        # 该行计入 fetch_failed 并进待复核，与其他抓取失败同等处理。
                        from grading_agent.models import SyntaxPenalty
                        result = GradeResult(
                            triple=triple, dimension_scores=[],
                            syntax_penalty=SyntaxPenalty(error_categories=[], category_count=0, total_deduction=0),
                            subtotal=0, total_score=0,
                            reasoning=f"worker 异常: {type(exc).__name__}: {exc}",
                            confidence=0.0, parse_ok=False,
                        )
                        fetch_result = FetchResult(
                            description="", code="", raw_html=None, ok=False,
                            error=f"worker 异常: {type(exc).__name__}: {exc}",
                            row_id=str(row.row_num),
                        )
                    result_q.put((row, result, fetch_result))
            except Exception as exc:
                # 浏览器启动失败等致命异常：打日志并让 worker 退出；
                # 队列里剩余行由其他 worker 消化（或由主线程兜底标记失败）。
                self._log(f"  ⚠ worker-{worker_id} 启动失败: {type(exc).__name__}: {exc}")
            finally:
                fetcher.close()

        workers = [
            threading.Thread(target=worker_loop, args=(i,), daemon=True, name=f"grader-{i}")
            for i in range(n_workers)
        ]
        for t in workers:
            t.start()
        self._log(f"  并发模式: {n_workers} 个 worker（每 worker 独立浏览器+LLM 客户端）")

        # 主线程：收集结果
        done = 0
        aborted = False
        while done < total:
            if self._stop_requested():
                self._log("\n  ⚠ 已请求停止，等待在途任务完成后收尾")
                self._emit("stopped", at_index=done, total=total)
                stop_workers.set()
                break
            try:
                row, result, fetch_result = result_q.get(timeout=1.0)
            except _queue.Empty:
                if not any(t.is_alive() for t in workers):
                    # 所有 worker 已死但还有任务没结果——浏览器全军覆没（如 chromium 未安装）。
                    # 把剩余任务全部标记失败，避免主线程永久阻塞。
                    self._log("  ❌ 所有 worker 异常退出，剩余行标记为失败")
                    while True:
                        try:
                            row = work_q.get_nowait()
                        except _queue.Empty:
                            break
                        if row is None:
                            continue
                        done += 1
                        state.fetch_failed += 1
                        state.fetch_failed_streak += 1
                        state.review_rows.append({
                            "row_num": row.row_num, "answer_id": row.answer_id,
                            "task_id": row.task_id, "url": row.url,
                            "confidence": 0, "ai_total_score": "",
                            "reason": "fetch_failed",
                            "reasoning": "worker 浏览器启动失败",
                        })
                        state.low_confidence_queued += 1
                        self._emit("row_done", index=done, total=total, row_num=row.row_num,
                                   task_id=row.task_id, answer_id=row.answer_id,
                                   outcome="fetch_failed", written=False,
                                   total_score=None, confidence=0.0,
                                   error="worker 浏览器启动失败",
                                   stats=self._stats(state))
                    break
                continue

            done += 1
            self._record_grade_result(row, result, fetch_result, state, stdlib, wb, layout, done, total)

            # 定期 save xlsx（按已完成数计，与串行版"每 N 行"语义一致）
            if not self.verify and ai_xlsx is not None and done % self.config.grading.xlsx_save_interval == 0:
                try:
                    save_workbook(wb, ai_xlsx)
                except Exception as e:
                    self._log(f"  ⚠ 定期 save 失败: {e}")

            # 熔断检查（state 由主线程独占更新，无需锁）
            if self._check_circuit_breaker(state):
                self._log("  ⚠ 熔断触发，停止后续处理")
                stop_workers.set()
                aborted = True
                break

        # 收尾：等在途任务跑完并收集它们的结果——已完成的 LLM 调用已付费，不能丢弃
        for t in workers:
            t.join()
        while not result_q.empty():
            row, result, fetch_result = result_q.get_nowait()
            done += 1
            self._record_grade_result(row, result, fetch_result, state, stdlib, wb, layout, done, total)

        if aborted or self._stop_requested():
            remaining = total - done
            if remaining > 0:
                self._log(f"  未处理 {remaining} 行（已中断；重跑时断点续跑会跳过已完成行）")

    def _stats(self, state: "_RunState") -> dict:
        """当前累计统计快照，随每个 row_done 事件带给 GUI。"""
        return {
            "fetched_ok": state.fetched_ok,
            "fetch_failed": state.fetch_failed,
            "high_confidence_written": state.high_confidence_written,
            "low_confidence_queued": state.low_confidence_queued,
            # 低置信度也写回的行数。单独计数，不并入 high_confidence_written——
            # 否则"已写回"里会混进未经人工确认的分，读日志的人会误判。
            "low_confidence_written": state.low_confidence_written,
        }

    def _smoke_test(
        self,
        fetcher: UrlFetcher,
        rows: list[TaskRow],
        task_ids: list[str],
    ) -> "dict[str, FetchResult] | None":
        """阶段 A 冒烟测试：每个题目ID 抽 1 个 URL 预抓取，验证连通性。

        与主流程共用传入的 fetcher（不再自建浏览器，省一次 Chromium 启动）。
        成功时返回 {task_id: FetchResult} 缓存，供阶段 B 识别复用（避免同一 URL 抓两次）；
        任一失败返回 None（调用方中止）。无可测试 URL 时返回空 dict（视为通过）。
        """
        self._log("\n[冒烟测试] 预抓取验证连通性")
        sample_urls: list[tuple[str, str]] = []  # (task_id, url)
        for tid in task_ids[:3]:
            first = next((r for r in rows if r.task_id == tid), None)
            if first:
                sample_urls.append((tid, first.url))
        if not sample_urls:
            self._log("  无可测试 URL，跳过")
            return {}

        cache: dict[str, FetchResult] = {}
        for tid, url in sample_urls:
            result = fetcher.fetch(url, row_id=f"smoke_{tid}")
            if result.ok:
                self._log(f"  ✓ 题目ID={tid} URL 可访问，抓取成功")
                cache[tid] = result
            else:
                self._log(f"  ✗ 题目ID={tid} URL 抓取失败: {result.error}")
                self._log(f"    冒烟测试失败，请检查网络/URL有效性/页面结构后重试")
                return None
        return cache

    def _check_circuit_breaker(self, state: _RunState) -> bool:
        """熔断检查：连续 N 次同类失败触发。返回 True 表示应中止。"""
        cb = self.config.circuit_breaker
        if state.fetch_failed_streak >= cb.max_consecutive_fetch_failures:
            return self._handle_circuit("连续抓取失败", state)
        if state.llm_failed_streak >= cb.max_consecutive_llm_failures:
            return self._handle_circuit("连续 LLM 失败", state)
        return False

    def _handle_circuit(self, kind: str, state: _RunState) -> bool:
        """熔断处理：按 config.circuit_breaker.action 决定。"""
        cb = self.config.circuit_breaker
        if cb.action == "abort":
            return True
        if cb.action == "skip":
            self._log(f"  ⚠ {kind}触发熔断(action=skip)，自动跳过剩余")
            return True
        # prompt：询问用户。stdin 不可用（后台跑批）时降级为"继续"，避免崩溃丢失进度
        while True:
            try:
                raw = self.input_fn(f"  ⚠ {kind}触发熔断，选择 [c=继续 / a=中止 / s=跳过剩余]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                self._log(f"  ⚠ {kind}触发熔断，但 stdin 不可用（无人值守），自动选择继续并重置计数器")
                state.fetch_failed_streak = 0
                state.llm_failed_streak = 0
                return False
            if raw == "c":
                # 重置计数器继续
                state.fetch_failed_streak = 0
                state.llm_failed_streak = 0
                return False
            if raw == "a":
                return True
            if raw == "s":
                return True
            self._log("  无效输入")

    def _fail_report(self) -> RunReport:
        return RunReport(
            total_rows=0, fetched_ok=0, fetch_failed=0,
            high_confidence_written=0, low_confidence_queued=0,
            detail_log_path=self.detail_log_path,
            review_queue_path=self.review_queue_path,
            output_xlsx_path=Path(""),
        )


def _timestamp() -> str:
    """生成时间戳字符串。"""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
