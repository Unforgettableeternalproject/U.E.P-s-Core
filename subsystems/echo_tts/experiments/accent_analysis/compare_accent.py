"""
台灣腔 vs 大陸腔 TTS 輸出聲學比較分析

比較 BreezyVoice（台灣腔）與 IndexTTS2（大陸腔）的輸出差異，
量化腔調在頻譜、基頻、共振峰、節奏等維度的差異。

輸出：
  - outputs/comparison/analysis/ 底下的所有圖表
  - 終端機上的數值摘要報告

用法：
  python compare_accent.py
"""

import os
import sys
import json
import numpy as np
import librosa
import librosa.display
import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import soundfile as sf
from scipy import signal

# ── Paths ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
COMPARISON_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison")
OUTPUT_DIR = os.path.join(COMPARISON_DIR, "analysis")
os.makedirs(OUTPUT_DIR, exist_ok=True)

BREEZY_WAV = os.path.join(COMPARISON_DIR, "breezy.wav")
INDEX_WAV = os.path.join(COMPARISON_DIR, "index.wav")

# ── Config ──
SR = 22050  # unified sample rate
HOP_LENGTH = 256
N_FFT = 2048
N_MELS = 80
FMIN = 50
FMAX = 8000

# Matplotlib 中文支援
plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def load_audio(path: str) -> tuple[np.ndarray, int]:
    """載入音頻並 resample 到統一取樣率"""
    y, sr = librosa.load(path, sr=SR, mono=True)
    # 正規化
    y = y / (np.max(np.abs(y)) + 1e-8)
    return y, sr


def extract_features(y: np.ndarray, sr: int, label: str) -> dict:
    """從波形提取所有聲學特徵"""
    features = {"label": label}

    # 1. 基本資訊
    features["duration"] = len(y) / sr
    features["rms_energy"] = float(np.sqrt(np.mean(y**2)))

    # 2. Mel spectrogram
    mel = librosa.feature.melspectrogram(
        y=y, sr=sr, n_fft=N_FFT, hop_length=HOP_LENGTH,
        n_mels=N_MELS, fmin=FMIN, fmax=FMAX
    )
    mel_db = librosa.power_to_db(mel, ref=np.max)
    features["mel_db"] = mel_db

    # 3. F0 (基頻) — 用 pyin
    f0, voiced_flag, voiced_prob = librosa.pyin(
        y, fmin=50, fmax=500, sr=sr, hop_length=HOP_LENGTH
    )
    features["f0"] = f0
    features["voiced_flag"] = voiced_flag
    features["voiced_prob"] = voiced_prob

    # F0 統計（只取有聲段）
    f0_voiced = f0[voiced_flag]
    if len(f0_voiced) > 0:
        features["f0_mean"] = float(np.nanmean(f0_voiced))
        features["f0_std"] = float(np.nanstd(f0_voiced))
        features["f0_min"] = float(np.nanmin(f0_voiced))
        features["f0_max"] = float(np.nanmax(f0_voiced))
        features["f0_range"] = features["f0_max"] - features["f0_min"]
        # F0 變異係數 — 腔調的韻律變化程度
        features["f0_cv"] = features["f0_std"] / features["f0_mean"]
    else:
        features["f0_mean"] = features["f0_std"] = features["f0_min"] = 0
        features["f0_max"] = features["f0_range"] = features["f0_cv"] = 0

    # 4. Spectral centroid（頻譜重心）— 反映音色亮度
    spec_centroid = librosa.feature.spectral_centroid(
        y=y, sr=sr, hop_length=HOP_LENGTH
    )[0]
    features["spectral_centroid"] = spec_centroid
    features["spectral_centroid_mean"] = float(np.mean(spec_centroid))
    features["spectral_centroid_std"] = float(np.std(spec_centroid))

    # 5. Spectral rolloff（頻譜滾降點）— 高頻能量分佈
    spec_rolloff = librosa.feature.spectral_rolloff(
        y=y, sr=sr, hop_length=HOP_LENGTH, roll_percent=0.85
    )[0]
    features["spectral_rolloff"] = spec_rolloff
    features["spectral_rolloff_mean"] = float(np.mean(spec_rolloff))

    # 6. MFCC — 語音特徵的標準表徵
    mfcc = librosa.feature.mfcc(
        y=y, sr=sr, n_mfcc=13, hop_length=HOP_LENGTH
    )
    features["mfcc"] = mfcc
    features["mfcc_mean"] = mfcc.mean(axis=1).tolist()
    features["mfcc_std"] = mfcc.std(axis=1).tolist()

    # 7. 能量包絡（RMS per frame）
    rms = librosa.feature.rms(y=y, hop_length=HOP_LENGTH)[0]
    features["rms"] = rms

    # 8. 語速指標 — onset detection 作為 proxy
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr, hop_length=HOP_LENGTH
    )
    onsets = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=HOP_LENGTH
    )
    features["onset_count"] = len(onsets)
    features["onset_rate"] = len(onsets) / features["duration"]  # onsets/sec
    features["onset_env"] = onset_env
    features["onsets"] = onsets

    # 9. Zero crossing rate — 摩擦音/噝音指標
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=HOP_LENGTH)[0]
    features["zcr"] = zcr
    features["zcr_mean"] = float(np.mean(zcr))

    # 10. Spectral bandwidth — 頻譜寬度
    spec_bw = librosa.feature.spectral_bandwidth(
        y=y, sr=sr, hop_length=HOP_LENGTH
    )[0]
    features["spectral_bandwidth"] = spec_bw
    features["spectral_bandwidth_mean"] = float(np.mean(spec_bw))

    # 11. Formant estimation (簡化版 — 用 LPC)
    features["formants"] = estimate_formants_lpc(y, sr)

    return features


