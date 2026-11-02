"""語調 A/B 共用：測試句、切句模擬、後處理與接縫量測（純 numpy／librosa，CPU）。"""
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emotion_common as C  # noqa: E402

ROOT = C.ROOT
OUT = os.path.join(ROOT, "outputs", "prosody_ab")
RAW = os.path.join(OUT, "raw")
PACK_LOCAL = os.path.join(OUT, "pack")
DEST = "E:/Materials/Voices/UEP/2026-11-01_prosody_ab"
SR = 48000

# 6 段 U.E.P 口吻回覆（含問句、驚嘆、轉折；s2 用「」、s4 用『』）。
SENTS = [
    ("s1", "你真的要現在就走嗎？外面雨下得很大耶！不然…先喝杯熱茶，等雨小一點再出門吧～"),
    ("s2", "他說「等一下」就走了，結果到現在都沒回來！我是不是該傳個訊息問問他？還是…再等等看呢？"),
    ("s3", "哇，這個結果也太出乎意料了吧！我本來以為會失敗的，沒想到居然成功了～不過，接下來才是真正的挑戰喔。"),
    ("s4", "她說『明天見』的時候聲音很輕，我卻聽得一清二楚…怎麼說呢，有點捨不得，但還是笑著揮手了。"),
    ("s5", "好啦好啦，我知道錯了嘛！下次一定會先確認再動手，真的啦～你就別再生氣了，好不好？"),
    ("s6", "唔…這樣說也有道理，可是，我還是覺得哪裡怪怪的。你有沒有發現，他今天從頭到尾都沒笑過？"),
]
KEEP = ("s1", "s2", "s4")  # 聆聽包保留的 3 句（其餘 3 句只用於量測，不留檔）
SEED0 = 7000
BRACKET_SIDS = ("s2", "s4")
BRACKET_SEEDS = (0, 100, 200)  # 加在句子 seed 上
BRK = re.compile(r"[「」『』]")
CLAUSE_END = "，。！？～…；"


def seed_of(sid):
    return SEED0 + int(sid[1:])


def strip_brackets(t):
    return BRK.sub("", t)


def nchars(t):
    return C.count_chars(t)


def clauses(text):
    """依 ，。！？～…； 切子句（標點跟在前一子句尾；連續標點併入同一子句；不在「」『』內切）。"""
    out, buf, depth = [], "", 0
    i = 0
    while i < len(text):
        ch = text[i]
        buf += ch
        if ch in "「『":
            depth += 1
        elif ch in "」』":
            depth = max(0, depth - 1)
        if ch in CLAUSE_END and depth == 0:
            while i + 1 < len(text) and text[i + 1] in CLAUSE_END:
                i += 1
                buf += text[i]
            out.append(buf)
            buf = ""
        i += 1
    if buf.strip():
        out.append(buf)
    return out


def old_split(text, first_max=10, rest_target=20):
    """模擬 bot 舊切法：首段 <= 10 字（以子句為單位累積，首子句就超過則硬切在 10 字），其餘約 20 字。"""
    cl = clauses(text)
    segs, cur = [], ""
    for c in cl:
        limit = first_max if not segs else rest_target
        if not cur:
            if nchars(c) > limit and not segs:
                cut, n = 0, 0
                for k, ch in enumerate(c):
                    n += nchars(ch)
                    if n >= limit:
                        cut = k + 1
                        break
                segs.append(c[:cut])
                cur = c[cut:]
                continue
            cur = c
        elif nchars(cur) + nchars(c) <= limit:
            cur += c
        elif not segs and nchars(cur) < 4:  # 首段太短（< 4 字）：從下一子句硬補到 10 字
            need, k, n = limit - nchars(cur), 0, 0
            for k, ch in enumerate(c):
                n += nchars(ch)
                if n >= need:
                    break
            segs.append(cur + c[:k + 1])
            cur = c[k + 1:]
        else:
            segs.append(cur)
            cur = c
    if cur:
        segs.append(cur)
    return segs


