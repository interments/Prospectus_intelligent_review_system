from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float
    max_retries: int


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "ep-20260306210052-mbrnw"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 2


def load_llm_config() -> LLMConfig:
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

    api_key = (os.getenv("ARK_API_KEY") or "").strip().strip('"')
    base_url = (os.getenv("ARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.getenv("ARK_MODEL") or DEFAULT_MODEL).strip()

    timeout_raw = (os.getenv("ARK_TIMEOUT_SECONDS") or str(DEFAULT_TIMEOUT_SECONDS)).strip()
    retries_raw = (os.getenv("ARK_MAX_RETRIES") or str(DEFAULT_MAX_RETRIES)).strip()
    try:
        timeout_seconds = float(timeout_raw)
    except Exception:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    try:
        max_retries = int(retries_raw)
    except Exception:
        max_retries = DEFAULT_MAX_RETRIES

    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if max_retries < 0:
        max_retries = DEFAULT_MAX_RETRIES

    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def build_chat_llm(temperature: float = 0.0) -> ChatOpenAI:
    cfg = load_llm_config()
    if not cfg.api_key:
        raise RuntimeError("ARK_API_KEY is not configured")

    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=temperature,
        timeout=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
    )
