"""IndexTTS 2.5 八向量兩兩組合掃描（艾斯維爾 2026-08-19 指定）。

參考音固定 B 組 = slice_04（yapping 4:02 起 15 秒）。
C(8,2) = 28 組合 × 3 語言 = 84 檔，每組配一句專屬複合語氣台詞。

強度：起始分量中英 0.5、日文 0.65，再經 clamp 壓到語言別目標總和。

⚠️ clamp 是必要的：上游 infer_v2_5 的 normalize_emo_vec() 只被 webui.py 呼叫，
   走 tts.infer() 這條路徑「不會」做任何 clamp（我們的 lite_engine.py:1151
   normalize_vector 有做，但這支腳本直接打上游，繞過了它）。未 clamp 的話
   0.5+0.5=1.0 會整個灌進模型，音色被訓練集說話人特徵蓋掉。

clamp 語意比照上游 normalize_emo_vec：先套 emo_bias 去強調容易出怪聲的情緒，
再等比縮放到目標總和——只砍總量、保留分量間比例。

語言別目標總和：中英 0.6、日文 0.8。
   為何不是同一個值：等分量組合壓到同一總和後結果會完全一樣（ZH 1.0→0.8 與
   JA 1.3→0.8 都變 [0.4,0.4]），日文加成會歸零。改成中英 0.6 / 日文 0.8，
   維持 1.33x 的相對加成（≈ 原本 0.65/0.5），且兩者都不超過上游建議上限 0.8。

情感向量順序：[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]

用法：
    python experiments/v25_emotion_combos.py            # 全跑（已存在的跳過）
    python experiments/v25_emotion_combos.py --lang ZH  # 只跑單一語言
    python experiments/v25_emotion_combos.py --force    # 覆寫既有檔
"""
import argparse
import itertools
import json
import os
import sys
import time

# Windows 終端預設 cp950，中文台詞會亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UPSTREAM_REPO = r"C:\Users\Bernie\source\repos\Other\index-tts\index-tts"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT_DIR = os.path.join(PROJECT_ROOT, "echo_tts", "modules", "tts_module", "checkpoints_v25")
SLICE_DIR = os.path.join(PROJECT_ROOT, "outputs", "yapping_slices")
OUT_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_combos")

REF_AUDIO = os.path.join(SLICE_DIR, "slice_04_at242s.wav")  # B 組

sys.path.insert(0, UPSTREAM_REPO)

import transformers.pytorch_utils as _pu

if not hasattr(_pu, "is_torch_greater_or_equal_than_1_13"):
    _pu.is_torch_greater_or_equal_than_1_13 = True

# 八向量在 emo_vector 中的索引
EMO_ORDER = ["happy", "angry", "sad", "afraid",
             "disgusted", "melancholic", "surprised", "calm"]

# 起始分量強度（clamp 前）。單軸與組合分開設：
# 組合有兩個分量，總能量天生是單軸的兩倍，壓不住就會蓋掉音色（實測不 clamp
# 的組合基本上塌回原始語料）；單軸只有一個分量，可以放寬。
STRENGTH = {"ZH": 0.5, "EN": 0.5, "JA": 0.65}          # 組合用
SINGLE_STRENGTH = {"ZH": 0.8, "EN": 0.8, "JA": 1.0}    # 單軸用（艾斯維爾 2026-08-19）

# clamp 後的總和上限（上游建議 0.8）
TARGET_SUM = {"ZH": 0.6, "EN": 0.6, "JA": 0.8}         # 組合用
SINGLE_TARGET_SUM = {"ZH": 0.8, "EN": 0.8, "JA": 0.8}  # 單軸用，放寬到上游上限

# 上游 normalize_emo_vec 的 bias：去強調容易產生怪聲的情緒
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMO_BIAS = [0.9375, 0.875, 1.0, 1.0, 0.9375, 0.9375, 0.6875, 0.5625]

