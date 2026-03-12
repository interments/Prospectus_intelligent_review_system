from __future__ import annotations

import os
from openai import OpenAI


def build_ark_client() -> tuple[OpenAI, str]:
    base_url = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    api_key = os.environ.get("ARK_API_KEY")
    model = os.environ.get("ARK_MODEL", "ep-20260306210052-mbrnw")
    if not api_key:
        raise ValueError("ARK_API_KEY is missing. Please export it before running.")
    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model
