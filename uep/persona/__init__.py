"""U.E.P 人設：受版控的預設 system prompt（``persona.yaml``）。

``UEP_INFER_SYSTEM_PROMPT`` 留空時以此為預設；環境變數有值時以環境變數為準（見 ``uep.config``）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PERSONA_FILE = Path(__file__).with_name("persona.yaml")


@lru_cache(maxsize=1)
def load_persona() -> dict:
    """讀取 ``persona.yaml``（只讀一次）。"""
    with PERSONA_FILE.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def default_system_prompt() -> str:
    """依 ``compose`` 串接人設段落；``{system_values}`` 佔位在此移除（情緒值尚未接線）。"""
    data = load_persona()
    parts = [str(data[name]).replace("{system_values}", "").strip() for name in data.get("compose", [])]
    return "\n\n".join(p for p in parts if p)


__all__ = ["PERSONA_FILE", "default_system_prompt", "load_persona"]
