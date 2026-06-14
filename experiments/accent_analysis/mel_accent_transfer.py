"""
Mel-Space Accent Transfer 實驗

目標：分析 BreezyVoice（台灣腔）與 IndexTTS2（大陸腔）在 mel 頻帶上的統計差異，
     設計一個 mel 校正濾波器，嘗試將 IndexTTS2 的 mel 轉換為台灣腔特徵，
     並用 Griffin-Lim 生成預覽音檔。

策略：
  1. Per-band 統計分析 — 找出哪些 mel band 差異最大
  2. Spectral Envelope Transfer — 將 IndexTTS2 的頻譜包絡向台灣腔靠攏
  3. F0 Contour Smoothing — 台灣腔聲調轉折更平緩
  4. 組合校正 → 生成預覽

約束：uep.pt 不動，IndexTTS2 推論流程不改，只在 mel 輸出後做後處理。
"""

import os
import sys
import json
import numpy as np
import librosa
import librosa.display
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import soundfile as sf
from scipy import signal, interpolate
from scipy.ndimage import gaussian_filter1d

# ── Paths ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
COMPARISON_DIR = os.path.join(PROJECT_ROOT, "outputs", "comparison")
OUTPUT_DIR = os.path.join(COMPARISON_DIR, "analysis")
TRANSFER_DIR = os.path.join(COMPARISON_DIR, "transfer")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TRANSFER_DIR, exist_ok=True)

BREEZY_WAV = os.path.join(COMPARISON_DIR, "breezy.wav")
INDEX_WAV = os.path.join(COMPARISON_DIR, "index.wav")

# ── Config ──
SR = 22050
HOP_LENGTH = 256
N_FFT = 2048
N_MELS = 80
FMIN = 50
FMAX = 8000

plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ═══════════════════════════════════════════
#  Phase 1: Per-Band Mel Analysis
# ═══════════════════════════════════════════

def compute_mel(y: np.ndarray) -> np.ndarray:
    """計算 mel spectrogram (dB)"""
    mel = librosa.feature.melspectrogram(
        y=y, sr=SR, n_fft=N_FFT, hop_length=HOP_LENGTH,
        n_mels=N_MELS, fmin=FMIN, fmax=FMAX
    )
    return librosa.power_to_db(mel, ref=np.max)


def compute_mel_linear(y: np.ndarray) -> np.ndarray:
    """計算 mel spectrogram (linear power, 用於後續轉換)"""
    return librosa.feature.melspectrogram(
        y=y, sr=SR, n_fft=N_FFT, hop_length=HOP_LENGTH,
        n_mels=N_MELS, fmin=FMIN, fmax=FMAX
    )


def analyze_per_band(mel_b_db: np.ndarray, mel_i_db: np.ndarray):
    """
    逐 mel band 比較：mean, std, energy distribution
    找出腔調差異集中的頻帶。
    """
    # Per-band 統計
    band_stats = {
        "breezy_mean": mel_b_db.mean(axis=1),    # (N_MELS,)
        "breezy_std": mel_b_db.std(axis=1),
        "index_mean": mel_i_db.mean(axis=1),
        "index_std": mel_i_db.std(axis=1),
    }
    band_stats["mean_diff"] = band_stats["breezy_mean"] - band_stats["index_mean"]
    band_stats["std_diff"] = band_stats["breezy_std"] - band_stats["index_std"]

    # Mel band 對應的頻率
    mel_freqs = librosa.mel_frequencies(n_mels=N_MELS + 1, fmin=FMIN, fmax=FMAX)
    band_stats["mel_center_freqs"] = (mel_freqs[:-1] + mel_freqs[1:]) / 2

    return band_stats