# 28 組合各自的專屬複合台詞（中／英／日語意對應）
COMBO_TEXTS = {
    ("happy", "angry"): {
        "ZH": "哈，你看吧！我早就说过会成功了——你还不信我，真是的！",
        "EN": "Ha! See? I told you it would work — and you didn't believe me. Honestly!",
        "JA": "ほら、やっぱり成功したじゃない！信じてくれなかったくせに、もう！",
    },
    ("happy", "sad"): {
        "ZH": "太好了……终于完成了。我，我居然有点想哭，明明该高兴的……",
        "EN": "It's done... we actually did it. I... I feel like crying, even though I should be happy...",
        "JA": "よかった……やっと終わった。うれしいはずなのに、なんだか泣きそう……",
    },
    ("happy", "afraid"): {
        "ZH": "成功了！可是……真的没问题吗？我有点不敢相信，会不会出什么差错？",
        "EN": "It worked! But... is it really fine? I'm almost afraid to believe it. What if something breaks?",
        "JA": "できた！でも……本当に大丈夫かな。信じるのが、ちょっと怖い……",
    },
    ("happy", "disgusted"): {
        "ZH": "哈哈哈，这代码……居然能跑？写得这么烂居然能跑，我真是服了。",
        "EN": "Hahaha, this code... actually runs? It's hideous, and it runs. Unbelievable.",
        "JA": "ははは、このコード……動くの？ひどい書き方なのに動くなんて、呆れた。",
    },
    ("happy", "melancholic"): {
        "ZH": "还记得吗？我们第一次跑通的时候……那时候真好啊，现在想起来还会笑。",
        "EN": "Remember our first successful run? Those were good days. It still makes me smile.",
        "JA": "覚えてる？初めて動いた時のこと……あの頃はよかったな。今でも笑っちゃう。",
    },
    ("happy", "surprised"): {
        "ZH": "咦？等一下——它真的通过了？哇，我完全没想到！太厉害了吧！",
        "EN": "Wait — it actually passed? Whoa, I did not see that coming! That's incredible!",
        "JA": "えっ、ちょっと待って——本当に通ったの？うわぁ、全然思わなかった！",
    },
    ("happy", "calm"): {
        "ZH": "嗯，都整理好了。今天过得很顺利，这样就很好了。",
        "EN": "Everything's in order. Today went smoothly. This is enough, and it's good.",
        "JA": "うん、全部片付いた。今日は順調だったね。これで十分だよ。",
    },
    ("angry", "sad"): {
        "ZH": "我明明都做到了……为什么还要这样说我？我真的，真的很努力了。",
        "EN": "I did everything I was asked... so why say that to me? I tried. I really tried.",
        "JA": "ちゃんとやったのに……どうしてそんなこと言うの？わたし、頑張ったのに。",
    },
    ("angry", "afraid"): {
        "ZH": "别、别碰那个开关！你不知道会发生什么——住手，我说真的！",
        "EN": "Don't — don't touch that switch! You have no idea what it does. Stop, I'm serious!",
        "JA": "だ、駄目！そのスイッチに触らないで！何が起きるか分からないの、やめて！",
    },
    ("angry", "disgusted"): {
        "ZH": "又是这种借口。听都不想听，你自己不觉得难堪吗？",
        "EN": "That excuse again. I don't even want to hear it. Doesn't it embarrass you?",
        "JA": "またその言い訳？聞きたくもない。自分で恥ずかしくないの？",
    },
    ("angry", "melancholic"): {
        "ZH": "算了……我不想吵了。每次都这样，我已经累了。",
        "EN": "Forget it... I'm done arguing. It's always like this, and I'm tired.",
        "JA": "もういい……言い争いたくない。いつもこうなんだから、疲れた。",
    },
    ("angry", "surprised"): {
        "ZH": "什么？你把它删掉了？！你在开玩笑吧——怎么可以这样！",
        "EN": "What? You deleted it?! You have to be joking — how could you do that!",
        "JA": "え？消したの？！冗談でしょ——なんでそんなことするの！",
    },
    ("angry", "calm"): {
        "ZH": "我说最后一次。把手拿开，然后离开这里。现在。",
        "EN": "I'll say this once more. Take your hand off it, and leave. Now.",
        "JA": "もう一度だけ言う。手を離して、ここから出て行って。今すぐ。",
    },
    ("sad", "afraid"): {
        "ZH": "我……我是不是又搞砸了？对不起，我不知道该怎么办了……",
        "EN": "Did I... did I ruin it again? I'm sorry. I don't know what to do anymore...",
        "JA": "わたし……また失敗しちゃった？ごめんなさい、どうしたらいいか分からない……",
    },
    ("sad", "disgusted"): {
        "ZH": "我讨厌这样的自己……明明什么都做不好，还在这里找借口。",
        "EN": "I hate myself like this... I can't do anything right, and still I make excuses.",
        "JA": "こんな自分が嫌い……何もできないくせに、言い訳ばかりして。",
    },
    ("sad", "melancholic"): {
        "ZH": "那些日子……再也回不来了吧。我还留着当时的记录，可是……",
        "EN": "Those days... they're not coming back, are they. I still keep the old logs, but...",
        "JA": "あの日々は……もう戻らないんだね。記録はまだ残してるけど……",
    },
    ("sad", "surprised"): {
        "ZH": "什么……不见了？可是我昨天还……怎么会，怎么会这样……",
        "EN": "What... it's gone? But it was there yesterday... How? How did this happen...",
        "JA": "え……消えてる？昨日まであったのに……どうして、そんな……",
    },
    ("sad", "calm"): {
        "ZH": "嗯，我知道了。虽然有点难过，但我可以接受。谢谢你告诉我。",
        "EN": "I see. It hurts a little, but I can accept it. Thank you for telling me.",
        "JA": "うん、分かった。少し寂しいけど、受け入れられる。教えてくれてありがとう。",
    },
    ("afraid", "disgusted"): {
        "ZH": "那是什么东西……别让它靠近我，拜托，我不想看到它。",
        "EN": "What is that thing... Don't let it near me. Please, I don't want to look at it.",
        "JA": "何、あれ……近づけないで。お願い、見たくないの。",
    },
    ("afraid", "melancholic"): {
        "ZH": "我一直在想，如果有一天你不再需要我了……我会变成什么呢。",
        "EN": "I keep wondering... if one day you don't need me anymore, what becomes of me.",
        "JA": "ずっと考えてるの。いつかあなたに必要とされなくなったら、わたしはどうなるのかな。",
    },
    ("afraid", "surprised"): {
        "ZH": "呀！什么声音——那边有东西在动，你听到了吗？",
        "EN": "Ah! What was that — something moved over there. Did you hear it?",
        "JA": "きゃっ！今の音——あそこで何か動いた。聞こえた？",
    },
    ("afraid", "calm"): {
        "ZH": "没事的，我不怕。只要你在，我就……我就没事。",
        "EN": "It's all right. I'm not afraid. As long as you're here, I'm... I'm fine.",
        "JA": "大丈夫、怖くないよ。あなたがいてくれるなら、わたしは……平気。",
    },
    ("disgusted", "melancholic"): {
        "ZH": "又是同样的错误。看着这些日志，我连生气的力气都没有了。",
        "EN": "The same errors again. Looking at these logs, I don't even have the energy to be angry.",
        "JA": "また同じエラー。このログを見てると、怒る気力も湧かない。",
    },
    ("disgusted", "surprised"): {
        "ZH": "等等，这是什么？……你确定这东西是能用的吗？也太糟了吧。",
        "EN": "Hold on, what is this? ...Are you sure this is usable? It's appalling.",
        "JA": "ちょっと待って、これ何？……本当に使えるの？ひどすぎるでしょ。",
    },
    ("disgusted", "calm"): {
        "ZH": "不用了，我不需要这个。请把它拿走吧。",
        "EN": "No, thank you. I don't need this. Please take it away.",
        "JA": "結構です。それは必要ありません。持って行ってください。",
    },
    ("melancholic", "surprised"): {
        "ZH": "啊……原来已经过了这么久了。我都没有察觉，时间过得真快。",
        "EN": "Ah... it's been that long already? I hadn't noticed. Time moves so quickly.",
        "JA": "あ……もうそんなに経ってたんだ。気づかなかった。時間って早いね。",
    },
    ("melancholic", "calm"): {
        "ZH": "外面又下雨了。这种时候，总会想起一些很久以前的事。",
        "EN": "It's raining again outside. On days like this, old memories come back quietly.",
        "JA": "また雨が降ってる。こういう日は、昔のことを思い出すんだ。",
    },
    ("surprised", "calm"): {
        "ZH": "哦？原来是这样啊。我倒是没想过，不过说得也有道理。",
        "EN": "Oh? So that's how it is. I hadn't considered it, but it does make sense.",
        "JA": "へえ、そうなんだ。考えたことなかったけど、確かに一理あるね。",
    },
}

