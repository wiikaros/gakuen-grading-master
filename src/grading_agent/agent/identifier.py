"""题目识别：LLM 1-of-N 分类 + CLI 逐个确认。

PRD §4 阶段 B：每个新题目ID 抓取首行 URL → LLM 分类 → CLI 打印候选+置信度 → 用户确认 → 缓存。
"""
from __future__ import annotations

from grading_agent.models import (
    FetchResult,
    IdentificationResult,
    TaskTriple,
)
from grading_agent.standard_library import StandardLibrary
from grading_agent.llm.base import LLMBackend
from grading_agent.url_fetcher import UrlFetcher


class ProblemIdentifier:
    """题目识别器：对每个新题目ID 做一次 LLM 分类 + CLI 确认。"""

    def __init__(self, fetcher: UrlFetcher, llm: LLMBackend, stdlib: StandardLibrary) -> None:
        self.fetcher = fetcher
        self.llm = llm
        self.stdlib = stdlib

    def identify(
        self,
        task_id: str,
        url: str,
        cached_fetch: FetchResult | None = None,
    ) -> tuple[IdentificationResult, FetchResult]:
        """抓取 URL + LLM 分类，返回 (识别结果, 抓取结果)。

        cached_fetch：冒烟测试已成功抓取的复用结果（ok=True 才复用），
        避免同一 URL 在冒烟和识别阶段被抓两次。None 或抓取失败的缓存照常重抓。
        """
        if cached_fetch is not None and cached_fetch.ok:
            fetch_result = cached_fetch
        else:
            fetch_result = self.fetcher.fetch(url, row_id=f"identify_{task_id}")
        if not fetch_result.ok:
            triples = self.stdlib.all_triples()
            fallback = triples[0] if triples else TaskTriple(level="未定", session="上午", question=1)
            return IdentificationResult(
                best_triple=fallback,
                confidence=0.0,
                candidates=[],
                reasoning=f"抓取失败: {fetch_result.error}",
                parse_ok=False,
            ), fetch_result

        # 候选编号与 candidate_summary_for_llm 同序（都来自 all_triples 的稳定排序）
        triples = self.stdlib.all_triples()
        candidates_summary = self.stdlib.candidate_summary_for_llm()
        result = self.llm.classify_problem(
            description=fetch_result.description,
            code=fetch_result.code,
            candidates_summary=candidates_summary,
            candidates=triples,
        )
        return result, fetch_result

    def confirm_with_user(
        self,
        task_id: str,
        result: IdentificationResult,
        fetch_result: FetchResult,
        input_fn=input,
        print_fn=print,
    ) -> TaskTriple | None:
        """CLI 交互：打印候选+置信度+参考代码片段，等用户确认。

        用户输入：
        - 'y' → 确认推荐（best_triple）
        - 'n' → 拒绝，返回 None（该题目ID 标记未识别）
        - 数字 1-N → 选其他候选（按候选列表序号，N=候选总数）

        返回确认的三元组，或 None（用户拒绝）。
        input_fn/print_fn 可注入，便于测试。
        """
        if not result.parse_ok:
            print_fn(f"\n  题目ID={task_id} 识别失败: {result.reasoning}")
            print_fn("  无法自动识别，请手动选择或拒绝。")

        triples = self.stdlib.all_triples()

        print_fn(f"\n  抓取题目ID={task_id} 的首行URL ... {'✓' if fetch_result.ok else '✗'}")
        if fetch_result.ok:
            print_fn(f"  LLM 识别结果:")
            print_fn(f"    最佳候选: {result.best_triple}  (置信度 {result.confidence:.2f})", )
            if result.confidence < 0.7:
                print_fn("    ⚠ 低置信")
            print_fn(f"    理由: {result.reasoning}")
            # 参考代码片段
            try:
                bundle = self.stdlib.get(result.best_triple)
                code_lines = bundle.reference_code.splitlines()[:10]
                print_fn("  参考代码片段:")
                for cl in code_lines:
                    print_fn(f"    {cl}")
            except KeyError:
                pass

        # 候选列表
        print_fn("  候选列表:")
        for i, triple in enumerate(triples, start=1):
            mark = " ← 推荐" if triple == result.best_triple else ""
            print_fn(f"    {i}. {triple}{mark}")

        while True:
            try:
                raw = input_fn(f"  确认 [y=确认推荐 / n=拒绝 / 1-{len(triples)}=选其他候选]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # stdin 不可用（后台无人值守跑批）：高置信度自动确认，低置信度拒绝
                if result.parse_ok and result.confidence >= 0.9:
                    print_fn(f"  (stdin 不可用，置信度 {result.confidence:.2f} >= 0.9，自动确认推荐)")
                    return result.best_triple
                print_fn(f"  (stdin 不可用，置信度 {result.confidence:.2f} < 0.9，跳过该题目ID)")
                return None
            if raw == "y":
                return result.best_triple
            if raw == "n":
                print_fn(f"  ✗ 题目ID {task_id} 未识别，所有该题目ID行将跳过评分")
                return None
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(triples):
                    chosen = triples[idx - 1]
                    print_fn(f"  ✓ 已选择: {chosen}")
                    return chosen
            print_fn(f"  无效输入，请输入 y/n 或 1-{len(triples)}")

    def identify_and_confirm(
        self,
        task_id: str,
        url: str,
        input_fn=input,
        print_fn=print,
    ) -> tuple[TaskTriple | None, IdentificationResult, FetchResult]:
        """一步完成：识别 + 确认。返回 (确认的三元组或None, 识别结果, 抓取结果)。"""
        result, fetch_result = self.identify(task_id, url)
        confirmed = self.confirm_with_user(task_id, result, fetch_result, input_fn, print_fn)
        return confirmed, result, fetch_result