# ----------------------------------------------------------------------------- 後處理
def fade(y, ms=4.0, sr=SR):
    y = y.copy()
    n = min(int(sr * ms / 1000), len(y) // 2)
    if n > 0:
        r = np.linspace(0, 1, n, endpoint=False, dtype=np.float32)
        y[:n] *= r
        y[-n:] *= r[::-1]
    return y


ES_ROOT = os.path.join(os.path.dirname(ROOT), "Echo-Stream")  # 只讀：直接 import adapter 的真實函式


def _es():
    if ES_ROOT not in sys.path:
        sys.path.insert(0, ES_ROOT)
    from echo_stream.adapters.speak_indextts import sanitize_for_tts, smooth_segment_edges, tensor_to_pcm16
    return sanitize_for_tts, smooth_segment_edges, tensor_to_pcm16


def es_sanitize(text):
    return _es()[0](text)


def es_segment(y, in_ms=4.0, out_ms=2.0, sil_ms=15.0, sr=SR):
    """重現 speak_vox._synthesize_one 的單句輸出（48k 直出、不重採樣）：float → int16（tensor_to_pcm16），
    句首半 Hann 淡入 in_ms；句尾 carry 只有 edge_fade_ms（4ms）樣本，所以實際淡出 out_ms = 2ms（n_fade = len//2）；
    句尾附 sil_ms 靜音墊。呼叫的是 adapter 的 smooth_segment_edges。回傳 float32。"""
    _, smooth, to_pcm = _es()
    n = len(y)
    pcm = to_pcm(y)
    head = smooth(pcm, sr, fade_ms=in_ms, tail_silence_ms=0.0, fade_in=True, fade_out=False)
    pcm = smooth(head, sr, fade_ms=out_ms, tail_silence_ms=sil_ms, fade_in=False, fade_out=True)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0


def concat_current(parts, sr=SR, in_ms=4.0, out_ms=2.0, sil_ms=15.0):
    """現行（ES adapter 實際行為）：每句淡入 4ms、淡出實際 2ms、句尾 15ms 靜音墊。
    回傳 (音訊, 接縫時間[秒])；接縫 = 下一句第一個樣本的位置（前面是上一句的靜音墊）。"""
    out, joins, pos = [], [], 0
    for k, p in enumerate(parts):
        seg = es_segment(p, in_ms, out_ms, sil_ms, sr)
        if k:
            joins.append(pos / sr)
        out.append(seg)
        pos += len(seg)
    return np.concatenate(out), joins


def concat_drop(parts, drop_ms, sr=SR, **kw):
    """丟掉每句開頭 drop_ms，再套現行。"""
    return concat_current([p[int(sr * drop_ms / 1000):] for p in parts], sr, **kw)


def natural_decay(y, ms=40.0, sr=SR):
    """backend 補尾示意：取句尾最後一個基音週期反覆接續，振幅以指數衰減到約 -40 dB（接 ms 毫秒）。"""
    n = int(sr * ms / 1000)
    w = y[-int(sr * 0.03):]
    lo, hi = int(sr / 400), int(sr / 100)
    ac = np.correlate(w - w.mean(), w - w.mean(), mode="full")[len(w) - 1:]
    per = lo + int(np.argmax(ac[lo:hi])) if len(w) > hi else lo
    cyc = y[-per:]
    ext = np.tile(cyc, n // per + 1)[:n]
    env = np.exp(np.linspace(0, np.log(0.01), n)).astype(np.float32)
    return np.concatenate([y, (ext * env).astype(np.float32)])


def concat_xfade(parts, xf_ms, drop_ms=0.0, sr=SR):
    """句間 xf_ms 等功率 crossfade，不加靜音墊；首句淡入、末句淡出各 4ms。回傳 (音訊, 接縫中心時間)。"""
    n = int(sr * xf_ms / 1000)
    parts = [p[int(sr * drop_ms / 1000):] for p in parts]
    out = parts[0].copy()
    k = int(sr * 0.004)
    out[:k] *= np.linspace(0, 1, k, endpoint=False, dtype=np.float32)
    joins = []
    for p in parts[1:]:
        a, b = out[-n:], p[:n]
        t = np.linspace(0, np.pi / 2, n, dtype=np.float32)
        mix = a * np.cos(t) + b * np.sin(t)
        joins.append((len(out) - n / 2) / sr)
        out = np.concatenate([out[:-n], mix, p[n:]])
    out[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
    return out, joins


# ----------------------------------------------------------------------------- 量測
def _f0_med(y, sr):
    import librosa
    y16 = librosa.resample(y, orig_sr=sr, target_sr=16000) if sr != 16000 else y
    if len(y16) < 1024:
        return None
    f0, voiced, _ = librosa.pyin(y16, fmin=130, fmax=700, sr=16000, frame_length=1024, hop_length=160)
    ok = voiced & ~np.isnan(f0)
    return float(np.median(f0[ok])) if ok.sum() >= 3 else None


def junction_metrics(y, joins, sr=SR, win_ms=200):
    """每個接縫（時間 J）前後 200ms：F0 中位數差（半音）、RMS 差（dB）、±200ms 內最低 10ms RMS 與低能量（< -45 dBFS）連續長度。"""
    w = int(sr * win_ms / 1000)
    h = int(sr * 0.01)
    rows = []
    for j in joins:
        c = int(round(j * sr))
        a, b = y[max(0, c - w):c], y[c:c + w]
        ex = y[max(0, c - w):c + w]
        db = lambda x: 20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))), 1e-7))  # noqa: E731
        fa, fb = _f0_med(a, sr), _f0_med(b, sr)
        n = len(ex) // h
        e = 20 * np.log10(np.maximum(np.sqrt(np.mean(ex[:n * h].reshape(n, h) ** 2, axis=1)), 1e-7))
        run = best = 0
        for v in (e < -45):
            run = run + 1 if v else 0
            best = max(best, run)
        rows.append({"join_s": round(j, 3),
                     "f0_jump_st": round(12 * np.log2(fb / fa), 2) if fa and fb else None,
                     "rms_jump_db": round(db(b) - db(a), 2),
                     "min_10ms_db": round(float(e.min()), 1),
                     "low_run_ms": best * 10})
    return rows


