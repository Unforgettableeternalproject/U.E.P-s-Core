"""SentenceSplitter——把 LLM 的 token 流切成可合成的句子。

## 這是整個管線最大的槓桿

不是「每一層都做成串流」，而是**在句子邊界讓各層重疊**。LLM 吐出第一句就
立刻送 TTS，LLM 生成第二句的時間被 TTS 合成第一句吃掉——兩層重疊而非相加。

## 為什麼是「切句子」而不是更細的粒度

調查過 CosyVoice 2/3、Orpheus、FlashTTS 這些宣稱 150-325ms 首包的方案後，
結論很明確：**它們能「不等句子邊界」是模型架構層級的能力，不是推論期的
wrapper。**

CosyVoice 2 的 chunk-aware causal flow matching 靠的是 text token 與
speech token 在訓練時就按約 5:15 比例交錯排列（text-speech interleaved LM），
所以文字還在進來時就能吐音訊。Orpheus 是 SNAC-frame-level 逐 token 解碼。
FlashTTS 是 lagged multi-track + MTP。三者都要求模型本身就是流式訓練的。

IndexTTS 2.5 是「先生成完整 semantic token 序列 → DiT + vocoder」的架構，
沒有交錯生成的基礎。換模型的代價遠高於收益（會丟掉情感向量系統這個既有資產）。

而且連專門研究更細切法的 Prosodic Boundary-Aware Streaming 論文，結論也是
「智能切分」而非「完全不切」。F5-TTS、XTTS 這些架構相近的模型，官方推薦
做法同樣是句子級 chunk streaming。

**所以句子級切分就是串接式管線的正解。** 可優化的是切點怎麼選，不是要不要切。

## 核心張力

**切太細，語調破碎；切太粗，TTFA 拉長。**

解法是首句與後續用不同閾值：首句只求快（越短越好，反正通常是承接語），
後續放寬求品質。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .contracts.cancellation import CancellationToken
from .contracts.types import Sentence

# --- 標點分類 ---

TERMINAL_PUNCT_FULLWIDTH = frozenset("。！？；…")
"""全形句末標點。全形標點不會出現在數字或縮寫裡，可以**立即切**，不需要 lookahead。"""

TERMINAL_PUNCT_ASCII = frozenset(".!?")
"""半形句末標點。有歧義（3.14、Mr.、U.S.A.），需要看下一個字元才能決定。"""

SECONDARY_PUNCT = frozenset("，、,:：")
"""次級標點。累積長度夠時可以當切點——這是「韻律邊界」的簡化版：
自然停頓處切分比在強母音區硬切，銜接假影小得多。"""

CLOSING_PUNCT = frozenset("」』）\")'】》>")
"""結尾引號括號。句末標點後面若接這些，要一起帶走再切。"""

BRACKET_PAIRS = {"《": "》", "〈": "〉", "「": "」", "『": "』", "【": "】",
                 "（": "）", "(": ")", "[": "]"}
"""成對括號。**括號內禁用次級切點**——書名《光與影：33號遠征隊》裡的
冒號是標題的一部分，切下去就是「《光與影：」這種殘句（2026-08-21 實測）。"""

_OPENING_BRACKETS = frozenset(BRACKET_PAIRS)
_CLOSING_BRACKETS = frozenset(BRACKET_PAIRS.values())


def bracket_depth(text: str) -> int:
    """未閉合的括號層數。LLM 忘記閉合時深度會卡住，所以只用來
    抑制**次級**切點——句末標點與強制切分不受它管，保證有出口。"""
    depth = 0
    for ch in text:
        if ch in _OPENING_BRACKETS:
            depth += 1
        elif ch in _CLOSING_BRACKETS and depth > 0:
            depth -= 1
    return depth

_NO_SPLIT_AFTER = frozenset("0123456789")
"""句末標點後面接數字 → 是小數點，不切。"""


def char_weight(ch: str) -> float:
    """字元的「發音時長權重」。

    切分閾值真正代表的是**合成後的音訊長度**，不是字元數。中文一個字約一個
    音節，英文要好幾個字母才一個音節。用同一組閾值套中英混合文字，
    不做換算就會讓英文句子被切得極短。

    權重取 CJK=1.0、拉丁字母=0.4（約 2.5 個字母 ≈ 一個中文字的時長），
    標點與空白 0。這是估算值，調參時要用聽感校正。
    """
    code = ord(ch)
    # CJK 統一表意文字 + 日文假名 + 韓文
    if 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF:
        return 1.0
    if ch.isalnum():
        return 0.4
    return 0.0


FLUSH = "\x00flush"
"""token 流裡的控制訊號：收到就把 buffer 立刻切成一句，**不看長度下限**。

