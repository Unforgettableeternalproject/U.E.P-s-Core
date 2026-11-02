"""Console `/llm`：以 fake stage 驗證指令層（不打真實 API、不需 PostgreSQL）。"""
from __future__ import annotations

from uep.console import llm_cmd
from uep.stream.adapters.think_config import ThinkConfig, ThinkConfigError
from uep.stream.adapters.think_llm import LLMThinkStage


class _Backend:
    async def stream_query(self, messages, system_instruction=None, **kwargs):
        for piece in ("你好呀。", "我是回聲，", "很高興見到你。"):
            yield piece


def _default_factory(cfg, prompt, tracer):
    return LLMThinkStage(_Backend(), system_prompt=prompt, tracer=tracer, config=cfg)


def _run(arg, *, loader=None, factory=None):
    lines: list[str] = []
    llm_cmd.run_llm(
        arg, lines.append,
        config_loader=loader or (lambda: (ThinkConfig(api_key="k"), "人設")),
        stage_factory=factory or _default_factory,
    )
    return "\n".join(lines)


def test_印出切句與耗時():
    text = _run("你好")
    assert "[0] +" in text and "你好呀。" in text and "首句" in text and "總耗時" in text
    assert "provider=openai" in text


def test_無參數印用法():
    assert llm_cmd.USAGE in _run("  ")


def test_缺金鑰清楚報錯():
    def factory(cfg, prompt, tracer):
        cfg.require_credentials()

    text = _run("你好", loader=lambda: (ThinkConfig(), ""), factory=factory)
    assert "LLM 設定錯誤" in text and "UEP_LLM_API_KEY" in text


def test_後端失敗不中止():
    def factory(cfg, prompt, tracer):
        raise RuntimeError("boom")

    assert "LLM 後端建立失敗" in _run("你好", factory=factory)


def test_stream_失敗顯示原因():
    class Bad:
        async def stream(self, utterance, token):
            raise ThinkConfigError("壞了")
            yield  # pragma: no cover

    assert "LLM 呼叫失敗" in _run("你好", factory=lambda cfg, prompt, tracer: Bad())