def junction_summary(rows):
    def m(k, f=lambda v: v):
        xs = [f(r[k]) for r in rows if r[k] is not None]
        return round(float(np.mean(xs)), 2) if xs else None
    return {"n": len(rows), "abs_f0_jump_st_mean": m("f0_jump_st", abs), "abs_rms_jump_db_mean": m("rms_jump_db", abs),
            "min_10ms_db_mean": m("min_10ms_db"), "low_run_ms_mean": m("low_run_ms"),
            "low_run_ms_max": max(r["low_run_ms"] for r in rows)}


def pauses(path_or_y, top_db=35, min_ms=80):
    """句內停頓：librosa.effects.split 的間隙 >= min_ms。回傳 [(中心佔語音段比例, 長度 ms)]。"""
    import librosa
    y = librosa.load(path_or_y, sr=16000, mono=True)[0] if isinstance(path_or_y, str) else path_or_y
    iv = librosa.effects.split(y, top_db=top_db, frame_length=1024, hop_length=160)
    if len(iv) == 0:
        return []
    s0, s1 = iv[0][0], iv[-1][1]
    out = []
    for (a0, a1), (b0, b1) in zip(iv[:-1], iv[1:]):
        gap = (b0 - a1) / 16000 * 1000
        if gap >= min_ms:
            out.append((round(((a1 + b0) / 2 - s0) / max(s1 - s0, 1), 3), round(gap)))
    return out


def head_frames(y, sr=SR, ms=200):
    """串流開頭：前 8 個 10ms 的 RMS（相對全句語音中位數 dB）、頭 40/80ms 頻譜重心 vs 全句中位數、頭 200ms 最大樣本跳變 vs 全句 p99。"""
    import librosa
    h = int(sr * 0.01)
    n = len(y) // h
    e = 20 * np.log10(np.maximum(np.sqrt(np.mean(y[:n * h].reshape(n, h) ** 2, axis=1)), 1e-7))
    act = e > (e.max() - 35)
    med = float(np.median(e[act]))
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=1024, hop_length=480)[0]
    cmed = float(np.median(cent))
    d = np.abs(np.diff(y))
    c40 = float(np.mean(librosa.feature.spectral_centroid(y=y[:int(sr * .04)], sr=sr, n_fft=1024, hop_length=480)[0]))
    c80 = float(np.mean(librosa.feature.spectral_centroid(y=y[:int(sr * .08)], sr=sr, n_fft=1024, hop_length=480)[0]))
    return {"first_8x10ms_rel_db": [round(float(v - med), 1) for v in e[:8]], "speech_median_db": round(med, 1),
            "centroid_first40_hz": round(c40), "centroid_first80_hz": round(c80), "centroid_median_hz": round(cmed),
            "max_jump_first200ms": round(float(d[:int(sr * ms / 1000)].max()), 4), "p99_jump_all": round(float(np.percentile(d, 99)), 4),
            "onset_ms": next((i * 10 for i, v in enumerate(e) if v > e.max() - 35), None)}