def estimate_formants_lpc(y: np.ndarray, sr: int, order: int = 12) -> dict:
    """
    用 LPC 估算前三共振峰 F1/F2/F3。
    取有聲段的中間 30ms 窗口做估算。
    """
    # 預加重
    y_preemph = np.append(y[0], y[1:] - 0.97 * y[:-1])

    # 取中間段（避免開頭靜音）
    center = len(y_preemph) // 2
    half_win = int(0.03 * sr)  # 30ms
    segment = y_preemph[max(0, center - half_win):center + half_win]

    if len(segment) < order + 1:
        return {"F1": 0, "F2": 0, "F3": 0}

    # Hamming window
    segment = segment * np.hamming(len(segment))

    # LPC
    from scipy.signal import lfilter
    a = librosa.lpc(segment, order=order)

    # 找根
    roots = np.roots(a)
    roots = roots[np.imag(roots) >= 0]  # 只取正頻率

    # 轉成頻率
    angles = np.arctan2(np.imag(roots), np.real(roots))
    freqs = angles * sr / (2 * np.pi)

    # 過濾：只要 50-5500 Hz 內的
    freqs = sorted([f for f in freqs if 50 < f < 5500])

    formants = {}
    for i, name in enumerate(["F1", "F2", "F3"]):
        formants[name] = float(freqs[i]) if i < len(freqs) else 0.0

    return formants


# ═══════════════════════════════════════════
#  Plotting
# ═══════════════════════════════════════════

def plot_mel_comparison(feat_b: dict, feat_i: dict):
    """並排 mel spectrogram"""
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=False)

    for ax, feat, cmap in zip(axes, [feat_b, feat_i], ["magma", "inferno"]):
        img = librosa.display.specshow(
            feat["mel_db"], sr=SR, hop_length=HOP_LENGTH,
            x_axis="time", y_axis="mel", fmin=FMIN, fmax=FMAX,
            ax=ax, cmap=cmap
        )
        ax.set_title(f'{feat["label"]} — Mel Spectrogram', fontsize=13)
        fig.colorbar(img, ax=ax, format="%+2.0f dB")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "01_mel_comparison.png"), dpi=150)
    plt.close()
    print("  ✅ 01_mel_comparison.png")


