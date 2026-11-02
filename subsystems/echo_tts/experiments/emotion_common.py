"""情緒聆聽實驗共用：情緒片段文本、測試句、強度對應、韻律代理指標。

強度協議：ES user 刻度 0 / 0.5 / 1 對應 IndexTTS 情緒向量總和 raw 0.57 / 0.65 / 0.73（echo_tts/config/emotion_presets.py）。
VOX 沒有情緒向量，以不同 strength 生成的情緒參考音表達強度；不得使用此區間以外的 strength。
"""
import json
import os
import re

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = r"C:\Users\Bernie\source\repos\Unforgettableeternalproject\TestSeparateTTSSystem"
OUT = os.path.join(ROOT, "outputs", "emotion_v1")
REFS = os.path.join(OUT, "refs")
CANDS = os.path.join(REFS, "candidates")
GEN_JSON = os.path.join(REFS, "gen.json")
REFS_JSON = os.path.join(REFS, "emotion_clips.json")
V25_DIR = os.path.join(OUT, "v25")
VOX_DIR = os.path.join(OUT, "vox")
VOX_JSON = os.path.join(OUT, "vox_gen.json")
METRICS_JSON = os.path.join(OUT, "metrics.json")
PACK = r"E:\Materials\Voices\UEP\2026-11-01_emotion_check"
R1_PATH = os.path.join(ROOT, "echo_tts", "assets", "reference", "uep_reference_v25.wav")
VOX_ASSETS = os.path.join(MAIN, "echo_tts", "assets", "vox")  # 主 worktree 唯讀
NEUTRAL_REF = os.path.join(VOX_ASSETS, "zh_neutral_x1.2.wav")  # ref120
NEUTRAL_TEXT = "今天早上我先把信箱整理了一遍，接著確認了下午會議要用的資料，等一下還要去超商領包裹，應該可以在天黑之前把事情都處理完。"
MODEL_DIR = os.path.join(ROOT, "models_cache", "VoxCPM2")
LORA_DIR = os.path.join(ROOT, "models_cache", "vox_lora_tw", "checkpoints", "step_0000600")

STRENGTHS = {"057": 0.57, "065": 0.65, "073": 0.73}
USER_OF = {"057": 0.0, "065": 0.5, "073": 1.0}
EMOTIONS = ["happy", "sad", "angry", "afraid", "surprised", "calm", "melancholic", "disgusted"]
TAKES = 2

# 情緒片段文本（8-12 秒、句尾完整）。三個強度、兩個 take 都用同一段文字與同一組 seed。
CLIP_TEXTS = {
    "happy": "太好了，我們終於成功了！這個月的努力總算有回報，我真的超開心，今晚一定要慶祝一下！",
    "angry": "你到底有沒有在聽我說話？這已經是第三次了，我明明交代過很多遍，你還是照自己的想法亂來！",
    "sad": "我一直想著那天晚上的事，心裡就好難過。早知道，我就多陪陪她了。",
    "afraid": "你有聽到嗎？樓上好像有人在走動……可是這間房子明明只有我一個人。我不敢回頭。",
    "disgusted": "噁，這是什麼味道啊？冰箱裡的剩菜居然放到發霉了，上面還長了一堆奇怪的東西，我看了都快吐了。",
    "melancholic": "雨一直下，窗外的路燈都變得模糊了。翻著舊照片，日子過得好快。",
    "surprised": "咦？等一下，你說什麼？真的假的，他居然就這樣突然出現在門口！我完全沒有想到，嚇了我一大跳耶！",
    "calm": "沒關係，慢慢來就好。先深呼吸一下，把手邊的事情一件一件做完，不用著急，時間還很充裕。",
}

