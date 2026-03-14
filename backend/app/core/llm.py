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


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "ep-20260306210052-mbrnw"


def load_llm_config() -> LLMConfig:
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

    api_key = (os.getenv("ARK_API_KEY") or "").strip().strip('"')
    base_url = (os.getenv("ARK_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.getenv("ARK_MODEL") or DEFAULT_MODEL).strip()
    return LLMConfig(api_key=api_key, base_url=base_url, model=model)


def build_chat_llm(temperature: float = 0.0) -> ChatOpenAI:
    cfg = load_llm_config()
    if not cfg.api_key:
        raise RuntimeError("ARK_API_KEY is not configured")

    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=temperature,
    )