def plot_f0_comparison(feat_b: dict, feat_i: dict):
    """F0 基頻曲線對比"""
    fig, axes = plt.subplots(2, 1, figsize=(14, 6), sharex=False)

    for ax, feat, color in zip(axes, [feat_b, feat_i], ["#e74c3c", "#3498db"]):
        times = librosa.times_like(feat["f0"], sr=SR, hop_length=HOP_LENGTH)
        ax.plot(times, feat["f0"], color=color, linewidth=1.2, alpha=0.8)
        ax.set_ylabel("F0 (Hz)")
        ax.set_title(f'{feat["label"]} — F0 (mean={feat["f0_mean"]:.0f} Hz, std={feat["f0_std"]:.0f}, CV={feat["f0_cv"]:.3f})')
        ax.set_ylim(50, 400)
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Time (s)")
    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "02_f0_comparison.png"), dpi=150)
    plt.close()
    print("  ✅ 02_f0_comparison.png")


def plot_f0_distribution(feat_b: dict, feat_i: dict):
    """F0 分佈直方圖"""
    fig, ax = plt.subplots(figsize=(10, 5))

    f0_b = feat_b["f0"][feat_b["voiced_flag"]]
    f0_i = feat_i["f0"][feat_i["voiced_flag"]]

    bins = np.linspace(50, 400, 70)
    ax.hist(f0_b, bins=bins, alpha=0.6, color="#e74c3c", label=f'BreezyVoice (μ={feat_b["f0_mean"]:.0f} Hz)', density=True)
    ax.hist(f0_i, bins=bins, alpha=0.6, color="#3498db", label=f'IndexTTS2 (μ={feat_i["f0_mean"]:.0f} Hz)', density=True)
    ax.set_xlabel("F0 (Hz)")
    ax.set_ylabel("Density")
    ax.set_title("F0 分佈對比 — 台灣腔 vs 大陸腔")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "03_f0_distribution.png"), dpi=150)
    plt.close()
    print("  ✅ 03_f0_distribution.png")


def plot_spectral_features(feat_b: dict, feat_i: dict):
    """頻譜特徵對比（centroid, bandwidth, rolloff）"""
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=False)

    metrics = [
        ("spectral_centroid", "Spectral Centroid (Hz)", "頻譜重心 — 音色亮度"),
        ("spectral_bandwidth", "Spectral Bandwidth (Hz)", "頻譜寬度 — 音色豐富度"),
        ("spectral_rolloff", "Spectral Rolloff (Hz)", "頻譜滾降 — 高頻能量"),
    ]

    for ax, (key, ylabel, title) in zip(axes, metrics):
        t_b = librosa.times_like(feat_b[key], sr=SR, hop_length=HOP_LENGTH)
        t_i = librosa.times_like(feat_i[key], sr=SR, hop_length=HOP_LENGTH)
        ax.plot(t_b, feat_b[key], color="#e74c3c", alpha=0.6, linewidth=0.8, label="BreezyVoice")
        ax.plot(t_i, feat_i[key], color="#3498db", alpha=0.6, linewidth=0.8, label="IndexTTS2")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Time (s)")
    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "04_spectral_features.png"), dpi=150)
    plt.close()
    print("  ✅ 04_spectral_features.png")