給「filler → 工具往返 → 續寫」用：filler（「嗯，讓我想一下。」權重 6）不夠首句下限
時會被扣在 buffer 裡，等到第二段的 token 回來才放出——filler 就白講了
（2026-08-22 實測 TTFA 多吃一整段 round-trip）。think stage 在 filler 後送這個訊號。
內容是不可能出現在 LLM 輸出裡的 NUL 開頭字串，對既有的純文字消費端不可見。"""


def text_weight(text: str) -> float:
    """整段文字的發音時長權重。"""
    return sum(char_weight(c) for c in text)


@dataclass(slots=True)
class SplitPolicy:
    """切分參數。

    ## 預設值的由來（2026-08-20 實測 IndexTTS2 後修訂）

    量了三個首句長度，解出成本結構：

    | 首句長度 | 首段音訊時間 |
    |---------|------------|
    | 16 字   | 3411ms |
    |  9 字   | 2845ms |
    |  1 字   | 1788ms |

    → **每次合成有約 1.8 秒的固定開銷，外加每字約 80ms。**

    這個固定開銷改變了整個切分策略的最佳解，因為它是**按句累積**的：

    * **首句要短** — 固定開銷主導，1 字比 16 字快 1.6 秒。純賺 TTFA。
    * **後續句不能短** — 每多切一句就多付 1.8 秒。實測 3 句總合成 8.4 秒、
      音訊 8.5 秒，**RTF ≈ 0.99**，幾乎沒有餘裕：再切細一點，
      合成速度就跟不上播放速度，音訊會斷續。

    粗估（每字音訊約 0.33 秒）：

    | 句長 | 合成時間 | 音訊長度 | RTF |
    |------|---------|---------|-----|
    |  5 字 | 2.2s | 1.7s | 1.33 ✗ 會斷 |
    | 10 字 | 2.6s | 3.3s | 0.79 |
    | 20 字 | 3.4s | 6.6s | 0.52 |
    | 40 字 | 5.0s | 13.2s | 0.38 |

    所以後續句的下限拉到 20 字左右才安全。

    ⚠️ 這些數字是**在當前 IndexTTS2 實作下**測得的。導入 TensorRT 加速
    （端到端 3.46-3.60×）後固定開銷會降到 ~500ms，屆時整組參數要重調——
    後續句可以切得比現在細得多。

    調參時看 :attr:`Sentence.split_reason` 的分布，並**用耳朵驗證**：
    這裡只保證延遲數字，保證不了語調。
    """

    first_min_weight: float = 6.0
    """首句的最小長度。

    壓到 6 是因為固定開銷主導——首句從 16 字縮到 1 字省了 1.6 秒，
    而首句通常是承接語（「嗯，我想想」），切短對語調的傷害小。
    再往下壓（1-3 字）延遲收益已經很小，但語調會明顯破碎。"""

    first_max_weight: float = 14.0
    """首句的強制切分長度。首句拖長 = TTFA 直接受害。"""

    min_weight: float = 20.0
    """後續句子在次級標點處切分的最小長度。

    **不是「越小越好」**——每切一句就多付 1.8 秒固定開銷。
    低於 20 字，RTF 會接近或超過 1.0，合成追不上播放，音訊開始斷續。"""

    max_weight: float = 48.0
    """後續句子的強制切分長度。超過就算在句子中間也要切。

    上限存在的理由是「單句太長會讓後面全部延遲」，但在固定開銷這麼大的
    情況下，切得太積極反而更糟，所以放寬到 48。"""

    allow_secondary_split: bool = True
    """是否允許在逗號等次級標點切分。關掉可用來比較「只在句末切」的音質差異。"""

    hard_cut_grace: float = 8.0
    """**首句**超過上限後的寬限量：到達 first_max_weight **先不硬切**，再多等這麼多權重，
    期間出現任何標點就在標點切。硬切點落在詞中間（「…遊戲吸/引…」）的
    銜接假影遠比句子多幾個字難聽（2026-08-21 實測）。寬限也用完才真的硬切。
    後續句改用 :attr:`hard_cap_weight`。"""

    hard_cap_weight: float | None = None
    """**後續句**的絕對上限。``None`` 表示 2 × max_weight。

    權重超過 max_weight 之後，遇到任何標點（含括號內的次級標點）就立刻切；
    到這個上限還沒遇到標點才 ``_force_cut``。首句不套用，維持 hard_cut_grace
    （首句拖長直接傷 TTFA）。"""

    metadata: dict = field(default_factory=dict)


class SentenceSplitter:
    """把 token 流切成 :class:`Sentence` 流。

    用法::

        splitter = SentenceSplitter(policy)
        async for sentence in splitter.split(token_stream, turn_id, token):
            ...

    實例是**有狀態**的（buffer、句序號），一個 turn 用一個實例。
    """

    def __init__(self, policy: SplitPolicy | None = None) -> None:
        self.policy = policy or SplitPolicy()

    async def split(
        self,
        tokens: AsyncIterator[str],
        turn_id: str,
        token: CancellationToken | None = None,
    ) -> AsyncIterator[Sentence]:
        """消費 token 流，逐句產出。

        取消時直接停止產出——**不 flush 剩餘 buffer**。被 barge-in 中止時，
        使用者已經在講話了，把殘句補送去合成只會讓機器人多講半句廢話。
        """
        buffer = ""
        index = 0
        depth = 0
        """buffer 內未閉合的括號層數。逐字維護，切完重算。"""
        # 半形句末標點的 lookahead 狀態：記住「上次看到 ASCII 標點的位置」，
        # 等下一個字元進來才能判斷那是句末還是小數點
        pending_ascii_at = -1

        async for tok in tokens:
            if token is not None:
                token.raise_if_cancelled()
            if not tok:
                continue
            if tok == FLUSH:
                if buffer.strip():
                    yield self._make(buffer, turn_id, index, "flush")
                    index += 1
                buffer = ""
                depth = 0
                pending_ascii_at = -1
                continue

            for ch in tok:
                buffer += ch
                if ch in _OPENING_BRACKETS:
                    depth += 1
                elif ch in _CLOSING_BRACKETS and depth > 0:
                    depth -= 1

                # 1. 先處理待決的半形標點（已經看到下一個字元了）
                if pending_ascii_at >= 0:
                    # 這個 ch 就是標點後的下一個字元
                    if ch in _NO_SPLIT_AFTER or ch in TERMINAL_PUNCT_ASCII:
                        # 小數點或省略號 → 不是句末
                        pending_ascii_at = -1
                    else:
                        cut = pending_ascii_at + 1
                        # 把結尾引號一起帶走
                        while cut < len(buffer) and buffer[cut] in CLOSING_PUNCT:
                            cut += 1
                        head = buffer[:cut]
                        pending_ascii_at = -1
                        # 句末標點也要達最短長度——"Of course!" 這種短句
                        # 單獨合成會各吃一次 TTS 固定開銷，且語氣破碎。
                        # 不夠長就不切，併入下一句一起講。
                        floor = (
                            self.policy.first_min_weight
                            if index == 0
                            else self.policy.min_weight
                        )
                        if text_weight(head) >= floor:
                            buffer = buffer[cut:]
                            depth = bracket_depth(buffer)
                            if head.strip():
                                yield self._make(head, turn_id, index, "terminal")
                                index += 1
                            continue

                weight = text_weight(buffer)
                is_first = index == 0
                max_w = (
                    self.policy.first_max_weight if is_first else self.policy.max_weight
                )
                min_w = (
                    self.policy.first_min_weight if is_first else self.policy.min_weight
                )

                # 2. 全形句末標點：立即切（不會有小數點歧義），但同樣要達
                #    最短長度——太短就繼續累積，併入下一句
                if ch in TERMINAL_PUNCT_FULLWIDTH:
                    if weight >= min_w:
                        head, buffer = buffer, ""
                        depth = 0
                        if head.strip():
                            yield self._make(head, turn_id, index, "terminal")
                            index += 1
                    continue

                # 3. 半形句末標點：進入待決狀態，等下一個字元
                if ch in TERMINAL_PUNCT_ASCII:
                    pending_ascii_at = len(buffer) - 1
                    continue

                # 4. 次級標點 + 長度達標。**括號內不切**——書名、引文裡的
                #    冒號逗號是內容的一部分，切出去就是殘句
                if (
                    self.policy.allow_secondary_split
                    and ch in SECONDARY_PUNCT
                    and weight >= min_w
                    and (depth == 0 or (not is_first and weight >= max_w))
                ):
                    head, buffer = buffer, ""
                    depth = 0
                    if head.strip():
                        yield self._make(head, turn_id, index, "secondary")
                        index += 1
                    continue

                # 5. 長度上限。到達 max 不立刻硬切——硬切點多半落在詞中間，
                #    先等自然切點（上面的規則 2/4 會在標點處收掉）。首句等 grace，
                #    後續句等到 hard_cap（預設 2×max），用完才回溯硬切。
                if is_first:
                    cut_at = max_w + self.policy.hard_cut_grace
                else:
                    cap = self.policy.hard_cap_weight
                    cut_at = cap if cap is not None else 2.0 * max_w
                    cut_at = max(cut_at, max_w)
                if weight >= cut_at:
                    head, buffer = self._force_cut(buffer)
                    depth = bracket_depth(buffer)
                    if head.strip():
                        yield self._make(head, turn_id, index, "max_length")
                        index += 1

        # token 流正常結束 → flush 殘餘
        if token is not None and token.is_cancelled:
            return
        if buffer.strip():
            yield self._make(buffer, turn_id, index, "flush", is_last=True)
        elif index > 0:
            # 沒有殘餘，但要讓下游知道結束了。用零長度的收尾句標記。
            yield self._make("", turn_id, index, "flush", is_last=True)

    def _force_cut(self, buffer: str) -> tuple[str, str]:
        """長度超限時的切點選擇。

        優先回溯到最近的次級標點或空白處——**在自然停頓處切分，銜接假影
        遠小於在強母音中間硬切**。回溯範圍限制在尾端 40%，避免切出過短的頭段。
        """
        limit = int(len(buffer) * 0.6)
        for i in range(len(buffer) - 1, limit, -1):
            if buffer[i] in SECONDARY_PUNCT or buffer[i].isspace():
                return buffer[: i + 1], buffer[i + 1 :]
        return buffer, ""

    @staticmethod
    def _make(
        text: str,
        turn_id: str,
        index: int,
        reason: str,
        is_last: bool = False,
    ) -> Sentence:
        return Sentence(
            text=text.strip(),
            turn_id=turn_id,
            index=index,
            is_first=index == 0,
            is_last=is_last,
            split_reason=reason,
        )
