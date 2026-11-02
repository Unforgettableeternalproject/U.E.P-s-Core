"""GapFiller——使用者講完到第一段真音訊之間的「墊片音」（Phase 4b D 段）。

## 為什麼

TTFA 的兩個大頭（LLM 純網路往返、TTS 每次合成的固定開銷）都是程式碼救不了的，
真實場景 8-15s、新會話更糟。**絕對延遲壓不下去，就只剩感知延遲可以動。**
Vapi / Retell 那類 voice agent 的標配：STT 一完就先出「嗯…」，使用者感覺對方
已經在回應，而不是乾等。

## 設計決策（艾斯維爾裁決）

1. **排隊，不打斷**（2026-08-23）。墊片音寫進 sink 之後，真音訊接在它後面——
   它就是這輪 TTS 輸出的一部分。中途切掉反而突兀。代價是真音訊可能晚墊片音的
   長度才播，所以墊片要短（「嗯…」一兩秒）。
2. **即時合成，不預建語音庫**（2026-08-24，推翻 08-23 的預建方案）。
   預建庫在啟動 / 改風格時要把三語文案全部合成一遍，IndexTTS2 每句 ~2s 固定
   開銷，啟動時間直接翻倍；且連續大量合成疑似誘發 CUDA device-side assert
   （lite_engine 的 emotion tensor 建立處浮出，實際觸發點更早）。改成 STT 剛
   結束、LLM 還沒回來的空窗才合成**這一輪要出的那一句**。GPU 競爭的代價
   （合成期間佔 TTS，真首句可能排隊）艾斯維爾明確接受——反正這段空窗 TTS
   本來就閒著。
3. **一輪最多一聲**（2026-08-23）。墊片播完真音訊還沒接上，也不補第二聲——
   補了反而怪（「嗯…嗯……」像結巴）。寧可留一段安靜。
4. **只排隊、不搶位**：真音訊到達前就決定出不出聲。runner 在寫第一段真音訊
   **之前** :meth:`GapFiller.stop`——還在合成就直接放棄（一個 chunk 都沒寫，
   不存在倒序）；已經開始寫就等它寫完、之後不再追加。sink 裡絕不會出現
   「真音訊 → 墊片音」的倒序，真音訊也絕不等墊片合成。

## 邊界

這裡不決定「說什麼」——文案全部來自設定檔；也不合成——合成走 SpeakStage
（fake / IndexTTS2 都行，它看不到差別）。core 不 import 任何子系統。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .contracts.cancellation import CancellationToken, CancelReason
from .contracts.style import SpeechStyle
from .contracts.types import AudioChunk, Sentence

logger = logging.getLogger(__name__)

GAP_SENTENCE_INDEX = -1
"""墊片音的 chunk 掛在這個 sentence_index 上——它不對應任何句子，
barge-in 推算 spoken_text 時要把它排除。"""


@dataclass(slots=True)
class GapFillerPolicy:
    """墊片音策略。預設值見 ``echo_stream.toml`` 的 ``[gap_filler]``。"""

    enabled: bool = False
    start_delay_s: float = 0.4
    """STT 完成後先停多久才出聲。人類接話也不是零間隔，太快反而假。"""
    phrases: dict[str, list[str]] = field(
        default_factory=lambda: {
            "zh": ["嗯…", "嗯，讓我想想。"],
            "en": ["Hmm…", "Let me think."],
            "ja": ["えっと…", "うーん…"],
        }
    )
    """候選文案，**按語言分組**（key 是 STT 給的語言碼：zh / en / ja…）。
    使用者講英文就不能「嗯」中文——艾斯維爾 2026-08-23。每輪從該語言的池子
    隨機挑一句、不連續重複；該語言沒池子就退回 ``default_language``。
    **零語意**——「對」「是啊」這種在問句後面會變成回答，不能用；
    「我不記得了」是可被推翻的記憶斷言，也不能用。"""
    default_language: str = "zh"
    """STT 沒給語言（fake / 偵測失敗）或該語言沒池子時用哪一組。"""


class GapFiller:
    """在 turn 的空檔期即時合成一句墊片音、排進 sink。

    生命週期：:meth:`attach`（管線就緒後給 SpeakStage）→ 每個 turn
    :meth:`start` → 真音訊到達前 :meth:`stop`。
    """

    def __init__(self, policy: GapFillerPolicy | None = None) -> None:
        self.policy = policy or GapFillerPolicy()
        self.speak_stage: Any = None
        self.style_getter: Callable[[], SpeechStyle | None] | None = None
        """每次合成時取當下的語音風格——面板改了語速/情緒，下一聲直接生效，
        不需要任何重建。"""
        self._last_pick: str | None = None
        """上次挑到的文案——不連續重複。"""
        self._task: asyncio.Task | None = None
        self._synthesis_token: CancellationToken | None = None
        self._stopped = asyncio.Event()
        self._writing = False
        """已開始把 chunk 寫進 sink——過了這個點 stop() 只能等寫完，不能取消。"""
        self.emitted_duration_s = 0.0
        """這一輪排進 sink 的墊片音總長。runner 推算 spoken_text 時要扣掉。"""
        self.last_emitted: str | None = None
        """這一輪出了哪一聲——turn meta / session log 用。"""

    # --- 掛載 ---

    def attach(
        self,
        speak_stage: Any,
        style_getter: Callable[[], SpeechStyle | None] | None = None,
    ) -> None:
        """給 GapFiller 合成用的 SpeakStage（管線就緒後呼叫一次）。"""
        self.speak_stage = speak_stage
        self.style_getter = style_getter

    @property
    def ready(self) -> bool:
        return self.speak_stage is not None

    def phrases_for(self, language: str | None) -> list[str]:
        """某語言的池子。``zh-TW`` → ``zh``；沒有就退回預設語言。"""
        code = (language or "").split("-")[0].split("_")[0].lower()
        pools = self.policy.phrases
        pool = pools.get(code) if code else None
        if not pool:
            pool = pools.get(self.policy.default_language) or next(iter(pools.values()), [])
        return [t for t in pool if t.strip()]

    def _pick_text(self, language: str | None) -> str | None:
        pool = self.phrases_for(language)
        if not pool:
            return None
        if len(pool) > 1 and self._last_pick in pool:
            pool = [t for t in pool if t != self._last_pick]
        text = random.choice(pool)
        self._last_pick = text
        return text

    async def _synthesize(
        self, speak_stage: Any, text: str, style: SpeechStyle | None, token: CancellationToken
    ) -> list[AudioChunk]:
        turn_id = f"gap-{abs(hash(text)) % 10**8:08x}"

        async def one() -> AsyncIterator[Sentence]:
            yield Sentence(
                text=text, turn_id=turn_id, index=0, is_first=True, is_last=True,
                split_reason="gap", style=style,
            )

        chunks: list[AudioChunk] = []
        async for chunk in speak_stage.stream(one(), token):
            chunks.append(chunk)
        return chunks

    # --- 每輪 ---

    def start(
        self,
        sink: Any,
        turn_id: str,
        token: CancellationToken,
        on_first_audio=None,
        language: str | None = None,
    ) -> None:
        """開始排墊片音（背景 task）。``language`` 是 STT 偵測到的語言碼，決定用哪組
        文案；``on_first_audio`` 在墊片寫入 sink 時呼叫（打點用）。"""
        self.emitted_duration_s = 0.0
        self.last_emitted = None
        self._stopped = asyncio.Event()
        self._writing = False
        self._task = None
        if not self.policy.enabled or self.speak_stage is None or sink is None:
            return
        synthesis_token = token.child()
        self._synthesis_token = synthesis_token
        self._task = asyncio.ensure_future(
            self._run(sink, turn_id, token, synthesis_token, on_first_audio, language)
        )
        self._task.add_done_callback(
            lambda _, child=synthesis_token: self._release_synthesis_token(child)
        )

    def _release_synthesis_token(self, token: CancellationToken | None) -> None:
        if token is None:
            return
        if self._synthesis_token is token:
            self._synthesis_token = None
        token.detach()

    async def stop(self) -> None:
        """真音訊來了（或 turn 結束）：不再追加。

        還在等待/合成 → 取消墊片並立即返回，一個 chunk 都不寫；同步
        ``generate()`` 若已進模型，後續 TTS 工作須等它退出才能取得模型所有權；
        已在寫 sink → 等正在寫的寫完。runner 必須在寫第一段真音訊之前
        await 這個——這是 sink 不倒序的保證。
        """
        self._stopped.set()
        synthesis_token = self._synthesis_token
        if synthesis_token is not None and not self._writing:
            synthesis_token.cancel(CancelReason.SUPERSEDED, "墊片已過期")
        self._release_synthesis_token(synthesis_token)
        task, self._task = self._task, None
        if task is None:
            return
        if not self._writing:
            # task.cancel 不會終止 executor thread；子 token 已通知 callback，
            # stop 只等 async 消費端退出，模型鎖會由 adapter 持有到 worker 結束。
            task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task

    async def _run(
        self,
        sink: Any,
        turn_id: str,
        token: CancellationToken,
        synthesis_token: CancellationToken,
        on_first_audio,
        language: str | None,
    ) -> None:
        if await self._wait(self.policy.start_delay_s):
            return  # 真音訊比墊片還快——不出聲，這是最好的情況
        if self._stopped.is_set() or token.is_cancelled:
            return
        text = self._pick_text(language)
        if not text:
            return
        style = self.style_getter() if self.style_getter is not None else None
        try:
            chunks = await self._synthesize(self.speak_stage, text, style, synthesis_token)
        except asyncio.CancelledError:
            raise  # stop() 取消的——正常路徑
        except Exception as exc:  # noqa: BLE001 - 墊片是加分項，失敗只是這輪安靜
            logger.warning(
                "gap filler 合成失敗，這輪不出聲 %r：%s: %s",
                text, type(exc).__name__, exc,
            )
            return
        if not chunks or self._stopped.is_set() or token.is_cancelled:
            return
        # stop() 的「set → 檢查 _writing → cancel」中間沒有 await，
        # 在事件迴圈上是原子的——要嘛它看到 _writing 等我們寫完，
        # 要嘛我們看到 _stopped 一個 chunk 都不寫。不存在寫到一半被砍
        self._writing = True
        self.last_emitted = text
        for i, src in enumerate(chunks):
            chunk = AudioChunk(
                pcm=src.pcm, sample_rate=src.sample_rate, turn_id=turn_id,
                sentence_index=GAP_SENTENCE_INDEX, chunk_index=i, channels=src.channels,
            )
            await sink.write(chunk)
            self.emitted_duration_s += chunk.duration_s
        if on_first_audio is not None:
            on_first_audio()

    async def _wait(self, seconds: float) -> bool:
        """等 seconds；期間被 stop 就回 True。"""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=max(0.0, seconds))
            return True
        return False


__all__ = ["GapFiller", "GapFillerPolicy", "GAP_SENTENCE_INDEX"]