def plot_mfcc_comparison(feat_b: dict, feat_i: dict):
    """MFCC 均值/標準差對比"""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    x = np.arange(13)
    width = 0.35

    # Mean
    axes[0].bar(x - width/2, feat_b["mfcc_mean"], width, color="#e74c3c", alpha=0.7, label="BreezyVoice")
    axes[0].bar(x + width/2, feat_i["mfcc_mean"], width, color="#3498db", alpha=0.7, label="IndexTTS2")
    axes[0].set_xlabel("MFCC Coefficient")
    axes[0].set_ylabel("Mean")
    axes[0].set_title("MFCC 均值對比")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    # Std
    axes[1].bar(x - width/2, feat_b["mfcc_std"], width, color="#e74c3c", alpha=0.7, label="BreezyVoice")
    axes[1].bar(x + width/2, feat_i["mfcc_std"], width, color="#3498db", alpha=0.7, label="IndexTTS2")
    axes[1].set_xlabel("MFCC Coefficient")
    axes[1].set_ylabel("Std Dev")
    axes[1].set_title("MFCC 標準差對比")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "05_mfcc_comparison.png"), dpi=150)
    plt.close()
    print("  ✅ 05_mfcc_comparison.png")


def plot_energy_rhythm(feat_b: dict, feat_i: dict):
    """能量包絡與語速/節奏比較"""
    fig, axes = plt.subplots(2, 2, figsize=(14, 8))

    # RMS energy
    for ax_row, feat, color, label in [
        (0, feat_b, "#e74c3c", "BreezyVoice"),
        (1, feat_i, "#3498db", "IndexTTS2"),
    ]:
        t = librosa.times_like(feat["rms"], sr=SR, hop_length=HOP_LENGTH)
        axes[ax_row, 0].plot(t, feat["rms"], color=color, linewidth=0.8)
        axes[ax_row, 0].set_title(f"{label} — RMS Energy")
        axes[ax_row, 0].set_ylabel("RMS")
        axes[ax_row, 0].grid(True, alpha=0.3)

        # Onset strength
        t_onset = librosa.times_like(feat["onset_env"], sr=SR, hop_length=HOP_LENGTH)
        axes[ax_row, 1].plot(t_onset, feat["onset_env"], color=color, linewidth=0.8)
        onset_times = librosa.frames_to_time(feat["onsets"], sr=SR, hop_length=HOP_LENGTH)
        axes[ax_row, 1].vlines(onset_times, 0, max(feat["onset_env"]), color="gray", alpha=0.5, linewidth=0.5)
        axes[ax_row, 1].set_title(f"{label} — Onset Strength ({feat['onset_count']} onsets, {feat['onset_rate']:.1f}/s)")
        axes[ax_row, 1].set_ylabel("Strength")
        axes[ax_row, 1].grid(True, alpha=0.3)

    for ax in axes[1]:
        ax.set_xlabel("Time (s)")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "06_energy_rhythm.png"), dpi=150)
    plt.close()
    print("  ✅ 06_energy_rhythm.png")


def plot_zcr_comparison(feat_b: dict, feat_i: dict):
    """Zero Crossing Rate — 摩擦音/捲舌音指標"""
    fig, ax = plt.subplots(figsize=(14, 4))

    t_b = librosa.times_like(feat_b["zcr"], sr=SR, hop_length=HOP_LENGTH)
    t_i = librosa.times_like(feat_i["zcr"], sr=SR, hop_length=HOP_LENGTH)

    ax.plot(t_b, feat_b["zcr"], color="#e74c3c", alpha=0.6, linewidth=0.8, label=f'BreezyVoice (μ={feat_b["zcr_mean"]:.4f})')
    ax.plot(t_i, feat_i["zcr"], color="#3498db", alpha=0.6, linewidth=0.8, label=f'IndexTTS2 (μ={feat_i["zcr_mean"]:.4f})')
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("ZCR")
    ax.set_title("Zero Crossing Rate — 摩擦音/噝音特徵（台灣腔捲舌弱化預期 ZCR 較低）")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "07_zcr_comparison.png"), dpi=150)
    plt.close()
    print("  ✅ 07_zcr_comparison.png")