def chunk_joins(y, chunk_lens, k=6):
    """串流 chunk 接縫（前 k 個）處的樣本差 / 全句 p99 樣本差（>> 1 表示不連續）。"""
    d = np.abs(np.diff(y))
    p99 = float(np.percentile(d, 99))
    out, pos = [], 0
    for L in chunk_lens[:k]:
        pos += L
        if 0 < pos < len(y):
            out.append(round(float(d[pos - 1]) / p99, 2))
    return out


def _db(x):
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))), 1e-7)) if len(x) else -140.0


def tail_stats(y, sr=SR):
    """句尾收音：最後 20/10/5 ms 的 RMS（dBFS 與相對語音中位數）、峰值、最後一個樣本 / 全句峰值、
    「衰減長度」= 最後一個高於（語音中位數 - 20 dB）的 10ms 框到音訊結尾的毫秒數（越短 = 收得越硬）。"""
    h = int(sr * 0.01)
    n = len(y) // h
    e = np.array([_db(y[i * h:(i + 1) * h]) for i in range(n)])
    med = float(np.median(e[e > e.max() - 35]))
    above = np.where(e > med - 20)[0]
    decay_ms = round(((len(y) - (above[-1] + 1) * h) / sr) * 1000 + 10) if len(above) else None
    out = {"len_samples": len(y), "speech_med_db": round(med, 1), "decay_ms": decay_ms,
           "last_over_peak": round(float(abs(y[-1]) / max(float(np.abs(y).max()), 1e-9)), 4)}
    for ms in (20, 10, 5):
        w = y[-int(sr * ms / 1000):]
        out[f"rms_last{ms}_db"] = round(_db(w), 1)
        out[f"rms_last{ms}_rel_db"] = round(_db(w) - med, 1)
        out[f"peak_last{ms}"] = round(float(np.abs(w).max()), 4)
    return out


def click_metrics(y, joins, sr=SR, win_ms=20):
    """接縫 click 代理：±20ms 內最大樣本差 / 全檔 p99 樣本差；±20ms 的 >4kHz 能量（dBFS）與 ±200ms 的 >4kHz 能量之差。"""
    from scipy.signal import butter, sosfiltfilt
    hp = sosfiltfilt(butter(4, 4000, "hp", fs=sr, output="sos"), y.astype(np.float64))
    d = np.abs(np.diff(y))
    p99 = float(np.percentile(d[d > 0], 99)) if (d > 0).any() else 1e-9
    w, W = int(sr * win_ms / 1000), int(sr * 0.2)
    rows = []
    for j in joins:
        c = int(round(j * sr))
        seg = d[max(0, c - w):c + w]
        hf_w, hf_W = _db(hp[max(0, c - w):c + w]), _db(hp[max(0, c - W):c + W])
        rows.append({"join_s": round(j, 3), "max_step_over_p99": round(float(seg.max()) / p99, 2) if len(seg) else None,
                     "hf_win_db": round(hf_w, 1), "hf_win_minus_200ms_db": round(hf_w - hf_W, 1)})
    return rows


def click_summary(rows):
    xs = lambda k: [r[k] for r in rows if r[k] is not None]  # noqa: E731
    return {"max_step_over_p99_mean": round(float(np.mean(xs("max_step_over_p99"))), 2),
            "max_step_over_p99_max": round(float(np.max(xs("max_step_over_p99"))), 2),
            "hf_win_db_mean": round(float(np.mean(xs("hf_win_db"))), 1),
            "hf_rel_db_mean": round(float(np.mean(xs("hf_win_minus_200ms_db"))), 1)}
