"""語調 A/B 前置：掃描 zh 參考音候選的 F0 範圍／標準差（CPU，共用環境）。輸出 outputs/prosody_ab/ref_scan.json。"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

if __name__ == "__main__":
    cands = []
    for p in sorted(glob.glob(os.path.join(os.path.dirname(C.ROOT), "TTS-wt-vox-emotion", "echo_tts", "assets", "vox", "emotion", "*.wav"))):
        emo = os.path.basename(p).split("_")[1]
        cands.append((os.path.basename(p), p, C.CLIP_TEXTS[emo]))
    clips = C.jload(C.REFS_JSON)["clips"]
    for emo in C.EMOTIONS:
        c = clips[emo]["073"]
        cands.append((f"zh_{emo}_073.wav", os.path.join(C.REFS, c["file"]), c["text"]))
    v = C.jload(os.path.join(C.ROOT, "outputs", "v25_emotion_refs", "emotion_refs.json"))
    for it in v["items"]:
        if it.get("selected"):
            cands.append((f"v25ref_{it['emotion']}.wav", os.path.join(C.ROOT, "outputs", "v25_emotion_refs", it["selected_file"]), it["text"]))
    cands.append(("zh_neutral_x1.2.wav", C.NEUTRAL_REF, C.NEUTRAL_TEXT))
    rows = []
    for name, p, t in cands:
        r = C.prosody(p, t)
        r.update({"name": name, "path": p, "text": t})
        rows.append(r)
        print(name, r.get("dur_s"), r.get("f0_range_st"), r.get("f0_std_st"), r.get("rate_cps"), flush=True)
    C.jdump(os.path.join(C.ROOT, "outputs", "prosody_ab", "ref_scan.json"), rows)