def plot_per_band_analysis(stats: dict):
    """繪製 per-band 差異圖"""
    fig, axes = plt.subplots(3, 1, figsize=(14, 12))
    freqs = stats["mel_center_freqs"]
    bands = np.arange(N_MELS)

    # 1. Mean energy per band
    ax = axes[0]
    ax.plot(bands, stats["breezy_mean"], "r-", linewidth=1.5, label="BreezyVoice (台灣腔)")
    ax.plot(bands, stats["index_mean"], "b-", linewidth=1.5, label="IndexTTS2 (大陸腔)")
    ax.set_xlabel("Mel Band")
    ax.set_ylabel("Mean Energy (dB)")
    ax.set_title("Per-Band 平均能量對比")
    ax.legend()
    ax.grid(True, alpha=0.3)
    # 加上頻率刻度
    ax2 = ax.twiny()
    tick_positions = [0, 15, 30, 45, 60, 79]
    ax2.set_xlim(ax.get_xlim())
    ax2.set_xticks(tick_positions)
    ax2.set_xticklabels([f"{freqs[i]:.0f} Hz" for i in tick_positions])

    # 2. Mean difference (accent signature)
    ax = axes[1]
    colors = ["#e74c3c" if d > 0 else "#3498db" for d in stats["mean_diff"]]
    ax.bar(bands, stats["mean_diff"], color=colors, alpha=0.7)
    ax.axhline(0, color="black", linewidth=0.5)
    ax.set_xlabel("Mel Band")
    ax.set_ylabel("Diff (dB)")
    ax.set_title("腔調差異 (台灣 - 大陸)：紅色 = 台灣腔能量較多，藍色 = 大陸腔能量較多")
    ax.grid(True, alpha=0.3)

    # 標記關鍵區域
    # 捲舌音區域大約在 2-4 kHz → 找對應的 mel bands
    retro_bands = [i for i, f in enumerate(freqs) if 2000 <= f <= 4500]
    if retro_bands:
        ax.axvspan(retro_bands[0], retro_bands[-1], alpha=0.1, color="purple",
                   label=f"捲舌音區域 ({freqs[retro_bands[0]]:.0f}-{freqs[retro_bands[-1]]:.0f} Hz)")
        ax.legend(fontsize=9)

    # 3. Std difference
    ax = axes[2]
    ax.plot(bands, stats["breezy_std"], "r-", linewidth=1.2, alpha=0.7, label="BreezyVoice std")
    ax.plot(bands, stats["index_std"], "b-", linewidth=1.2, alpha=0.7, label="IndexTTS2 std")
    ax.fill_between(bands, stats["breezy_std"], stats["index_std"], alpha=0.2, color="gray")
    ax.set_xlabel("Mel Band")
    ax.set_ylabel("Std Dev (dB)")
    ax.set_title("Per-Band 能量變異度對比（動態範圍）")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "08_per_band_analysis.png"), dpi=150)
    plt.close()
    print("  [OK] 08_per_band_analysis.png")


# ═══════════════════════════════════════════
#  Phase 2: Mel Correction Filters
# ═══════════════════════════════════════════

def design_spectral_envelope_filter(stats: dict, strength: float = 1.0) -> np.ndarray:
    """
    設計頻譜包絡校正濾波器。

    概念：
    - 計算台灣腔和大陸腔在每個 mel band 的平均能量差
    - 用這個差值作為校正增益（dB）
    - strength 控制校正強度（0.0 = 不校正, 1.0 = 完全校正）

    回傳 (N_MELS,) 的 dB 增益陣列。
    """
    raw_diff = stats["mean_diff"]  # 台灣 - 大陸 (dB)

    # 平滑化，避免過度修正個別 band
    smoothed_diff = gaussian_filter1d(raw_diff, sigma=2.0)

    # 應用強度
    gain_db = smoothed_diff * strength

    return gain_db


def design_retroflexion_suppressor(stats: dict, strength: float = 0.8) -> np.ndarray:
    """
    專門針對捲舌音頻段的抑制濾波器。

    捲舌音（zh, ch, sh, r）的聲學特徵：
    - 2000-4500 Hz 區域有特定的頻譜凹陷和能量分佈
    - 台灣腔弱化捲舌，這個區域的能量分佈不同

    這個濾波器只作用於捲舌音可能出現的頻段。
    """
    freqs = stats["mel_center_freqs"]
    gain_db = np.zeros(N_MELS)

    for i, f in enumerate(freqs):
        if 2000 <= f <= 4500:
            # 在捲舌音區域，用高斯形狀的抑制
            center = 3000  # 捲舌音能量峰值約在 3 kHz
            sigma = 800
            suppression = -3.0 * np.exp(-((f - center) ** 2) / (2 * sigma ** 2))
            gain_db[i] = suppression * strength

    return gain_db


