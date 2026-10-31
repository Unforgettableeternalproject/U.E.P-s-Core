"""外語 v3 共用：句子表、prompt 候選句、路徑與 CER 工具（純標準庫 + 延遲匯入，core env 與 .venv-voxcpm 皆可 import）。"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(ROOT, "outputs", "foreign_v3")
CAND = os.path.join(W, "cand")        # prompt 候選的原始輸出
PROMPTS = os.path.join(W, "prompts")  # 選定後的 prompt（wav + json）
RAW = os.path.join(W, "raw")          # 測試句的原始輸出
REFS = os.path.join(ROOT, "outputs", "voxcpm2_zeroshot", "refs")
DEST = "E:/Materials/Voices/UEP/2026-11-01_foreign_v3"
SEED = 1031
# V25 現行情緒向量順序 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMO = {"en2": [0.5, 0, 0, 0, 0, 0, 0, 0], "en3": [0, 0, 0.5, 0, 0, 0, 0, 0],
       "ja2": [0.65, 0, 0, 0, 0, 0, 0, 0], "ja3": [0, 0, 0.65, 0, 0, 0, 0, 0]}

SENT = {  # 測試句：en1–3、ja1–3 沿用 v2；en4–6、ja4–6 為新增
    "en1": "The weather is nice today. I finished organizing everything on my desk. What should we do next?",
    "en2": "Haha! Guess what? The new model passed every single test on the first try! Let's get it running right now!",
    "en3": "I'm sorry, I lost the backup files. I promised I would keep them safe, and I let you down.",
    "en4": "Please remember to save your work before closing the window. I will check the settings again tomorrow morning, and then we can decide whether anything needs to change.",
    "en5": "Could you tell me where the nearest station is? I think I took the wrong exit.",
    "en6": "Our schedule for next week is almost ready. On Monday we review the plan, on Tuesday we run the tests, and by Friday the report should be finished.",
    "ja1": "今日はいい天気ですね。机の上の仕事を全部片付けました。次は何をしましょうか？",
    "ja2": "ねえねえ、聞いて！新しいモデルのテスト、一発で全部合格したんだよ！すごくない？",
    "ja3": "あ...ごめんなさい……大事なデータを失くしちゃった……ちゃんと守るって約束したのに……",
    "ja4": "昨日の夜、生物の先生に生き物の世話の仕方を聞いた。明日は朝から人気の店へ行くつもりだ。",
    "ja5": "この辺りは大人から子どもまで楽しめる場所で、何度行っても飽きないと、友達が嬉しそうに話してくれました。",
    "ja6": "今日は一日中雨でしたが、仕事が早く終わったので、家でゆっくり本を読んで過ごしました。最近は、夜になると風が冷たくなってきたので、温かいお茶を入れて、ひと休みするのが楽しみです。",
}
LANG = {k: k[:2] for k in SENT}
# prompt 候選句（與測試句不重複；中性、日常報告語氣）
CAND_TEXT = {
    "en": ["I checked the schedule this morning, and everything looks fine. We can start the meeting at ten, and I will send the notes to everyone afterward.",
           "There is a small library near the station. I usually go there on weekends to read a book and drink a cup of tea.",
           "The report is almost finished. I only need to check the numbers once more, and then I will send it to the team.",
           "Please take a moment to review the plan. If you have any questions, let me know, and we will go through them together."],
    "ja": ["今朝の会議では、来月の予定と新しい計画について話し合い、最後に資料を全員に送りました。",
           "二人で行った京都の旅行は、天気もよく、本当に楽しい思い出になりました。",
           "私の兄は東京の大学で、日本の歴史を専攻していて、毎週図書館で本を読んでいます。",
           "駅の近くの小さな店で、温かいお茶を飲みながら、静かに手紙を書きました。"],
}


def _strip(t):
    return re.sub(r"[\s\.\,\!\?\;\:、。，！？；：…「」『』\(\)\-~～\"'・]+", "", t).lower()


def cer(ref, hyp):
    from rapidfuzz.distance import Levenshtein
    r, h = _strip(ref), _strip(hyp)
    return round(Levenshtein.distance(r, h) / max(1, len(r)), 3)