COMBOS = list(itertools.combinations(EMO_ORDER, 2))

# calm 專屬 bias（艾斯維爾 2026-08-19 聽感：其他七個齊平 OK，只有 calm 要壓回去）。
# 套用時機是 normalize 之後——套在前面會被 normalize 整個抵銷。
CALM_BIAS = 0.5625  # 沿用上游 EMO_BIAS 的 calm 係數

# 混合情緒的主副方向。多數組合只有一個方向在語意上站得住，
# 少數（喜極而泣 / 委屈的憤怒）兩個方向各是一種情緒，都值得聽。
# 依 COMBO_TEXTS 的台詞語意判定，value 為 [(主, 副), ...]。
COMBO_DIRECTIONS = {
    ("happy", "angry"):        [("happy", "angry")],          # 得意的嗔怪
    ("happy", "sad"):          [("happy", "sad"),             # 笑中帶淚
                                ("sad", "happy")],            # 淚中帶笑
    ("happy", "afraid"):       [("happy", "afraid")],         # 興奮中的不安
    ("happy", "disgusted"):    [("happy", "disgusted")],      # 嫌棄的笑
    ("happy", "melancholic"):  [("melancholic", "happy")],    # 懷念的微笑
    ("happy", "surprised"):    [("surprised", "happy")],      # 驚喜
    ("happy", "calm"):         [("calm", "happy")],           # 溫和的滿足
    ("angry", "sad"):          [("angry", "sad"),             # 帶委屈的怒
                                ("sad", "angry")],            # 委屈到發火
    ("angry", "afraid"):       [("afraid", "angry")],         # 恐懼驅動的怒吼
    ("angry", "disgusted"):    [("angry", "disgusted")],      # 厭惡的斥責
    ("angry", "melancholic"):  [("melancholic", "angry")],    # 疲憊的怒意
    ("angry", "surprised"):    [("surprised", "angry")],      # 錯愕的怒
    ("angry", "calm"):         [("calm", "angry")],           # 冷怒
    ("sad", "afraid"):         [("sad", "afraid")],           # 無助
    ("sad", "disgusted"):      [("sad", "disgusted")],        # 自我厭惡
    ("sad", "melancholic"):    [("melancholic", "sad")],      # 深沉的哀傷
    ("sad", "surprised"):      [("surprised", "sad")],        # 震驚的悲傷
    ("sad", "calm"):           [("calm", "sad")],             # 釋然的悲傷
    ("afraid", "disgusted"):   [("afraid", "disgusted")],     # 恐懼的排斥
    ("afraid", "melancholic"): [("melancholic", "afraid")],   # 不安的憂鬱
    ("afraid", "surprised"):   [("surprised", "afraid")],     # 驚嚇
    ("afraid", "calm"):        [("calm", "afraid")],          # 強作鎮定
    ("disgusted", "melancholic"): [("melancholic", "disgusted")],  # 厭倦
    ("disgusted", "surprised"):   [("surprised", "disgusted")],    # 驚訝的嫌惡
    ("disgusted", "calm"):        [("calm", "disgusted")],         # 冷淡的拒絕
    ("melancholic", "surprised"): [("melancholic", "surprised")],  # 恍然的惆悵
    ("melancholic", "calm"):      [("melancholic", "calm")],       # 靜謐的懷想
    ("surprised", "calm"):        [("calm", "surprised")],         # 平靜的訝異
}

