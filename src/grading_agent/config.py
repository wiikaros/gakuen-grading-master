"""配置加载：合并 config/default.yaml 与环境变量覆盖。

环境变量优先级高于 yaml（便于 CI/临时切换模型而不改文件）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class PlaywrightConfig:
    headless: bool = True
    timeout_ms: int = 20000
    wait_until: str = "networkidle"


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    temperature: float = 0.0
    max_retries: int = 3


@dataclass
class GradingConfig:
    confidence_threshold: float = 0.7
    write_back: bool = True
    max_score: int = 25
    xlsx_save_interval: int = 50


@dataclass
class CircuitBreakerConfig:
    max_consecutive_fetch_failures: int = 10
    max_consecutive_llm_failures: int = 5
    action: str = "prompt"  # prompt / skip / abort


@dataclass
class OutputConfig:
    detail_log: str = "grading_detail.jsonl"
    review_queue: str = "review_queue.csv"
    render_dumps_subdir: str = "render_dumps"
    render_dumps_on_failure_only: bool = True


@dataclass
class Config:
    playwright: PlaywrightConfig = field(default_factory=PlaywrightConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    grading: GradingConfig = field(default_factory=GradingConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    # API keys（从环境变量读，不放 yaml）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    openai_api_key: str = ""
    anthropic_api_key: str = ""


def _deep_update(base: dict, override: dict) -> dict:
    """递归合并 override 到 base。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖配置。环境变量优先级最高。"""
    if v := os.getenv("GRADING_MODEL_PROVIDER"):
        cfg.llm.provider = v
    if v := os.getenv("GRADING_MODEL_NAME"):
        cfg.llm.model = v
    if v := os.getenv("GRADING_CONFIDENCE_THRESHOLD"):
        try:
            cfg.grading.confidence_threshold = float(v)
        except ValueError:
            pass
    if v := os.getenv("GRADING_PLAYWRIGHT_TIMEOUT_MS"):
        try:
            cfg.playwright.timeout_ms = int(v)
        except ValueError:
            pass
    if v := os.getenv("GRADING_DEBUG_FETCH"):
        cfg.output.render_dumps_on_failure_only = v.lower() != "true"

    # API keys
    cfg.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
    cfg.deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", cfg.deepseek_base_url)
    cfg.openai_api_key = os.getenv("OPENAI_API_KEY", "")
    cfg.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")


def load_config(config_path: Path | None = None, dotenv_path: Path | None = None) -> Config:
    """加载配置：先读 yaml 默认值，再用环境变量覆盖。

    config_path 默认为仓库根下的 config/default.yaml。
    dotenv_path 默认为仓库根下的 .env（若存在则加载）。
    """
    if dotenv_path is None:
        # 向上查找 .env：从 config/default.yaml 所在目录的父目录
        dotenv_path = Path.cwd() / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)

    cfg = Config()

    if config_path is None:
        # 默认配置文件位置：仓库根/config/default.yaml
        # 本文件位于 src/grading_agent/config.py，向上两级是仓库根
        repo_root = Path(__file__).resolve().parents[2]
        config_path = repo_root / "config" / "default.yaml"

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        if data:
            if pw := data.get("playwright"):
                cfg.playwright = PlaywrightConfig(**{**PlaywrightConfig().__dict__, **pw})
            if llm := data.get("llm"):
                cfg.llm = LLMConfig(**{**LLMConfig().__dict__, **llm})
            if g := data.get("grading"):
                cfg.grading = GradingConfig(**{**GradingConfig().__dict__, **g})
            if cb := data.get("circuit_breaker"):
                cfg.circuit_breaker = CircuitBreakerConfig(**{**CircuitBreakerConfig().__dict__, **cb})
            if out := data.get("output"):
                cfg.output = OutputConfig(**{**OutputConfig().__dict__, **out})

    _apply_env_overrides(cfg)
    return cfg