def generate_summary_report(feat_b: dict, feat_i: dict) -> str:
    """生成數值摘要報告"""
    lines = []
    lines.append("=" * 70)
    lines.append("  台灣腔 vs 大陸腔 TTS 聲學比較報告")
    lines.append("=" * 70)
    lines.append("")

    # 基本資訊
    lines.append("📊 基本資訊")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    lines.append(f"  {'Duration (s)':20s} {feat_b['duration']:>15.2f} {feat_i['duration']:>15.2f} {feat_b['duration']-feat_i['duration']:>+15.2f}")
    lines.append(f"  {'RMS Energy':20s} {feat_b['rms_energy']:>15.4f} {feat_i['rms_energy']:>15.4f} {feat_b['rms_energy']-feat_i['rms_energy']:>+15.4f}")
    lines.append("")

    # F0
    lines.append("🎵 F0 基頻（語調/聲調）")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    lines.append(f"  {'F0 Mean (Hz)':20s} {feat_b['f0_mean']:>15.1f} {feat_i['f0_mean']:>15.1f} {feat_b['f0_mean']-feat_i['f0_mean']:>+15.1f}")
    lines.append(f"  {'F0 Std (Hz)':20s} {feat_b['f0_std']:>15.1f} {feat_i['f0_std']:>15.1f} {feat_b['f0_std']-feat_i['f0_std']:>+15.1f}")
    lines.append(f"  {'F0 Range (Hz)':20s} {feat_b['f0_range']:>15.1f} {feat_i['f0_range']:>15.1f} {feat_b['f0_range']-feat_i['f0_range']:>+15.1f}")
    lines.append(f"  {'F0 CV':20s} {feat_b['f0_cv']:>15.3f} {feat_i['f0_cv']:>15.3f} {feat_b['f0_cv']-feat_i['f0_cv']:>+15.3f}")
    lines.append("  💡 F0 CV（變異係數）反映語調的動態範圍，台灣腔通常較平緩")
    lines.append("")

    # Spectral
    lines.append("🔊 頻譜特徵（音色）")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    lines.append(f"  {'Centroid (Hz)':20s} {feat_b['spectral_centroid_mean']:>15.0f} {feat_i['spectral_centroid_mean']:>15.0f} {feat_b['spectral_centroid_mean']-feat_i['spectral_centroid_mean']:>+15.0f}")
    lines.append(f"  {'Bandwidth (Hz)':20s} {feat_b['spectral_bandwidth_mean']:>15.0f} {feat_i['spectral_bandwidth_mean']:>15.0f} {feat_b['spectral_bandwidth_mean']-feat_i['spectral_bandwidth_mean']:>+15.0f}")
    lines.append(f"  {'Rolloff (Hz)':20s} {feat_b['spectral_rolloff_mean']:>15.0f} {feat_i['spectral_rolloff_mean']:>15.0f} {feat_b['spectral_rolloff_mean']-feat_i['spectral_rolloff_mean']:>+15.0f}")
    lines.append("  💡 Centroid 越高表示音色越亮；捲舌音會拉高高頻能量")
    lines.append("")

    # ZCR
    lines.append("📈 零交叉率（摩擦音/捲舌音指標）")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    lines.append(f"  {'ZCR Mean':20s} {feat_b['zcr_mean']:>15.4f} {feat_i['zcr_mean']:>15.4f} {feat_b['zcr_mean']-feat_i['zcr_mean']:>+15.4f}")
    lines.append("  💡 台灣腔捲舌弱化，預期摩擦音段的 ZCR 較低")
    lines.append("")

    # Rhythm
    lines.append("⏱️ 節奏/語速")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    lines.append(f"  {'Onset Rate (/s)':20s} {feat_b['onset_rate']:>15.1f} {feat_i['onset_rate']:>15.1f} {feat_b['onset_rate']-feat_i['onset_rate']:>+15.1f}")
    lines.append(f"  {'Onset Count':20s} {feat_b['onset_count']:>15d} {feat_i['onset_count']:>15d} {feat_b['onset_count']-feat_i['onset_count']:>+15d}")
    lines.append("")

    # Formants
    lines.append("🗣️ 共振峰估算（LPC, 中間段取樣）")
    lines.append(f"  {'':20s} {'BreezyVoice':>15s} {'IndexTTS2':>15s} {'差異':>15s}")
    for fn in ["F1", "F2", "F3"]:
        bv = feat_b["formants"][fn]
        iv = feat_i["formants"][fn]
        lines.append(f"  {fn+' (Hz)':20s} {bv:>15.0f} {iv:>15.0f} {bv-iv:>+15.0f}")
    lines.append("  💡 F1 ≈ 開口度，F2 ≈ 舌位前後；台灣腔的元音空間可能更集中")
    lines.append("")

    # MFCC distance
    mfcc_b = np.array(feat_b["mfcc_mean"])
    mfcc_i = np.array(feat_i["mfcc_mean"])
    mfcc_dist = float(np.sqrt(np.sum((mfcc_b - mfcc_i) ** 2)))
    lines.append(f"📐 MFCC 歐氏距離: {mfcc_dist:.2f}")
    lines.append("  💡 越大表示兩者整體音色差異越大")
    lines.append("")

    lines.append("=" * 70)
    lines.append("  分析圖表已保存到: outputs/comparison/analysis/")
    lines.append("=" * 70)

    return "\n".join(lines)


