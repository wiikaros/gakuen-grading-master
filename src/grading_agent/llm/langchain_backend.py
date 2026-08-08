"""LangChain 后端实现 + 多模型工厂。

PRD §6.6:
- make_llm 工厂：deepseek/openai/claude 三选一，temperature=0
- with_structured_output 强制结构化输出
- 降级方案：with_structured_output 报错时回退 JsonOutputParser + 校验

桥接：Pydantic 输出层（英文枚举）↔ 内部 dataclass（中文枚举），见 models.py 转换函数。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from grading_agent.config import Config
from grading_agent.models import (
    GradeResult,
    IdentificationResult,
    StandardBundle,
    TaskTriple,
    LEVEL_MAP,
    SESSION_MAP,
    get_llm_output_models,
    identification_output_to_result,
    grade_output_to_result,
)
from grading_agent.llm.prompts import (
    build_grade_prompt,
    build_identification_prompt,
    format_dimensions_block,
)


def make_llm(provider: str, model: str, config: Config, temperature: float = 0.0):
    """LangChain ChatModel 工厂。

    - 'deepseek' → ChatOpenAI(model, base_url=DEEPSEEK_BASE_URL, api_key=DEEPSEEK_API_KEY)
    - 'openai'   → ChatOpenAI(model)
    - 'claude'   → ChatAnthropic(model)
    temperature=0 保证评分可复现。
    """
    if provider == "deepseek":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            base_url=config.deepseek_base_url,
            api_key=config.deepseek_api_key,
            temperature=temperature,
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=config.openai_api_key,
            temperature=temperature,
        )
    elif provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=config.anthropic_api_key,
            temperature=temperature,
        )
    else:
        raise ValueError(f"未知 provider: {provider}，支持 deepseek/openai/claude")


class LangChainBackend:
    """LangChain 实现：with_structured_output + JSON 降级。"""

    def __init__(self, llm, config: Config) -> None:
        self.llm = llm
        self.config = config
        self._max_retries = config.llm.max_retries

    def _call_with_retry(self, fn):
        """带指数退避的重试包装。1s, 2s, 4s，最多 max_retries 次。"""
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    sleep_s = 2 ** attempt  # 1, 2, 4
                    time.sleep(sleep_s)
        raise last_exc  # type: ignore[misc]

    def classify_problem(
        self,
        description: str,
        code: str,
        candidates_summary: str,
    ) -> IdentificationResult:
        """题目识别：LLM 1-of-N 分类。

        DeepSeek 走 json_mode（function_calling 在 thinking 模型上不可用），
        要求 prompt 含 "json" 关键词（prompts.py 已注入）。
        """
        IdentificationOutput, _, _ = get_llm_output_models()
        prompt = build_identification_prompt(description, code, candidates_summary)

        try:
            structured = self.llm.with_structured_output(IdentificationOutput, method="json_mode")
            out = self._call_with_retry(lambda: structured.invoke(prompt))
            return identification_output_to_result(out)
        except Exception:
            # 降级：纯文本 + JSON 解析
            return self._classify_fallback(prompt)

    def _classify_fallback(self, prompt: str) -> IdentificationResult:
        """JSON 降级方案：prompt 追加"输出严格JSON"，用 JsonOutputParser。"""
        from langchain_core.output_parsers import JsonOutputParser
        fallback_prompt = prompt + "\n\n请输出严格 JSON（不要 markdown 代码块），格式：\n" + json.dumps({
            "level": "L1 或 L2",
            "session": "morning 或 afternoon",
            "question": 1,
            "confidence": 0.0,
            "reasoning": "",
        }, ensure_ascii=False)
        try:
            parser = JsonOutputParser()
            out_dict = self._call_with_retry(lambda: parser.parse(self.llm.invoke(fallback_prompt).content))
            IdentificationOutput, _, _ = get_llm_output_models()
            out = IdentificationOutput(**out_dict)
            return identification_output_to_result(out)
        except Exception as exc:
            return IdentificationResult(
                best_triple=TaskTriple(level="一级", session="上午", question=1),  # type: ignore[arg-type]
                confidence=0.0,
                candidates=[],
                reasoning=f"LLM 解析失败: {exc}",
                parse_ok=False,
            )

    def grade_answer(
        self,
        bundle: StandardBundle,
        description: str,
        student_code: str,
    ) -> GradeResult:
        """评分：结构化输出 + 降级。"""
        _, GradeOutput, _ = get_llm_output_models()
        prompt = build_grade_prompt(
            triple=str(bundle.triple),
            total_max=bundle.standard.total_max,
            dimensions_block=format_dimensions_block(bundle.standard.dimensions),
            special_notes=bundle.standard.special_notes,
            syntax_penalty_rule=bundle.standard.syntax_penalty_rule,
            reference_code=bundle.reference_code,
            description=description,
            student_code=student_code,
        )

        try:
            structured = self.llm.with_structured_output(GradeOutput, method="json_mode")
            out = self._call_with_retry(lambda: structured.invoke(prompt))
            return grade_output_to_result(out, bundle.triple)
        except Exception:
            return self._grade_fallback(prompt, bundle.triple)

    def _grade_fallback(self, prompt: str, triple: TaskTriple) -> GradeResult:
        """评分 JSON 降级方案。"""
        from langchain_core.output_parsers import JsonOutputParser
        from grading_agent.models import DimensionScore, SyntaxPenalty
        fallback_prompt = prompt + "\n\n请输出严格 JSON（不要 markdown 代码块），字段：dimension_scores(列表,每项含name/awarded/max_score/reasoning), error_categories(列表), total_deduction, subtotal, total_score, reasoning, confidence"
        try:
            parser = JsonOutputParser()
            out_dict = self._call_with_retry(lambda: parser.parse(self.llm.invoke(fallback_prompt).content))
            _, GradeOutput, _ = get_llm_output_models()
            out = GradeOutput(**out_dict)
            return grade_output_to_result(out, triple)
        except Exception as exc:
            return GradeResult(
                triple=triple,
                dimension_scores=[],
                syntax_penalty=SyntaxPenalty(error_categories=[], category_count=0, total_deduction=0),
                subtotal=0,
                total_score=0,
                reasoning=f"LLM 解析失败: {exc}",
                confidence=0.0,
                parse_ok=False,
            )
