"""外語 v3 音訊工具：韻律統計（F0/RMS，當「情緒是否極端」的代理指標）、高通、頻譜閘降噪、前導低頻量測。"""
import numpy as np


def prosody(y, sr):
    """回傳 F0 均值(Hz)、F0 標準差(半音)、F0 範圍 p5–p95(半音)、有聲段 RMS(dB) 標準差、語音段 RMS 均值(dB)。"""
    import librosa
    y16 = librosa.resample(y, orig_sr=sr, target_sr=16000) if sr != 16000 else y
    f0, vf, _ = librosa.pyin(y16, fmin=65, fmax=450, sr=16000, frame_length=1024, hop_length=160)
    f = f0[vf & ~np.isnan(f0)]
    rms = librosa.feature.rms(y=y16, frame_length=640, hop_length=160)[0]
    db = 20 * np.log10(rms + 1e-6)
    sp = db > (db.max() - 35)
    if len(f) < 10:
        return {"f0_mean": None, "f0_std_st": None, "f0_range_st": None, "rms_std_db": round(float(db[sp].std()), 2), "rms_mean_db": round(float(db[sp].mean()), 1)}
    st = 12 * np.log2(f / f.mean())
    return {"f0_mean": round(float(f.mean()), 1), "f0_std_st": round(float(st.std()), 2),
            "f0_range_st": round(float(np.percentile(st, 95) - np.percentile(st, 5)), 2),
            "rms_std_db": round(float(db[sp].std()), 2), "rms_mean_db": round(float(db[sp].mean()), 1)}


def highpass(y, sr, fc=70.0):
    from scipy.signal import butter, sosfiltfilt
    return sosfiltfilt(butter(4, fc, "highpass", fs=sr, output="sos"), y)


def head_lowfreq_db(y, sr, head_s=0.3, fc=80.0):
    """開頭 head_s 秒內 <fc Hz 的能量（dBFS）與全頻 RMS（dBFS）；用來看低頻底噪前後差異。"""
    from scipy.signal import butter, sosfiltfilt
    h = np.asarray(y[: int(sr * head_s)], dtype="float64")
    if len(h) < 64:
        return None
    lo = sosfiltfilt(butter(4, fc, "lowpass", fs=sr, output="sos"), h)
    f = lambda x: round(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9), 1)  # noqa: E731
    return {"head_low_db": f(lo), "head_full_db": f(h)}


def spectral_gate(y, sr, reduce_db=12.0, n_fft=1024, hop=256, noise_pct=10, smooth=3):
    """輕度降噪（頻譜閘）：以各頻帶最安靜 noise_pct% 的幀估噪聲底，低於噪聲底 * 係數的 bin 衰減 reduce_db。
    只動非語音的底噪，不做頻譜減法，避免人聲產生音樂噪聲。"""
    from scipy.ndimage import uniform_filter
    from scipy.signal import istft, stft
    _, _, Z = stft(y, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
    mag = np.abs(Z)
    floor = np.percentile(mag, noise_pct, axis=1, keepdims=True)
    thr = floor * 2.0
    mask = (uniform_filter(mag, size=(1, smooth)) > thr).astype(float)
    mask = uniform_filter(mask, size=(3, smooth))
    gain = mask + (1 - mask) * 10 ** (-reduce_db / 20)
    _, out = istft(Z * gain, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
    out = out[: len(y)]
    return np.pad(out, (0, len(y) - len(out)))