def main():
    print("🔍 台灣腔 vs 大陸腔 TTS 聲學比較分析")
    print()

    # 載入
    print("📂 載入音頻...")
    if not os.path.exists(BREEZY_WAV):
        print(f"❌ 找不到: {BREEZY_WAV}")
        sys.exit(1)
    if not os.path.exists(INDEX_WAV):
        print(f"❌ 找不到: {INDEX_WAV}")
        sys.exit(1)

    y_breezy, _ = load_audio(BREEZY_WAV)
    y_index, _ = load_audio(INDEX_WAV)

    print(f"  BreezyVoice: {len(y_breezy)/SR:.2f}s")
    print(f"  IndexTTS2:   {len(y_index)/SR:.2f}s")
    print()

    # 特徵提取
    print("🧪 提取聲學特徵...")
    feat_breezy = extract_features(y_breezy, SR, "BreezyVoice (台灣腔)")
    feat_index = extract_features(y_index, SR, "IndexTTS2 (大陸腔)")
    print()

    # 繪圖
    print("📊 生成分析圖表...")
    plot_mel_comparison(feat_breezy, feat_index)
    plot_f0_comparison(feat_breezy, feat_index)
    plot_f0_distribution(feat_breezy, feat_index)
    plot_spectral_features(feat_breezy, feat_index)
    plot_mfcc_comparison(feat_breezy, feat_index)
    plot_energy_rhythm(feat_breezy, feat_index)
    plot_zcr_comparison(feat_breezy, feat_index)
    print()

    # 報告
    report = generate_summary_report(feat_breezy, feat_index)
    print(report)

    # 保存報告
    report_path = os.path.join(OUTPUT_DIR, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 保存 JSON 數據（方便後續程式讀取）
    summary = {
        "breezy": {k: v for k, v in feat_breezy.items() if isinstance(v, (int, float, str, list, dict)) and k != "mfcc"},
        "index": {k: v for k, v in feat_index.items() if isinstance(v, (int, float, str, list, dict)) and k != "mfcc"},
        "mfcc_distance": float(np.sqrt(np.sum(
            (np.array(feat_breezy["mfcc_mean"]) - np.array(feat_index["mfcc_mean"])) ** 2
        ))),
    }
    json_path = os.path.join(OUTPUT_DIR, "metrics.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n💾 數值報告: {report_path}")
    print(f"💾 JSON 資料: {json_path}")


if __name__ == "__main__":
    main()