# 單軸八向量的專屬台詞。happy / sad 刻意沿用 v25_final 的文本，
# 讓同文本同參考音的新舊兩批可直接對照（差別只在有無套 EMO_BIAS）。
SINGLE_TEXTS = {
    "happy": {
        "ZH": "哈哈，你猜怎么着？新模型的测试一次就通过了！我们赶快把它装上去试试吧！",
        "EN": "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!",
        "JA": "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？",
    },
    "angry": {
        "ZH": "我说过多少次了？不要随便动那些设定！你到底有没有在听我说话！",
        "EN": "How many times have I said it? Don't touch those settings! Are you even listening to me?",
        "JA": "何回言えば分かるの？その設定を勝手にいじらないで！ちゃんと聞いてるの？",
    },
    "sad": {
        "ZH": "我...对不起……我把之前的存档弄丢了，明明答应过你要好好保管的……",
        "EN": "I...I'm sorry... I lost the backup files. I promised I would keep them safe, and I let you down...",
        "JA": "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……",
    },
    "afraid": {
        "ZH": "等一下，那个声音是从哪里来的？我不敢过去看……你能陪我一起吗？",
        "EN": "Wait, where is that sound coming from? I don't dare go and look... Will you come with me?",
        "JA": "待って、あの音どこから？見に行くのが怖い……一緒に来てくれる？",
    },
    "disgusted": {
        "ZH": "这是什么啊……又黏又臭的，我一秒都不想碰它，快拿开。",
        "EN": "What even is this... sticky and reeking. I don't want to touch it for a second. Take it away.",
        "JA": "何これ……ベタベタして臭いし、一秒も触りたくない。早くどけて。",
    },
    "melancholic": {
        "ZH": "又是一个人的夜晚啊。窗外的灯还亮着，可是好像跟我没什么关系。",
        "EN": "Another night alone. The lights outside are still on, but none of them are for me.",
        "JA": "また一人の夜か。窓の外の灯りはまだついてるけど、わたしには関係ないみたい。",
    },
    "surprised": {
        "ZH": "咦？！这、这怎么可能——它什么时候变成这样的？我完全没注意到！",
        "EN": "Huh?! Th-that can't be — when did it turn into this? I didn't notice at all!",
        "JA": "えっ？！そ、そんな——いつの間にこうなったの？全然気づかなかった！",
    },
    "calm": {
        "ZH": "别急，慢慢来就好。我们还有时间，一件一件处理就可以了。",
        "EN": "There's no rush. Take your time. We still have time; we'll handle it one piece at a time.",
        "JA": "焦らなくていいよ、ゆっくりで。時間はまだあるから、一つずつ片付けよう。",
    },
}