# 測試句：每種情緒 2 句台灣口語，與情緒片段逐字稿不重複
SENTENCES = {
    "happy": ["欸欸，我剛剛抽到演唱會的票了耶！好幸運喔，等一下馬上傳給大家分享這個好消息！",
              "耶，今天的報告被老闆大力稱讚了！整個人都輕飄飄的，晚餐我請客吧！"],
    "sad": ["今天整理房間的時候，翻到她以前寫給我的卡片，眼淚就忍不住掉下來了。",
            "明明說好要一起去看海的，現在只剩我一個人站在這裡，心裡空空的。"],
    "angry": ["我昨天就講過了，資料要先備份再動手，結果你連問都不問就直接覆蓋掉，是要我講幾次才聽得懂？",
              "夠了喔，每次都是同樣的藉口，遲到就遲到，為什麼不能直接認錯？"],
    "afraid": ["剛剛走廊的燈突然全部熄掉了，我聽到後面有腳步聲，而且越來越近……",
               "拜託你不要放開我的手，那邊好黑，我真的不敢一個人走過去。"],
    "surprised": ["什麼？你說她已經搬到國外了？我上個禮拜還在路上碰到她耶，怎麼可能！",
                  "哇，這個盒子裡面居然是我小時候弄丟的那隻玩具熊，你是在哪裡找到的啊？"],
    "calm": ["沒事的，你先坐下來喝口水，我們把問題一個一個列出來，再慢慢討論該怎麼做。",
             "今天的天氣很舒服，我泡了一壺茶，什麼都不急著做，就靜靜地待一會兒。"],
    "melancholic": ["傍晚的風吹起來有點涼，我坐在老地方，想起很多年前的夏天，不知道你現在過得好不好。",
                    "老房子拆掉之後，那條巷子安靜了很多，有些事情，也只能留在回憶裡了吧。"],
    "disgusted": ["天啊，這杯牛奶怎麼酸成這樣，我才喝一口就整個胃都翻過來了。",
                  "你看那個水溝裡面，又黑又臭還漂著一堆垃圾，我連靠近都不想靠近。"],
}


def sentence_items():
    """回傳 [(sid, emotion, idx, text, seed)]；sid 如 happy1。"""
    out = []
    for ei, emo in enumerate(EMOTIONS):
        for i, t in enumerate(SENTENCES[emo]):
            out.append((f"{emo}{i + 1}", emo, i + 1, t, 5000 + ei * 10 + i))
    return out


def count_chars(text):
    """語速用字數：去掉標點與空白。"""
    return len(re.sub(r"[\s，。！？、…「」『』（）()：；,.!?~\-—]", "", text))


def prosody(path, text):
    """粗略韻律代理：F0（pyin，Hz 與半音）、能量（dB）、語速。人耳判斷仍為準。"""
    import librosa
    y, sr = librosa.load(path, sr=16000, mono=True)
    dur = len(y) / sr
    # 以 -35 dB 修掉頭尾靜音，算有效語音長度
    intervals = librosa.effects.split(y, top_db=35)
    speech = (intervals[-1][1] - intervals[0][0]) / sr if len(intervals) else dur
    f0, voiced, _ = librosa.pyin(y, fmin=130, fmax=700, sr=sr, frame_length=1024, hop_length=160)
    ok = voiced & ~np.isnan(f0)
    ok &= (np.nan_to_num(f0) > 135) & (np.nan_to_num(f0) < 690)  # 排除貼著搜尋邊界的誤判幀
    # 去八度誤判：與前後 11 幀滑動中位數相差超過 7 半音的幀視為離群
    from scipy.ndimage import median_filter
    st = np.where(ok, 12 * np.log2(np.where(ok, f0, 1.0) / 100.0), np.nan)
    fill = np.where(np.isnan(st), np.nanmedian(st) if ok.any() else 0.0, st)
    ok &= np.abs(fill - median_filter(fill, size=11, mode="nearest")) <= 7
    f = f0[ok]
    rms = librosa.feature.rms(y=y, frame_length=640, hop_length=160)[0]
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    act = db > (db.max() - 35)
    res = {"dur_s": round(dur, 2), "speech_s": round(float(speech), 2),
           "rate_cps": round(count_chars(text) / max(speech, 1e-3), 2) if text else None}
    if len(f) > 10:
        p5, p95 = np.percentile(f, [5, 95])
        res.update({"f0_mean_hz": round(float(np.mean(f)), 1), "f0_median_hz": round(float(np.median(f)), 1),
                    "f0_p5_hz": round(float(p5), 1), "f0_p95_hz": round(float(p95), 1),
                    "f0_range_st": round(float(12 * np.log2(p95 / p5)), 2),
                    "f0_std_st": round(float(np.std(12 * np.log2(f / np.median(f)))), 2)})
    res.update({"rms_db_mean": round(float(db[act].mean()), 1), "rms_db_p95": round(float(np.percentile(db[act], 95)), 1),
                "rms_db_std": round(float(db[act].std()), 1)})
    return res


def cer(ref, hyp):
    """字元錯誤率：兩邊先轉繁體並去標點、去拉丁字母與空白。"""
    import opencc
    cc = opencc.OpenCC("s2t")
    norm = lambda s: re.sub(r"[\s，。！？、…「」『』（）()：；,.!?~\-—A-Za-z]", "", cc.convert(s))  # noqa: E731
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / len(r)


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jdump(p, o):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)
