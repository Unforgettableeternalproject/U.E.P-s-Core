"""日文標點西化的契約測試。

背景：2.5 模型以「CJK/全形標點→西式標點」的前處理訓練（上游 infer_v2_5
在進語言分支前對所有語言套 char_rep_map）。lite_engine 的 zh 路徑在自家
normalize() 一條龍內含此步，但 ja/es 分支原本直接進 NeMo（ja 無 grammar
→ 透傳），日文標點（。、）原樣進 GPT。

後果（2026-08-19 Whisper 轉寫實證）：日文標點對 GPT 是罕見 token，會被
模型唸成無意義音節（雪エク/シンジュウ/青春），且 codes 膨脹 40-60%
（160-250 vs 上游 124-131）。修法：ja/es 分支在 NeMo 前先呼叫
TextNormalizer.apply_char_rep_map()。
"""
from echo_tts.modules.tts_module.tts_utils.front import TextNormalizer


def test_apply_char_rep_map_westernizes_ja_punctuation():
    tn = TextNormalizer()
    out = tn.apply_char_rep_map("また同じエラー。このログを見てると、怒る気力も湧かない。")
    assert "。" not in out
    assert "、" not in out
    assert out == "また同じエラー.このログを見てると,怒る気力も湧かない."


def test_apply_char_rep_map_preserves_plain_text():
    tn = TextNormalizer()
    assert tn.apply_char_rep_map("こんにちは") == "こんにちは"


def test_apply_char_rep_map_handles_fullwidth_marks():
    tn = TextNormalizer()
    out = tn.apply_char_rep_map("すごい！本当？はい：そうです；")
    assert out == "すごい!本当?はい,そうです,"