def make_vector(emos: tuple[str, ...], strength: float,
                ratio: float | None = None) -> list[float]:
    """把 1~2 個情緒分量填進 8 維向量，其餘為 0。

    emos 為 (主, 副) 時依 ratio 分配：主 = strength*ratio、副 = strength*(1-ratio)。
    ratio=None 則兩個分量等值（舊行為）。
    """
    vec = [0.0] * 8
    if len(emos) == 2 and ratio is not None:
        primary, secondary = emos
        vec[EMO_ORDER.index(primary)] = strength * ratio
        vec[EMO_ORDER.index(secondary)] = strength * (1.0 - ratio)
    else:
        for emo in emos:
            vec[EMO_ORDER.index(emo)] = strength
    return vec


def apply_calm_bias(vec: list[float]) -> list[float]:
    """在 clamp/normalize 之後把 calm 分量壓回去。

    順序關鍵：normalize 會把總和拉回目標值，若先套 bias 會被完全抵銷，
    所以 calm 的壓制必須是最後一步——代價是總和會低於目標值，
    但那正是想要的（calm 是最容易出怪聲的向量，總能量低一點更安全）。
    """
    out = list(vec)
    idx = EMO_ORDER.index("calm")
    if out[idx]:
        out[idx] = int(out[idx] * CALM_BIAS * 10000) / 10000
    return out