def design_f0_smoother(y: np.ndarray, smoothing_factor: float = 0.6) -> np.ndarray:
    """
    F0 平滑化：讓聲調轉折更平緩（台灣腔特徵）。

    不直接改 mel，而是對音頻做 pitch smoothing。
    這裡用 PSOLA-like 方法的簡化版：對 F0 做高斯平滑。

    回傳平滑化後的音頻（近似）。
    注意：真正的 pitch modification 需要 PSOLA/WORLD，
    這裡先做頻譜域的簡化嘗試。
    """
    # 提取 F0
    f0, voiced, _ = librosa.pyin(y, fmin=50, fmax=500, sr=SR, hop_length=HOP_LENGTH)

    # 對有聲段的 F0 做平滑
    f0_smooth = f0.copy()
    voiced_indices = np.where(voiced)[0]

    if len(voiced_indices) > 3:
        f0_voiced = f0[voiced_indices]
        # 高斯平滑
        sigma = int(len(f0_voiced) * smoothing_factor * 0.1)
        sigma = max(sigma, 2)
        f0_smoothed = gaussian_filter1d(f0_voiced, sigma=sigma)
        f0_smooth[voiced_indices] = f0_smoothed

    return f0, f0_smooth


def apply_mel_correction(mel_linear: np.ndarray, gain_db: np.ndarray) -> np.ndarray:
    """
    將 dB 增益應用到 linear mel spectrogram。

    mel_linear: (N_MELS, T) linear power
    gain_db: (N_MELS,) per-band gain in dB

    回傳校正後的 linear mel。
    """
    gain_linear = 10 ** (gain_db / 10.0)  # dB → linear multiplier
    gain_linear = gain_linear[:, np.newaxis]  # (N_MELS, 1) for broadcasting
    return mel_linear * gain_linear


def mel_to_audio(mel_linear: np.ndarray, n_iter: int = 64) -> np.ndarray:
    """
    用 Griffin-Lim 從 mel spectrogram 重建音頻。
    品質不如 BigVGAN，但足以做初步聽覺驗證。
    """
    # Mel → STFT magnitude
    mel_basis = librosa.filters.mel(sr=SR, n_fft=N_FFT, n_mels=N_MELS, fmin=FMIN, fmax=FMAX)
    # Pseudo-inverse to go from mel back to linear
    mel_inv = np.linalg.pinv(mel_basis)
    stft_mag = np.maximum(0, mel_inv @ mel_linear)

    # Griffin-Lim
    y = librosa.griffinlim(
        stft_mag, n_iter=n_iter, hop_length=HOP_LENGTH,
        n_fft=N_FFT, length=None
    )
    # Normalize
    y = y / (np.max(np.abs(y)) + 1e-8)
    return y


# ═══════════════════════════════════════════
#  Phase 3: Experiment Runner
# ═══════════════════════════════════════════

def run_experiment(name: str, mel_linear: np.ndarray, gain_db: np.ndarray,
                   description: str) -> str:
    """
    執行一個 mel 校正實驗：
    1. 套用校正
    2. 生成音頻
    3. 保存
    4. 繪製 before/after mel 比較
    """
    print(f"\n  --- 實驗: {name} ---")
    print(f"  {description}")

    # 套用校正
    mel_corrected = apply_mel_correction(mel_linear, gain_db)

    # 生成音頻
    print(f"  Griffin-Lim 重建中...")
    y_corrected = mel_to_audio(mel_corrected, n_iter=64)

    # 保存
    output_path = os.path.join(TRANSFER_DIR, f"{name}.wav")
    sf.write(output_path, y_corrected, SR)
    print(f"  [OK] 已保存: {output_path}")

    # 繪製 before/after
    mel_corrected_db = librosa.power_to_db(mel_corrected, ref=np.max)
    mel_original_db = librosa.power_to_db(mel_linear, ref=np.max)

    fig, axes = plt.subplots(3, 1, figsize=(14, 10))

    # Original
    librosa.display.specshow(
        mel_original_db, sr=SR, hop_length=HOP_LENGTH,
        x_axis="time", y_axis="mel", fmin=FMIN, fmax=FMAX,
        ax=axes[0], cmap="inferno"
    )
    axes[0].set_title(f"原始 IndexTTS2 Mel")

    # Corrected
    librosa.display.specshow(
        mel_corrected_db, sr=SR, hop_length=HOP_LENGTH,
        x_axis="time", y_axis="mel", fmin=FMIN, fmax=FMAX,
        ax=axes[1], cmap="inferno"
    )
    axes[1].set_title(f"校正後 Mel — {name}")

    # Difference
    diff = mel_corrected_db - mel_original_db
    im = axes[2].imshow(
        diff, aspect="auto", origin="lower",
        cmap="RdBu_r", vmin=-10, vmax=10,
        extent=[0, diff.shape[1] * HOP_LENGTH / SR, 0, N_MELS]
    )
    axes[2].set_title("校正差異 (dB): 紅=增加, 藍=減少")
    axes[2].set_xlabel("Time (s)")
    axes[2].set_ylabel("Mel Band")
    fig.colorbar(im, ax=axes[2], format="%+.1f dB")

    # 在差異圖上標記增益曲線
    ax_gain = axes[2].twinx()
    ax_gain.plot(np.full(N_MELS, diff.shape[1] * HOP_LENGTH / SR * 0.02),
                 np.arange(N_MELS), alpha=0)  # dummy for scale
    ax_gain.set_ylabel("")

    plt.tight_layout()
    plt.savefig(os.path.join(TRANSFER_DIR, f"{name}_comparison.png"), dpi=150)
    plt.close()
    print(f"  [OK] {name}_comparison.png")

    return output_path


