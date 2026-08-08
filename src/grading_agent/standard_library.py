"""标准库构建与索引：组合 docx_parser + reference_codes，按三元组索引。

构建时校验 standards 与 reference_codes 的 key 集合（降级策略，见 PRD §6.5）：
- reference_codes 为基准——文件夹里实际有几段参考代码，本次任务就处理几个三元组
- 某三元组仅在 standards 中存在而 reference_codes 中没有 → 报错退出
- 某三元组仅在 reference_codes 中存在而 standards 中没有 → 警告，该题跳过评分
- 双方都有的三元组正常处理
"""
from __future__ import annotations

import sys
from pathlib import Path

from grading_agent.models import StandardBundle, StandardEntry, TaskTriple
from grading_agent.docx_parser import parse_level1_standards, parse_level2_standards
from grading_agent.reference_codes import parse_reference_codes


class StandardLibraryError(Exception):
    """标准库构建错误（缺参考代码等致命情况）。"""


class StandardLibrary:
    """进程内标准库：三元组索引 standards + reference_codes。"""

    def __init__(self, level1_docx: Path, level2_docx: Path, ref_codes_docx: Path) -> None:
        self.standards: dict[TaskTriple, StandardEntry] = {}
        self.reference_codes: dict[TaskTriple, str] = {}
        self._triples_without_standard: set[TaskTriple] = set()  # 有参考代码但无评分标准
        self._build(level1_docx, level2_docx, ref_codes_docx)

    def _build(self, level1_docx: Path, level2_docx: Path, ref_codes_docx: Path) -> None:
        # 解析评分标准
        level1 = parse_level1_standards(level1_docx)
        level2 = parse_level2_standards(level2_docx)
        for e in level1 + level2:
            self.standards[e.triple] = e

        # 解析参考代码
        self.reference_codes = parse_reference_codes(ref_codes_docx)

        # 校验 key 集合
        std_keys = set(self.standards.keys())
        ref_keys = set(self.reference_codes.keys())

        only_std = std_keys - ref_keys
        only_ref = ref_keys - std_keys

        # 致命：有评分标准但缺参考代码（可能文件放错或解析错误）
        if only_std:
            triples_str = ", ".join(sorted(str(t) for t in only_std))
            raise StandardLibraryError(
                f"以下三元组有评分标准但缺参考代码，疑似文件放错或解析错误：{triples_str}"
            )

        # 警告：有参考代码但缺评分标准（该题跳过评分）
        if only_ref:
            triples_str = ", ".join(sorted(str(t) for t in only_ref))
            print(
                f"⚠ 警告：以下三元组有参考代码但缺评分标准，将跳过评分：{triples_str}",
                file=sys.stderr,
            )
            self._triples_without_standard = only_ref

    def get(self, triple: TaskTriple) -> StandardBundle:
        """返回该题的标准答案代码 + 评分标准。

        若该题无评分标准（仅有参考代码），抛 KeyError。
        """
        if triple not in self.standards:
            raise KeyError(f"三元组 {triple} 无评分标准（在 _triples_without_standard 中）")
        if triple not in self.reference_codes:
            raise KeyError(f"三元组 {triple} 无参考代码")
        return StandardBundle(
            triple=triple,
            standard=self.standards[triple],
            reference_code=self.reference_codes[triple],
        )

    def all_triples(self) -> list[TaskTriple]:
        """返回全部可识别+评分的三元组（即 reference_codes 的 key，按稳定顺序排序）。"""
        # 排序：级别（一级<二级）、场次（上午<下午）、题号
        level_order = {"一级": 0, "二级": 1}
        session_order = {"上午": 0, "下午": 1}
        return sorted(
            self.reference_codes.keys(),
            key=lambda t: (level_order.get(t.level, 9),
                          session_order.get(t.session, 9),
                          t.question),
        )

    def gradable_triples(self) -> list[TaskTriple]:
        """返回可评分的三元组（既有参考代码又有评分标准）。"""
        std_keys = set(self.standards.keys())
        return [t for t in self.all_triples() if t in std_keys]

    def candidate_summary_for_llm(self) -> str:
        """生成候选题概览，纯文本列表格式，喂识别 prompt。

        格式：
            候选1: 一级/上午/题1
              参考代码: int d; cin>>d; cout<<d*8; ...
            候选2: ...
        """
        lines: list[str] = []
        triples = self.all_triples()
        for i, triple in enumerate(triples, start=1):
            code = self.reference_codes.get(triple, "")
            # 参考代码前 15 行
            code_lines = code.splitlines()[:15]
            code_preview = "\n".join(code_lines)
            lines.append(f"候选{i}: {triple}")
            lines.append(f"  参考代码:")
            for cl in code_lines:
                lines.append(f"    {cl}")
        return "\n".join(lines)

    def has_standard(self, triple: TaskTriple) -> bool:
        """该三元组是否有评分标准。"""
        return triple in self.standards