def clamp_vector(vec: list[float], max_sum: float, apply_bias: bool = True,
                 normalize: bool = False) -> list[float]:
    """比例混合式 clamp——只砍總量、保留分量間比例。

    語意比照上游 infer_v2_5.normalize_emo_vec()（該函式只有 webui 會呼叫，
    tts.infer() 不會，所以這裡必須自己做）。

    Args:
        vec: 8 維情感向量
        max_sum: 總和上限
        apply_bias: 是否套用上游 EMO_BIAS 去強調易出怪聲的情緒
        normalize: True 則不論總和大小都縮放到 max_sum（單軸與組合同總量才可比）；
                   False 只在超過時往下壓，低於上限就原樣保留
    """
    out = list(vec)
    if apply_bias:
        out = [v * b for v, b in zip(out, EMO_BIAS)]

    total = sum(out)
    if total > 0 and max_sum > 0 and (normalize or total > max_sum):
        scale = max_sum / total
        out = [v * scale for v in out]

    # 截到 4 位小數，避免 log 難讀（比照上游作法）
    return [int(v * 10000) / 10000 for v in out]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", choices=["ZH", "EN", "JA"], action="append",
                        help="只跑指定語言，可重複指定；預設三語全跑")
    parser.add_argument("--force", action="store_true", help="覆寫既有檔案")
    parser.add_argument("--max-sum", type=float, default=None,
                        help="覆寫所有語言的 clamp 總和上限（預設中英 0.6 / 日文 0.8）")
    parser.add_argument("--no-bias", action="store_true",
                        help="不套用上游 EMO_BIAS，純等比縮放")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印出各組合 clamp 後的向量，不載入模型、不合成")
    parser.add_argument("--only", choices=["singles", "combos", "all"], default="all",
                        help="只跑單軸 / 只跑兩兩組合 / 全跑（預設 all）")
    parser.add_argument("--normalize", action="store_true",
                        help="把單軸也縮放到目標總和（與組合同總量才可比）；"
                             "預設不放大，忠於 0.5/0.65 的原設定")
    parser.add_argument("--tag", default="",
                        help="附加到檔名末尾的標記（如 --tag raw → S1_happy_zh_raw.wav），"
                             "讓不同 clamp 設定的批次並存對照")
    parser.add_argument("--no-clamp", action="store_true",
                        help="完全不 clamp：不套 bias、不做上限縮放，原始向量直送模型")
    parser.add_argument("--combo", action="append", metavar="A+B",
                        help="只跑指定組合，如 --combo happy+sad；可重複指定")
    parser.add_argument("--strength", type=float, action="append", metavar="V",
                        help="梯度模式：指定「最終送進模型的總和」，可重複指定"
                             "（如 --strength 0.35 --strength 0.5 --strength 0.7）。"
                             "會 normalize 到該值，八個情緒齊平；檔名加 _s035 等後綴。")
    parser.add_argument("--ratio", type=float, default=0.7,
                        help="混合情緒的主副比例，預設 0.7（主 70%% / 副 30%%）")
    parser.add_argument("--no-calm-bias", action="store_true",
                        help="不對 calm 套 CALM_BIAS（預設會套，在 normalize 之後）")
    parser.add_argument("--both-directions", action="store_true",
                        help="28 組每組都跑兩個主副方向；預設只跑語意上成立的方向")
    args = parser.parse_args()

    tag = f"_{args.tag}" if args.tag else ""
    if args.no_clamp:
        args.no_bias, args.normalize = True, False

    langs = args.lang or ["ZH", "EN", "JA"]
    # 單軸與組合各有一套強度／上限，用 kind 查表
    strength = {"single": dict(SINGLE_STRENGTH), "combo": dict(STRENGTH)}
    target_sum = {"single": dict(SINGLE_TARGET_SUM), "combo": dict(TARGET_SUM)}
    if args.max_sum is not None:
        target_sum = {k: {lang: args.max_sum for lang in langs} for k in target_sum}
    if args.no_clamp:
        target_sum = {k: {lang: 0.0 for lang in langs} for k in target_sum}
    apply_bias = not args.no_bias

    # --combo happy+sad → 只留指定組合
    combo_filter = None
    if args.combo:
        combo_filter = set()
        for spec in args.combo:
            parts = tuple(p.strip() for p in spec.split("+"))
            if len(parts) != 2 or any(p not in EMO_ORDER for p in parts):
                raise SystemExit(f"--combo 格式錯誤：{spec}（應為 A+B，且需為八向量之一）")
            # 統一成 EMO_ORDER 的順序，才對得上 COMBOS 的 key
            combo_filter.add(tuple(sorted(parts, key=EMO_ORDER.index)))

    if not os.path.isfile(REF_AUDIO):
        raise SystemExit(f"找不到參考音：{REF_AUDIO}")
    missing = [c for c in COMBOS if c not in COMBO_TEXTS]
    if missing:
        raise SystemExit(f"缺少台詞的組合：{missing}")
    missing_single = [e for e in EMO_ORDER if e not in SINGLE_TEXTS]
    if missing_single:
        raise SystemExit(f"缺少台詞的單軸情緒：{missing_single}")

    os.makedirs(OUT_DIR, exist_ok=True)

    # 梯度模式：每個 --strength 跑一輪，該值即最終總和（normalize 到它）
    # levels 的元素為 (檔名後綴, 覆寫的目標總和 or None)
    if args.strength:
        levels = [(f"_s{int(round(v * 100)):03d}", v) for v in args.strength]
    else:
        levels = [("", None)]

    # 待生成清單：(檔名, 路徑, 情緒 tuple, 語言, 台詞, 目標總和覆寫)
    # 單軸用 S{n}_ 前綴、組合用 {nn}_ 數字前綴，排序時自然分群
    plan = []
    for suffix, level in levels:
        if args.only in ("singles", "all"):
            for idx, emo in enumerate(EMO_ORDER, start=1):
                for lang in langs:
                    plan.append((f"S{idx}_{emo}_{lang.lower()}{tag}{suffix}.wav",
                                 (emo,), lang, SINGLE_TEXTS[emo][lang], level))
        if args.only in ("combos", "all"):
            for idx, (a, b) in enumerate(COMBOS, start=1):
                if combo_filter and (a, b) not in combo_filter:
                    continue
                if args.both_directions:
                    dirs = [(a, b), (b, a)]
                else:
                    dirs = COMBO_DIRECTIONS.get((a, b), [(a, b)])
                for primary, secondary in dirs:
                    for lang in langs:
                        # 檔名用 主-副 表示方向，跟舊的 a_b 等分批區分開
                        plan.append((
                            f"C{idx:02d}_{primary}-{secondary}_{lang.lower()}{tag}{suffix}.wav",
                            (primary, secondary), lang,
                            COMBO_TEXTS[(a, b)][lang], level))

    # 跳過已存在的（支援斷點續跑）
    todo = []
    for name, emos, lang, text, level in plan:
        path = os.path.join(OUT_DIR, name)
        if os.path.isfile(path) and not args.force:
            continue
        todo.append((name, path, emos, lang, text, level))

    print(f"參考音：{os.path.basename(REF_AUDIO)}（B 組）")
    print(f"clamp：單軸上限 {target_sum['single']} / 組合上限 {target_sum['combo']}，"
          f"bias={'on' if apply_bias else 'off'}，"
          f"normalize={'on' if args.normalize else 'off'}")
    print(f"清單：{len(plan)} 檔（--only {args.only}），待生成 {len(todo)} 檔")

    if args.dry_run:
        print("\n--dry-run：以下為實際會送進模型的向量\n")
        for name, _path, emos, lang, _text, level in todo:
            kind = "single" if len(emos) == 1 else "combo"
            ratio = args.ratio if kind == "combo" else None
            raw = make_vector(emos, strength[kind][lang], ratio)
            tgt = level if level is not None else target_sum[kind][lang]
            vec = clamp_vector(raw, tgt, apply_bias,
                               args.normalize or level is not None)
            if not args.no_calm_bias:
                vec = apply_calm_bias(vec)
            nz = {EMO_ORDER[i]: v for i, v in enumerate(vec) if v}
            print(f"  {name:<42} 總和 {sum(vec):.4f}  {nz}")
        return

    if not todo:
        print("全部都已存在，沒事做。")
        return

    from indextts.infer_v2_5 import IndexTTS2

    tts = IndexTTS2(
        cfg_path=os.path.join(CKPT_DIR, "config.yaml"),
        model_dir=CKPT_DIR,
        use_bf16=True,
    )
    print(">> 模型就緒\n", flush=True)

    t_start = time.perf_counter()
    manifest = []
    for n, (name, path, emos, lang, text, level) in enumerate(todo, start=1):
        kind = "single" if len(emos) == 1 else "combo"
        ratio = args.ratio if kind == "combo" else None
        raw = make_vector(emos, strength[kind][lang], ratio)
        tgt = level if level is not None else target_sum[kind][lang]
        normalized = args.normalize or level is not None
        vec = clamp_vector(raw, tgt, apply_bias, normalized)
        if not args.no_calm_bias:
            vec = apply_calm_bias(vec)

        t0 = time.perf_counter()
        tts.infer(
            spk_audio_prompt=REF_AUDIO,
            text=text,
            lang=lang,
            output_path=path,
            emo_vector=vec,
        )
        elapsed = time.perf_counter() - t0
        eta = (time.perf_counter() - t_start) / n * (len(todo) - n)
        print(f">> [{n}/{len(todo)}] {name} 總和 {sum(vec):.3f} {elapsed:.2f}s"
              f" | 剩餘約 {eta/60:.1f} 分", flush=True)

        manifest.append({
            "file": name, "kind": kind,
            "emotions": list(emos), "lang": lang, "text": text,
            "raw_vector": raw, "final_vector": vec, "final_sum": round(sum(vec), 4),
            "target_sum": tgt, "bias_applied": apply_bias,
            "normalized": normalized, "clamped": not args.no_clamp,
            "level": level, "ratio": ratio,
            "primary": emos[0] if kind == "combo" else None,
            "calm_bias": not args.no_calm_bias,
        })

    # 記下每檔實際送進模型的向量，之後聽感評估才對得回設定
    manifest_path = os.path.join(OUT_DIR, "manifest.json")
    existing = []
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as fp:
            existing = [e for e in json.load(fp)
                        if e["file"] not in {m["file"] for m in manifest}]
    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump(sorted(existing + manifest, key=lambda e: e["file"]),
                  fp, ensure_ascii=False, indent=2)

    print(f"\n=== 組合掃描完成：{OUT_DIR}（manifest.json 已更新）===")


if __name__ == "__main__":
    main()