def plot_correction_filters(filters: dict, stats: dict):
    """繪製所有校正濾波器的增益曲線"""
    fig, ax = plt.subplots(figsize=(14, 6))
    freqs = stats["mel_center_freqs"]
    bands = np.arange(N_MELS)

    colors = ["#e74c3c", "#3498db", "#2ecc71", "#9b59b6", "#f39c12"]
    for i, (name, gain) in enumerate(filters.items()):
        color = colors[i % len(colors)]
        ax.plot(bands, gain, linewidth=2, label=name, color=color)
        ax.fill_between(bands, 0, gain, alpha=0.1, color=color)

    ax.axhline(0, color="black", linewidth=0.5)
    ax.set_xlabel("Mel Band")
    ax.set_ylabel("Gain (dB)")
    ax.set_title("Mel 校正濾波器增益曲線")
    ax.legend(loc="lower left")
    ax.grid(True, alpha=0.3)

    # 頻率刻度
    ax2 = ax.twiny()
    tick_positions = [0, 15, 30, 45, 60, 79]
    ax2.set_xlim(ax.get_xlim())
    ax2.set_xticks(tick_positions)
    ax2.set_xticklabels([f"{freqs[i]:.0f} Hz" for i in tick_positions])

    plt.tight_layout()
    plt.savefig(os.path.join(TRANSFER_DIR, "correction_filters.png"), dpi=150)
    plt.close()
    print("  [OK] correction_filters.png")


def generate_baseline_reconstruction(mel_linear: np.ndarray):
    """
    先用原始 IndexTTS2 mel 做 Griffin-Lim 重建，
    作為 baseline（讓我們聽到 Griffin-Lim 本身的品質損失，
    跟校正效果分開評估）。
    """
    print("  Griffin-Lim baseline 重建...")
    y_baseline = mel_to_audio(mel_linear, n_iter=64)
    baseline_path = os.path.join(TRANSFER_DIR, "00_baseline_griffinlim.wav")
    sf.write(baseline_path, y_baseline, SR)
    print(f"  [OK] {baseline_path}")
    return baseline_path


# ═══════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════

