"""Think adapter 的設定載體（N3）。

adapter 不讀環境變數與 ``.env``：設定由 ``uep.config.UepSettings``（``UEP_LLM_*``）解析後，
經 ``uep/echo/inference_wiring.py`` 或 console 以本 dataclass 注入。``uep/stream/`` 不得 import
``uep`` 其他部分，所以解析與舊名相容（``ECHO_STREAM_*``／``OPENAI_*``）留在 ``uep/config.py``。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ThinkConfigError(RuntimeError):
    """Think 設定不完整（例如缺少 LLM 金鑰）；訊息為繁體中文且不含金鑰。"""


@dataclass(frozen=True)
class ThinkConfig:
    provider: str = "openai"
    model: str = "gpt-5.6-luna"
    api: str = "chat"
    reasoning_effort: str = "high"
    max_output_tokens: int = 1024
    temperature: float | None = None
    api_key: str | None = field(default=None, repr=False)
    project: str | None = None
    organization: str | None = None
    base_url: str | None = None
    prompt_cache_key: str | None = None
    output_guard_enabled: bool = True
    output_blocklist_extra: tuple[str, ...] = ()

    def backend_options(self) -> dict[str, object]:
        """交給 ``BackendRegistry.create`` 的 provider 設定（略過 None）。"""
        cfg: dict[str, object] = {
            "model": self.model,
            "api_key": self.api_key,
            "project": self.project,
            "organization": self.organization,
            "base_url": self.base_url,
            "api": self.api,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }
        return {k: v for k, v in cfg.items() if v is not None}

    def require_credentials(self) -> None:
        """openai 需要金鑰；缺少時明確報錯（只提變數名，不印任何值）。其他 provider 由其自身驗證。"""
        if self.provider == "openai" and not self.api_key:
            raise ThinkConfigError(
                "缺少 LLM 金鑰：請設定環境變數 UEP_LLM_API_KEY，或寫入 Core repo 根的 .env"
                "（舊名 OPENAI_API_KEY 仍相容）。"
            )


__all__ = ["ThinkConfig", "ThinkConfigError"]