def main():
    print("=== Mel-Space Accent Transfer ===")
    print()

    # ── 載入 ──
    print("[1/5] 載入音頻...")
    y_breezy, _ = librosa.load(BREEZY_WAV, sr=SR, mono=True)
    y_index, _ = librosa.load(INDEX_WAV, sr=SR, mono=True)
    y_breezy = y_breezy / (np.max(np.abs(y_breezy)) + 1e-8)
    y_index = y_index / (np.max(np.abs(y_index)) + 1e-8)

    # ── Mel 計算 ──
    print("[2/5] 計算 mel spectrogram...")
    mel_b_db = compute_mel(y_breezy)
    mel_i_db = compute_mel(y_index)
    mel_i_linear = compute_mel_linear(y_index)

    # ── Per-band 分析 ──
    print("[3/5] Per-band 統計分析...")
    stats = analyze_per_band(mel_b_db, mel_i_db)
    plot_per_band_analysis(stats)

    # 找出差異最大的 bands
    diff = stats["mean_diff"]
    top_tw_bands = np.argsort(diff)[-5:][::-1]  # 台灣腔能量更多的 bands
    top_cn_bands = np.argsort(diff)[:5]          # 大陸腔能量更多的 bands

    freqs = stats["mel_center_freqs"]
    print(f"\n  台灣腔能量較多的頻帶 (要增強):")
    for b in top_tw_bands:
        print(f"    Band {b:2d} ({freqs[b]:6.0f} Hz): {diff[b]:+.1f} dB")
    print(f"  大陸腔能量較多的頻帶 (要抑制):")
    for b in top_cn_bands:
        print(f"    Band {b:2d} ({freqs[b]:6.0f} Hz): {diff[b]:+.1f} dB")
    print()

    # ── Baseline ──
    print("[4/5] 生成 baseline...")
    generate_baseline_reconstruction(mel_i_linear)

    # ── 設計濾波器 ──
    print("[5/5] 設計校正濾波器並執行實驗...")

    # Filter A: 完整頻譜包絡校正 (strength=0.5, 保守)
    filter_a = design_spectral_envelope_filter(stats, strength=0.5)

    # Filter B: 完整頻譜包絡校正 (strength=1.0, 全力)
    filter_b = design_spectral_envelope_filter(stats, strength=1.0)

    # Filter C: 只做捲舌音抑制
    filter_c = design_retroflexion_suppressor(stats, strength=1.0)

    # Filter D: 包絡校正 + 捲舌音抑制 (組合)
    filter_d = filter_a + filter_c

    # Filter E: 強力組合 (full envelope + retroflexion)
    filter_e = filter_b + design_retroflexion_suppressor(stats, strength=1.2)

    # 繪製濾波器
    filters = {
        "A: Envelope 50%": filter_a,
        "B: Envelope 100%": filter_b,
        "C: Retroflexion Only": filter_c,
        "D: Envelope 50% + Retro": filter_d,
        "E: Full Combo": filter_e,
    }
    plot_correction_filters(filters, stats)

    # ── 執行實驗 ──
    experiments = [
        ("01_envelope_50pct", filter_a,
         "頻譜包絡校正 50% — 溫和地將 IndexTTS2 的頻譜包絡向台灣腔靠攏"),
        ("02_envelope_100pct", filter_b,
         "頻譜包絡校正 100% — 完全匹配台灣腔的平均頻譜包絡"),
        ("03_retroflexion_only", filter_c,
         "僅捲舌音抑制 — 只降低 2-4.5 kHz 捲舌音區域的能量"),
        ("04_envelope50_retro", filter_d,
         "組合：包絡 50% + 捲舌音抑制 — 預期最平衡的結果"),
        ("05_full_combo", filter_e,
         "全力組合：包絡 100% + 強捲舌音抑制 — 最大化台灣腔效果"),
    ]

    results = {}
    for name, filt, desc in experiments:
        path = run_experiment(name, mel_i_linear, filt, desc)
        results[name] = path

    # ── 摘要 ──
    print("\n" + "=" * 60)
    print("  實驗完成！生成的音檔：")
    print("=" * 60)
    print(f"  Baseline (原始 Griffin-Lim):  outputs/comparison/transfer/00_baseline_griffinlim.wav")
    for name, path in results.items():
        print(f"  {name}:  {os.path.basename(path)}")
    print()
    print("  請聽比較：")
    print("  1. 先聽 baseline vs 原始 index.wav → 了解 Griffin-Lim 的品質損失")
    print("  2. 再聽各實驗 vs baseline → 評估腔調校正效果")
    print("  3. 最後對照 breezy.wav → 看校正方向是否正確")
    print()
    print("  注意：Griffin-Lim 的品質有限，真正的效果需要透過")
    print("  IndexTTS2 管線的 BigVGAN vocoder 才能準確評估。")
    print("  這裡的目的是驗證校正方向是否正確。")
    print("=" * 60)

    # 保存濾波器數據（後續可匯入 IndexTTS2 管線）
    filter_data = {}
    for name, filt in filters.items():
        filter_data[name] = filt.tolist()
    filter_data["mel_center_freqs"] = stats["mel_center_freqs"].tolist()

    json_path = os.path.join(TRANSFER_DIR, "correction_filters.json")
    with open(json_path, "w") as f:
        json.dump(filter_data, f, indent=2)
    print(f"\n  [OK] 濾波器數據: {json_path}")


if __name__ == "__main__":
    main()
