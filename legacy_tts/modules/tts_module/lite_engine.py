"""
IndexTTS2 Lite Engine - 精簡推論引擎

精簡功能:
- ✅ 加載預提取的角色特徵
- ✅ 情感向量控制 (8維)
- ✅ 文本轉語音合成
- ❌ 移除: 特徵提取、情感識別、BPE分詞等

使用方式:
    from indextts.lite_engine import IndexTTSLite
    
    # 初始化
    engine = IndexTTSLite(
        cfg_path="checkpoints/config.yaml",
        model_dir="checkpoints"
    )
    
    # 加載角色
    engine.load_character("characters/uep-1.pt")
    
    # 合成語音
    engine.synthesize(
        text="Hello world",
        output_path="output.wav",
        emotion_vector=[0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # Happy
        max_emotion_strength=0.3  # 保留 70% 原始聲音
    )
"""

import os
import time
import torch
import warnings
from pathlib import Path
from typing import Optional, List, Union

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from omegaconf import OmegaConf
from huggingface_hub import hf_hub_download
import safetensors

# 導入系統日誌工具
try:
    from utils.debug_helper import debug_log, info_log, error_log
    _HAS_SYSTEM_LOG = True
except ImportError:
    _HAS_SYSTEM_LOG = False
    # Fallback: 使用 print
    def debug_log(level, msg): print(msg)
    def info_log(msg): print(msg)
    def error_log(msg): print(f"ERROR: {msg}")

# 使用絕對導入 (當作為腳本運行時)
import sys
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))

try:
    # 嘗試相對導入
    from .gpt.model_v2 import UnifiedVoice
    from .tts_utils.maskgct_utils import build_semantic_model, build_semantic_codec
    from .tts_utils.front import TextNormalizer, TextTokenizer
    from .tts_utils.gpt_length import calc_dynamic_max_generate_length
    from .tts_utils.chinese_segmenter import pre_segment_chinese
    from .tts_utils.english_segmenter import pre_segment_english
    from .tts_utils.custom_vocab import apply_custom_vocab
    from .tts_utils.segment_guard import check_segment_anomaly, get_retry_params, SegmentGuardStats
    from .s2mel.modules.commons import load_checkpoint2, MyModel
    from .s2mel.modules.bigvgan import bigvgan
except ImportError:
    # 回退到絕對導入
    from gpt.model_v2 import UnifiedVoice
    from tts_utils.maskgct_utils import build_semantic_model, build_semantic_codec
    from tts_utils.front import TextNormalizer, TextTokenizer
    from tts_utils.gpt_length import calc_dynamic_max_generate_length
    from tts_utils.chinese_segmenter import pre_segment_chinese
    from tts_utils.english_segmenter import pre_segment_english
    from tts_utils.custom_vocab import apply_custom_vocab
    from tts_utils.segment_guard import check_segment_anomaly, get_retry_params, SegmentGuardStats
    from s2mel.modules.commons import load_checkpoint2, MyModel
    from s2mel.modules.bigvgan import bigvgan


class IndexTTSLite:
    """精簡版 IndexTTS2 推論引擎"""

    @staticmethod
    def _remove_long_silence(
        codes: "torch.Tensor",
        stop_mel_token: int,
        silent_token: int = 52,
        max_consecutive: int = 30,
        keep_consecutive: int = 10,
    ) -> tuple:
        """Remove excessively long runs of silent tokens from GPT output codes.

        Ported from upstream IndexTTS2 ``infer_v2.py::remove_long_silence``.

        Parameters
        ----------
        codes:
            Shape ``[B, T]`` — batch of mel token sequences from GPT.
        stop_mel_token:
            The stop token ID (e.g. 8193).
        silent_token:
            The silence token ID (default 52 per upstream).
        max_consecutive:
            If total count of silent_token in the sequence exceeds this,
            trigger compression.
        keep_consecutive:
            Maximum consecutive silent tokens to keep after compression.

        Returns
        -------
        (codes, code_lens) where codes may be shortened.
        """
        device = codes.device
        codes_list = []
        code_lens = []
        modified = False

        for i in range(codes.shape[0]):
            code = codes[i]

            # Find effective length (up to stop token)
            if not torch.any(code == stop_mel_token).item():
                eff_len = code.size(0)
            else:
                stop_idx = (code == stop_mel_token).nonzero(as_tuple=False)
                eff_len = stop_idx[0].item() if len(stop_idx) > 0 else code.size(0)

            silent_count = torch.sum(code[:eff_len] == silent_token).item()

            if silent_count > max_consecutive:
                # Compress: keep at most `keep_consecutive` consecutive silent tokens
                keep_indices = []
                n_consecutive = 0
                for k in range(eff_len):
                    if code[k].item() != silent_token:
                        keep_indices.append(k)
                        n_consecutive = 0
                    elif n_consecutive < keep_consecutive:
                        keep_indices.append(k)
                        n_consecutive += 1
                    # else: skip (too many consecutive silences)

                eff_len = len(keep_indices)
                codes_list.append(code[keep_indices])
                modified = True
            else:
                codes_list.append(code[:eff_len])

            code_lens.append(eff_len)

        if modified:
            if len(codes_list) == 1:
                codes = codes_list[0].unsqueeze(0)
            else:
                from torch.nn.utils.rnn import pad_sequence
                codes = pad_sequence(codes_list, batch_first=True, padding_value=stop_mel_token)

        max_len = max(code_lens) if code_lens else 0
        if max_len > 0 and max_len < codes.shape[1]:
            codes = codes[:, :max_len]

        code_lens_tensor = torch.tensor(code_lens, dtype=torch.long, device=device)
        return codes, code_lens_tensor

    @staticmethod
    def _trim_trailing_silence(
        audio: "torch.Tensor",
        rms_threshold: float = 0.005,
        window_ms: int = 50,
        min_silence_ms: int = 300,
        keep_tail_ms: int = 80,
        sample_rate: int = 22050,
    ) -> "torch.Tensor":
        """Trim trailing silence from a segment's audio tensor.

        Uses windowed RMS to detect silence (robust against BigVGAN's
        low-level noise floor).  Scans backwards from the end; if trailing
        silence exceeds *min_silence_ms*, trims to last voiced window plus
        *keep_tail_ms* of fade-out.

        Works on shapes ``[samples]`` or ``[channels, samples]``.
        """
        if audio.numel() == 0:
            return audio

        # Flatten to 1-D for analysis
        flat = audio.view(-1) if audio.dim() == 1 else audio[0]
        n_samples = flat.shape[0]

        window_size = int(sample_rate * window_ms / 1000)
        min_silence_samples = int(sample_rate * min_silence_ms / 1000)
        keep_samples = int(sample_rate * keep_tail_ms / 1000)

        if n_samples < window_size:
            return audio

        # Compute RMS per window from the end backwards
        n_windows = n_samples // window_size
        last_voiced_sample = n_samples  # default: no trim

        for i in range(n_windows - 1, -1, -1):
            start = i * window_size
            end = start + window_size
            window_rms = (flat[start:end].float() ** 2).mean().sqrt().item()
            if window_rms > rms_threshold:
                last_voiced_sample = end
                break
        else:
            # All windows below threshold — keep minimal audio
            return audio[..., :keep_samples] if n_samples > keep_samples else audio

        trailing_silence = n_samples - last_voiced_sample

        if trailing_silence > min_silence_samples:
            trim_to = min(last_voiced_sample + keep_samples, n_samples)
            return audio[..., :trim_to]

        return audio

    def __init__(
        self,
        cfg_path: str = "checkpoints/config.yaml",
        model_dir: str = "checkpoints",
        use_fp16: bool = True,
        device: Optional[str] = None,
        use_cuda_kernel: bool = False,
        use_accel: bool = False,
    ):
        """
        初始化精簡推論引擎

        Args:
            cfg_path: 配置文件路徑
            model_dir: 模型目錄
            use_fp16: 是否使用半精度
            device: 設備 (None=自動檢測)
            use_cuda_kernel: 是否使用 CUDA kernel (需要 CUDA Toolkit)
            use_accel: 是否啟用加速引擎 (CUDA Graph + Flash Attention + Paged KV Cache)
        """
        self.model_dir = model_dir
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        self.use_accel = use_accel
        
        # 設置設備
        if device is None:
            if torch.cuda.is_available():
                self.device = torch.device("cuda")
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self.device = torch.device("mps")
            else:
                self.device = torch.device("cpu")
        else:
            self.device = torch.device(device)
        
        # 加載配置（必須先加載才能讀取優化設定）
        self.cfg = OmegaConf.load(cfg_path)
        
        # 🚀 進階優化配置（輕量方案，無需額外依賴）
        adv_opt = self.cfg.get('advanced_optimization', {})
        self.use_torch_compile = adv_opt.get('use_torch_compile', False)
        self.compile_mode = adv_opt.get('compile_mode', 'reduce-overhead')
        self.cudnn_benchmark = adv_opt.get('cudnn_benchmark', True)
        
        # SDPA (Scaled Dot Product Attention) 配置
        self.use_sdpa = adv_opt.get('use_sdpa', True)
        self.enable_math = adv_opt.get('enable_math', True)
        self.enable_flash = adv_opt.get('enable_flash', True)
        self.enable_mem_efficient = adv_opt.get('enable_mem_efficient', True)
        
        # 量化配置
        self.use_dynamic_quantization = adv_opt.get('use_dynamic_quantization', False)
        self.quantize_gpt = adv_opt.get('quantize_gpt', False)
        self.quantize_s2mel = adv_opt.get('quantize_s2mel', False)
        
        # 🚀 啟用 cuDNN benchmark（自動選擇最優 kernel）
        if self.cudnn_benchmark and torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
            debug_log(2, "   ⚡ 已啟用 cuDNN benchmark (自動調優)")
        
        # 🚀 配置 SDPA (Scaled Dot Product Attention)
        if self.use_sdpa and hasattr(torch.nn.functional, 'scaled_dot_product_attention'):
            # 設置 SDPA 後端（Flash Attention / Memory Efficient / Math）
            try:
                torch.backends.cuda.enable_flash_sdp(self.enable_flash)
                torch.backends.cuda.enable_mem_efficient_sdp(self.enable_mem_efficient)
                torch.backends.cuda.enable_math_sdp(self.enable_math)
                debug_log(2, f"   ⚡ 已配置 SDPA (Flash={self.enable_flash}, MemEff={self.enable_mem_efficient}, Math={self.enable_math})")
            except Exception as e:
                debug_log(1, f"   ⚠️  SDPA 配置失敗: {e}")
        
        # ── 情緒推論配置（從 tts_module/config.yaml 讀取）──
        _emo_cfg = self.cfg.get('emotion', {})
        self.emo_max_strength = float(_emo_cfg.get('max_strength', 0.8))
        self.emo_bias = _emo_cfg.get('emo_bias', None)
        if self.emo_bias is not None:
            self.emo_bias = [float(x) for x in self.emo_bias]
        self.emo_bias_cap = float(_emo_cfg.get('emo_bias_cap', 0.8))
        self.merge_alpha = float(_emo_cfg.get('merge_alpha', 1.0))

        # ── GPT 採樣預設值 ──
        _samp_cfg = self.cfg.get('gpt_sampling', {})
        self.default_do_sample = bool(_samp_cfg.get('do_sample', True))
        self.default_temperature = float(_samp_cfg.get('temperature', 0.8))
        self.default_top_p = float(_samp_cfg.get('top_p', 1.0))
        self.default_top_k = int(_samp_cfg.get('top_k', 50))
        self.default_num_beams = int(_samp_cfg.get('num_beams', 1))
        self.default_repetition_penalty = float(_samp_cfg.get('repetition_penalty', 10.0))
        self.default_length_penalty = float(_samp_cfg.get('length_penalty', 1.0))

        # ── 合成管線配置 ──
        _synth_cfg = self.cfg.get('synthesis', {})
        self.interval_silence_ms = float(_synth_cfg.get('interval_silence_ms', 200))
        self.s2mel_stretch_ratio = float(_synth_cfg.get('s2mel_stretch_ratio', 1.72))
        self.cfm_n_timesteps = int(_synth_cfg.get('cfm_n_timesteps', 25))
        self.cfm_inference_cfg_rate = float(_synth_cfg.get('cfm_inference_cfg_rate', 0.7))

        # 當前加載的角色特徵
        self.current_character = None
        self.character_features = None

        # Profiling: init timings (populated by _init_models / load_character / warmup)
        self._init_timings: dict[str, float] = {}

        # 初始化模型
        self._init_models()
        
        # 保存預熱配置（稍後在加載角色後執行）
        self._warmup_config = {
            'enable': adv_opt.get('enable_warmup', True),
            'text': adv_opt.get('warmup_text', 'This is a test generation for warmup.'),
            'iterations': adv_opt.get('warmup_iterations', 1)
        }
        
        info_log("✅ IndexTTS Lite Engine 初始化完成!")
    
    def _warmup_models(self, warmup_text: str = "你好", iterations: int = 1):
        """
        🔥 模型預熱，提前觸發 torch.compile 編譯和 CUDA kernel 編譯
        
        Args:
            warmup_text: 預熱用的文本（簡短即可）
            iterations: 預熱次數
        """
        info_log(f"🔥 [預熱] 開始模型預熱... (文本: '{warmup_text}', 次數: {iterations})")
        
        import tempfile
        import time
        
        start_time = time.time()
        
        try:
            # 創建臨時檔案
            with tempfile.NamedTemporaryFile(mode='w', suffix='.wav', delete=False) as tmp_file:
                tmp_path = tmp_file.name
            
            # 執行預熱生成
            for i in range(iterations):
                debug_log(3, f"   [預熱] 第 {i+1}/{iterations} 次...")
                
                # 呼叫 synthesize 方法（不顯示詳細訊息）
                self.synthesize(
                    text=warmup_text,
                    output_path=tmp_path,
                    emotion_vector=None,
                    verbose=False  # 關閉詳細輸出
                )
            
            # 刪除臨時檔案
            try:
                import os
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    debug_log(3, f"   [預熱] 已刪除臨時檔案: {tmp_path}")
            except Exception as e:
                debug_log(1, f"   [預熱] 刪除臨時檔案失敗: {e}")
            
            elapsed = time.time() - start_time
            self._init_timings["warmup_ms"] = elapsed * 1000
            info_log(f"✅ [預熱] 完成! 耗時: {elapsed:.2f}秒")
            info_log(f"   ✓ torch.compile 已編譯")
            info_log(f"   ✓ CUDA kernel 已編譯")
            info_log(f"   ✓ 後續推論將明顯加速")
            
        except Exception as e:
            error_log(f"⚠️  [預熱] 失敗: {e}")
            debug_log(1, "   系統將繼續使用，但首次推論會較慢")
            import traceback
            debug_log(3, f"   預熱錯誤詳情:\n{traceback.format_exc()}")
    
    def _init_models(self):
        """初始化必要的模型組件"""
        debug_log(2, "🚀 初始化模型...")

        # 1. GPT 模型
        debug_log(2, "   [1/4] 加載 GPT 模型...")
        _t0 = time.perf_counter()
        gpt_path = os.path.join(self.model_dir, self.cfg.gpt_checkpoint)
        # 使用字典展開來傳遞所有 GPT 配置參數(包括嵌套的 condition_module 等)
        gpt_kwargs = dict(self.cfg.gpt)
        gpt_kwargs['use_accel'] = self.use_accel
        self.gpt = UnifiedVoice(**gpt_kwargs).to(self.device)

        self.gpt.load_state_dict(torch.load(gpt_path, map_location=self.device, weights_only=True))

        if self.use_fp16:
            self.gpt.eval().half()
        else:
            self.gpt.eval()

        # 初始化 GPT inference model
        self.gpt.post_init_gpt2_config(use_deepspeed=False, kv_cache=True, half=self.use_fp16)

        # 🚀 應用 torch.compile 優化 GPT（如果配置啟用）
        if self.use_torch_compile and hasattr(torch, 'compile'):
            try:
                debug_log(2, f"   ⚡ 對 GPT 應用 torch.compile (mode={self.compile_mode})")
                # 編譯 inference_model 而非整個 gpt
                self.gpt.inference_model = torch.compile(
                    self.gpt.inference_model,
                    mode=self.compile_mode,
                    fullgraph=False  # 允許部分圖編譯
                )
            except Exception as e:
                debug_log(1, f"   ⚠️  torch.compile GPT 失敗，回退: {e}")
        self._init_timings["gpt_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ GPT 加載完成: {gpt_path}")

        # 2. Semantic Codec (用於 GPT 輸出解碼)
        debug_log(2, "   [2/4] 加載 Semantic Codec...")
        _t0 = time.perf_counter()
        semantic_codec = build_semantic_codec(self.cfg.semantic_codec)
        semantic_code_ckpt = hf_hub_download("amphion/MaskGCT", filename="semantic_codec/model.safetensors")
        safetensors.torch.load_model(semantic_codec, semantic_code_ckpt)
        self.semantic_codec = semantic_codec.to(self.device)
        self.semantic_codec.eval()
        self._init_timings["semantic_codec_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ Semantic Codec 加載完成")

        # 3. S2Mel 模型
        debug_log(2, "   [3/4] 加載 S2Mel 模型...")
        _t0 = time.perf_counter()
        s2mel_path = os.path.join(self.model_dir, self.cfg.s2mel_checkpoint)
        s2mel = MyModel(self.cfg.s2mel, use_gpt_latent=True)
        s2mel, _, _, _ = load_checkpoint2(s2mel, None, s2mel_path)
        self.s2mel = s2mel.to(self.device)
        # 初始化 GPT-Fast cache (參考 infer_v2.py line 139)
        self.s2mel.models['cfm'].estimator.setup_caches(max_batch_size=1, max_seq_length=8192)
        self.s2mel.eval()

        # 🚀 應用 torch.compile 優化 (如果配置啟用)
        if self.use_torch_compile and hasattr(torch, 'compile'):
            try:
                debug_log(2, f"   ⚡ 對 S2Mel 應用 torch.compile (mode={self.compile_mode})")
                self.s2mel = torch.compile(self.s2mel, mode=self.compile_mode)
            except Exception as e:
                debug_log(1, f"   ⚠️  torch.compile S2Mel 失敗，回退: {e}")

        # 🚀 可選: 應用動態量化 (INT8)
        if self.use_dynamic_quantization and self.quantize_s2mel:
            try:
                debug_log(2, "   ⚡ 對 S2Mel 應用動態量化 (INT8)")
                self.s2mel = torch.quantization.quantize_dynamic(
                    self.s2mel,
                    {torch.nn.Linear},
                    dtype=torch.qint8
                )
            except Exception as e:
                debug_log(1, f"   ⚠️  S2Mel 量化失敗，回退: {e}")
        # S2Mel torch.compile: 在 sm_120 (RTX 50 系列) + triton-windows 上
        # inductor 會產生錯誤結果 (dynamic=True) 或大幅變慢 (static/reduce-overhead)。
        # S2Mel 僅佔 12% 時間，compile 收益不足以抵消風險，預設關閉。
        # 若日後 triton-windows 修正 sm_120 支援，可透過 config.yaml 啟用：
        #   advanced_optimization.compile_s2mel: true
        if self.cfg.get('advanced_optimization', {}).get('compile_s2mel', False):
            if hasattr(self.s2mel, 'enable_torch_compile'):
                try:
                    debug_log(2, "   ⚡ 對 S2Mel CFM estimator 應用 torch.compile")
                    self.s2mel.enable_torch_compile()
                except Exception as e:
                    debug_log(1, f"   ⚠️  S2Mel CFM torch.compile 失敗，回退: {e}")
        else:
            debug_log(2, "   ℹ️  S2Mel torch.compile 已停用 (sm_120 相容性)")

        self._init_timings["s2mel_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ S2Mel 加載完成: {s2mel_path}")

        # 4. BigVGAN Vocoder
        debug_log(2, "   [4/4] 加載 BigVGAN Vocoder...")
        _t0 = time.perf_counter()
        bigvgan_name = self.cfg.vocoder.name
        self.bigvgan = bigvgan.BigVGAN.from_pretrained(bigvgan_name, use_cuda_kernel=self.use_cuda_kernel)
        self.bigvgan = self.bigvgan.to(self.device)
        self.bigvgan.remove_weight_norm()
        self.bigvgan.eval()

        # 🚀 應用 torch.compile 優化 (如果配置啟用)
        if self.use_torch_compile and hasattr(torch, 'compile'):
            try:
                debug_log(2, f"   ⚡ 對 BigVGAN 應用 torch.compile (mode={self.compile_mode})")
                self.bigvgan = torch.compile(self.bigvgan, mode=self.compile_mode)
            except Exception as e:
                debug_log(1, f"   ⚠️  torch.compile BigVGAN 失敗，回退: {e}")
        self._init_timings["bigvgan_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ BigVGAN 加載完成: {bigvgan_name}")

        # 5. 情感和說話人矩陣 (用於 emo_vector 映射)
        debug_log(2, "   [5/6] 加載情感和說話人矩陣...")
        _t0 = time.perf_counter()
        emo_matrix = torch.load(os.path.join(self.model_dir, self.cfg.emo_matrix), map_location=self.device, weights_only=True)
        spk_matrix = torch.load(os.path.join(self.model_dir, self.cfg.spk_matrix), map_location=self.device, weights_only=True)

        self.emo_matrix = emo_matrix.to(self.device)
        self.spk_matrix = spk_matrix.to(self.device)
        self.emo_num = list(self.cfg.emo_num)

        # Split 矩陣 (用於按情感類別索引)
        self.emo_matrix = torch.split(self.emo_matrix, self.emo_num)
        self.spk_matrix = torch.split(self.spk_matrix, self.emo_num)
        self._init_timings["emo_matrix_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ 矩陣加載完成: {self.cfg.emo_matrix}, {self.cfg.spk_matrix}")

        # 6. 文本標準化器和 BPE Tokenizer (參考 infer_v2.py line 159-162)
        debug_log(2, "   [6/6] 加載文本處理...")
        _t0 = time.perf_counter()
        bpe_path = os.path.join(self.model_dir, self.cfg.dataset["bpe_model"])
        # Glossary: 從 config 讀取是否啟用、以及 glossary 檔案路徑
        _text_cfg = self.cfg.get('text_processing', {})
        _enable_glossary = _text_cfg.get('enable_glossary', False)
        _glossary_path = _text_cfg.get('glossary_path', '')

        self.text_normalizer = TextNormalizer(enable_glossary=_enable_glossary)
        self.text_normalizer.load()

        # 載入術語詞彙表（若有指定路徑）
        if _enable_glossary and _glossary_path:
            abs_glossary = os.path.join(self.model_dir, _glossary_path) if not os.path.isabs(_glossary_path) else _glossary_path
            if self.text_normalizer.load_glossary_from_yaml(abs_glossary):
                debug_log(3, f"      ✓ 術語詞彙表已載入: {abs_glossary}")
            else:
                debug_log(3, f"      ⚠️  術語詞彙表未找到: {abs_glossary}")

        self.tokenizer = TextTokenizer(bpe_path, self.text_normalizer)
        self._init_timings["text_processor_ms"] = (time.perf_counter() - _t0) * 1000
        debug_log(3, f"      ✓ 文本處理器和 BPE 模型加載完成: {bpe_path}")
    
    def load_character(self, character_path: Union[str, Path], verbose: bool = True):
        """
        加載預提取的角色特徵
        
        Args:
            character_path: 角色特徵文件路徑 (.pt)
            verbose: 是否打印詳細信息
            
        Returns:
            bool: 是否加載成功
        """
        character_path = Path(character_path)
        _char_t0 = time.perf_counter()

        if not character_path.exists():
            raise FileNotFoundError(f"角色文件不存在: {character_path}")

        if verbose:
            debug_log(2, f"📂 加載角色: {character_path.name}")

        try:
            # 加載特徵
            features = torch.load(character_path, map_location=self.device, weights_only=False)
            
            # 驗證必要字段
            required_fields = ['spk_cond_emb', 'style', 'prompt_condition', 'ref_mel']
            missing_fields = [f for f in required_fields if f not in features]
            
            if missing_fields:
                raise ValueError(f"角色文件缺少必要字段: {missing_fields}")
            
            # 如果使用 fp16,將浮點數特徵轉換為 half
            if self.use_fp16:
                for key in ['spk_cond_emb', 'style', 'prompt_condition', 'ref_mel']:
                    if key in features and features[key].dtype in [torch.float32, torch.float64]:
                        features[key] = features[key].half()
            
            # 檢查情感索引 (emo_indices)
            if 'emo_indices' not in features:
                if verbose:
                    debug_log(2, "   ⚠️  警告: 此角色文件沒有 emo_indices,將使用全零向量")
                features['emo_indices'] = torch.zeros(8, dtype=torch.long).to(self.device)
            elif isinstance(features['emo_indices'], list):
                # 如果是 list,轉換為 tensor
                features['emo_indices'] = torch.tensor(features['emo_indices'], dtype=torch.long).to(self.device)
            
            self.character_features = features
            self.current_character = character_path.stem
            
            if verbose:
                info_log(f"   ✓ 角色 '{self.current_character}' 加載成功!")
                
                # 加載 metadata (如果存在)
                metadata_path = character_path.with_suffix('.pt_metadata.json')
                if metadata_path.exists():
                    import json
                    with open(metadata_path, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    debug_log(3, f"   📋 提取時間: {metadata.get('extraction_time', 'N/A')}")
                    debug_log(3, f"   📋 音頻長度: {metadata.get('audio_duration', 'N/A')}")
                    if 'emo_indices' in metadata:
                        debug_log(3, f"   📋 情感索引: {metadata['emo_indices']}")
            
            self._init_timings["char_load_ms"] = (time.perf_counter() - _char_t0) * 1000

            # 🔥 角色加載成功後，執行預熱（如果這是首次加載且已配置）
            if hasattr(self, '_warmup_config') and self._warmup_config.get('enable', False):
                # 執行一次預熱後就移除配置，避免重複預熱
                warmup_cfg = self._warmup_config
                delattr(self, '_warmup_config')  # 移除配置標記
                
                self._warmup_models(
                    warmup_text=warmup_cfg['text'],
                    iterations=warmup_cfg['iterations']
                )
            
            return True
            
        except Exception as e:
            error_log(f"❌ 加載角色失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def normalize_emotion_vector(
        self,
        emotion_vector: List[float],
        max_strength: float = 0.3,
        verbose: bool = False
    ) -> List[float]:
        """
        歸一化情感向量,確保不會覆蓋原始聲音特徵
        
        Args:
            emotion_vector: 8維情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
            max_strength: 最大情感強度 (0-1),推薦 0.3 保留 70% 原始聲音
            verbose: 是否打印信息
            
        Returns:
            歸一化後的情感向量
        """
        current_sum = sum(emotion_vector)
        
        if current_sum == 0 or current_sum <= max_strength:
            return emotion_vector
        
        # 比例壓縮
        scale_factor = max_strength / current_sum
        normalized = [v * scale_factor for v in emotion_vector]
        
        if verbose:
            debug_log(3, f"   📊 情感歸一化: {current_sum:.2f} → {sum(normalized):.2f}")
            debug_log(3, f"      原始聲音保留: {(1 - sum(normalized)) * 100:.0f}%")
        
        return normalized
    
    def synthesize_direct(
        self,
        text: str,
        output_path: str,
        emotion_vector: Optional[List[float]] = None,
        max_emotion_strength: float = 0.3,
        language: str = 'en',
        # GPT 優化參數
        num_beams: int = 1,
        do_sample: bool = True,
        temperature: float = 0.6,
        top_p: float = 0.9,
        top_k: int = 20,
        verbose: bool = True
    ) -> bool:
        """
        【已廢棄】直接手動調用各個模型步驟的合成方法
        
        此方法存在問題:
        1. 手動實現容易出錯
        2. 可能觸發 CUDA kernel 錯誤
        3. 可能導致無限循環
        
        請使用 synthesize() 方法代替!
        """
        raise DeprecationWarning("請使用 synthesize() 方法,它使用 IndexTTS2 的內部邏輯更穩定")
    
    def synthesize(
        self,
        text: str,
        output_path: str,
        emotion_vector: Optional[List[float]] = None,
        max_emotion_strength: Optional[float] = None,
        language: str = 'en',
        num_beams: Optional[int] = None,
        do_sample: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        length_penalty: Optional[float] = None,
        verbose: bool = True,
        on_segment_audio=None,
        max_text_tokens_per_segment: int = 120,
        quick_streaming_tokens: int = 0,
    ) -> bool:
        """
        合成語音 — 回傳 bool，保持向後相容。
        內部委派給 _synthesize_impl()。
        所有採樣參數預設為 None → 從 config.yaml (gpt_sampling) 讀取。

        Args:
            on_segment_audio: Optional callback(segment_audio_tensor, segment_index, total_segments).
                Called after each segment's vocoder completes, before concatenation.
                Use for streaming playback.
            max_text_tokens_per_segment: Maximum BPE tokens per segment (default 120, matches upstream).
            quick_streaming_tokens: If > 0, first segment will be at most this many tokens
                for faster first-audio latency.
        """
        success, _ = self._synthesize_impl(
            text=text, output_path=output_path,
            emotion_vector=emotion_vector, max_emotion_strength=max_emotion_strength,
            language=language, num_beams=num_beams, do_sample=do_sample,
            temperature=temperature, top_p=top_p, top_k=top_k,
            repetition_penalty=repetition_penalty, length_penalty=length_penalty,
            verbose=verbose, collect_timings=False,
            on_segment_audio=on_segment_audio,
            max_text_tokens_per_segment=max_text_tokens_per_segment,
            quick_streaming_tokens=quick_streaming_tokens,
        )
        return success

    def synthesize_with_profile(
        self,
        text: str,
        output_path: str,
        emotion_vector: Optional[List[float]] = None,
        max_emotion_strength: Optional[float] = None,
        language: str = 'en',
        num_beams: Optional[int] = None,
        do_sample: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        length_penalty: Optional[float] = None,
        verbose: bool = True,
        max_text_tokens_per_segment: int = 120,
        quick_streaming_tokens: int = 0,
    ) -> tuple:
        """
        合成語音並回傳 (success: bool, timings: dict)。
        timings 包含各階段的毫秒計時。
        """
        return self._synthesize_impl(
            text=text, output_path=output_path,
            emotion_vector=emotion_vector, max_emotion_strength=max_emotion_strength,
            language=language, num_beams=num_beams, do_sample=do_sample,
            temperature=temperature, top_p=top_p, top_k=top_k,
            repetition_penalty=repetition_penalty, length_penalty=length_penalty,
            verbose=verbose, collect_timings=True,
            max_text_tokens_per_segment=max_text_tokens_per_segment,
            quick_streaming_tokens=quick_streaming_tokens,
        )

    def _synthesize_impl(
        self,
        text: str,
        output_path: str,
        emotion_vector: Optional[List[float]] = None,
        max_emotion_strength: Optional[float] = None,
        language: str = 'en',
        num_beams: Optional[int] = None,
        do_sample: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        length_penalty: Optional[float] = None,
        verbose: bool = True,
        collect_timings: bool = False,
        on_segment_audio=None,
        max_text_tokens_per_segment: int = 120,
        quick_streaming_tokens: int = 0,
    ) -> tuple:
        """
        合成語音的核心實現 (獨立引擎實現,參考 infer_v2.py 邏輯)

        所有 Optional 參數為 None 時自動從 config.yaml 讀取預設值。

        Args:
            text: 要合成的文本
            output_path: 輸出音頻路徑
            emotion_vector: 8維情感向量
            max_emotion_strength: 最大情感強度 (None → config emotion.max_strength)
            language: 語言 ('en' 或 'zh')
            num_beams: GPT beam search 大小
            do_sample: 是否採樣 (None → config gpt_sampling.do_sample)
            temperature: 採樣溫度 (None → config gpt_sampling.temperature)
            top_p: Nucleus 採樣
            top_k: Top-K 採樣
            repetition_penalty: 重複懲罰 (None → config gpt_sampling.repetition_penalty)
            length_penalty: 長度懲罰 (None → config gpt_sampling.length_penalty)
            verbose: 是否打印詳細信息
            collect_timings: 是否收集各階段計時

        Returns:
            (success: bool, timings: dict) — timings 為空 dict 若 collect_timings=False
        """
        # ── 參數 fallback：None → config 預設值 ──
        if max_emotion_strength is None:
            max_emotion_strength = self.emo_max_strength
        if do_sample is None:
            do_sample = self.default_do_sample
        if temperature is None:
            temperature = self.default_temperature
        if top_p is None:
            top_p = self.default_top_p
        if top_k is None:
            top_k = self.default_top_k
        if num_beams is None:
            num_beams = self.default_num_beams
        if repetition_penalty is None:
            repetition_penalty = self.default_repetition_penalty
        if length_penalty is None:
            length_penalty = self.default_length_penalty

        timings: dict[str, float] = {}

        def _mark(key: str, t0: float) -> float:
            now = time.perf_counter()
            if collect_timings:
                timings[key] = (now - t0) * 1000
            return now

        if self.character_features is None:
            raise RuntimeError("請先使用 load_character() 加載角色!")

        wall_t0 = time.perf_counter()

        if verbose:
            debug_log(2, f"🎙️  合成語音...")
            debug_log(3, f"   角色: {self.current_character}")
            debug_log(3, f"   文本: {text}")
            debug_log(3, f"   文本長度: {len(text)} 字符")
            debug_log(3, f"   採樣: do_sample={do_sample}, T={temperature}, rep_penalty={repetition_penalty}")

        # 1. 情感向量處理
        if emotion_vector is None:
            emotion_vector = [0.0] * 8  # 中性

        if len(emotion_vector) != 8:
            raise ValueError("情感向量必須是 8 維")

        # 套用 emo_bias（上游情緒偏差：壓制容易出怪聲的情緒維度）
        if self.emo_bias is not None and any(v > 0 for v in emotion_vector):
            biased = [v * b for v, b in zip(emotion_vector, self.emo_bias)]
            biased_sum = sum(biased)
            if biased_sum > self.emo_bias_cap:
                scale = self.emo_bias_cap / biased_sum
                biased = [v * scale for v in biased]
            emotion_vector = biased
            if verbose:
                debug_log(3, f"   🎭 emo_bias 套用後: {[f'{v:.3f}' for v in emotion_vector]}, sum={sum(emotion_vector):.3f}")

        # 歸一化情感向量
        normalized_emotion = self.normalize_emotion_vector(emotion_vector, max_emotion_strength, verbose)

        try:
            # 2. 文本處理 — tokenize + 分段 (參考 infer_v2.py line 487-523)
            _t = time.perf_counter()
            if verbose:
                debug_log(3, "   [1/4] 文本處理...")

            # Custom vocabulary replacements (before segmentation and BPE)
            _vocab_cfg = self.cfg.get('text_processing', {}).get('custom_vocab', {})
            text = apply_custom_vocab(text, _vocab_cfg)

            # Language-aware semantic pre-segmentation (before BPE tokenization)
            if language == 'zh':
                _jieba_cfg = self.cfg.get('chunking', {}).get('jieba_segmentation', {})
                sub_texts = pre_segment_chinese(text, _jieba_cfg)
                if len(sub_texts) > 1 and verbose:
                    debug_log(2, f"      🈶 jieba 語義預切分: {len(sub_texts)} 段")
            else:
                _en_cfg = self.cfg.get('chunking', {}).get('english_segmentation', {})
                sub_texts = pre_segment_english(text, _en_cfg)
                if len(sub_texts) > 1 and verbose:
                    debug_log(2, f"      🔤 pySBD 句子預切分: {len(sub_texts)} 段")

            # Tokenize each sub-text independently, then split into segments
            all_tokens: list = []
            for sub_text in sub_texts:
                all_tokens.extend(self.tokenizer.tokenize(sub_text))

            segments = self.tokenizer.split_segments(
                all_tokens,
                max_text_tokens_per_segment=max_text_tokens_per_segment,
                quick_streaming_tokens=quick_streaming_tokens,
            )

            # UNK token 防線：移除 BPE 詞彙表中不存在的 tokens，防止 GPT 產生亂音
            segments = self.tokenizer.sanitize_unk_tokens(segments, verbose=verbose)

            num_segments = len(segments)
            if num_segments > 1 and verbose:
                debug_log(2, f"      ℹ️  文本分為 {num_segments} 段處理")
                if quick_streaming_tokens > 0:
                    debug_log(2, f"      ⚡ 首段快速串流: ≤{quick_streaming_tokens} tokens")
            _t = _mark("text_norm_ms", _t)

            # 3. 情感向量映射 (跨段共用, 只計算一次)
            if 'emo_indices' in self.character_features:
                emo_indices = self.character_features['emo_indices']
            else:
                style = self.character_features['style']
                emo_indices = []
                for spk_mat in self.spk_matrix:
                    query_norm = torch.nn.functional.normalize(style.squeeze(0), p=2, dim=-1)
                    matrix_norm = torch.nn.functional.normalize(spk_mat, p=2, dim=-1)
                    similarities = torch.matmul(query_norm, matrix_norm.T)
                    most_similar_idx = torch.argmax(similarities).item()
                    emo_indices.append(most_similar_idx)

            weight_vector = torch.tensor(normalized_emotion, device=self.device)
            emo_matrix_selected = [emo_mat[idx].unsqueeze(0) for idx, emo_mat in zip(emo_indices, self.emo_matrix)]
            emo_matrix_cat = torch.cat(emo_matrix_selected, 0)
            emovec_mat = weight_vector.unsqueeze(1) * emo_matrix_cat
            emovec_mat = torch.sum(emovec_mat, 0).unsqueeze(0)

            if self.use_fp16:
                emovec_mat = emovec_mat.half()
            _t = _mark("emotion_ms", _t)

            # Shared conditioning (跨段共用)
            spk_cond_emb = self.character_features['spk_cond_emb']
            emo_cond_emb = spk_cond_emb

            # --- 分段循環：每段獨立走 GPT→S2Mel→BigVGAN 管線 ---
            audio_segments: list = []
            seg_gpt_semantic_ms = 0.0
            seg_gpt_latent_ms = 0.0
            seg_s2mel_cfm_ms = 0.0
            seg_vocoder_ms = 0.0
            first_segment_done_ms: Optional[float] = None

            # Anomaly detection config
            _anomaly_cfg = self.cfg.get('synthesis', {}).get('anomaly_detection', {})
            _anomaly_enabled = _anomaly_cfg.get('enabled', True)
            _max_retries = int(_anomaly_cfg.get('max_retries', 1))
            _max_audio_ratio = float(_anomaly_cfg.get('max_audio_ratio', 3.0))
            _max_segment_rtf = float(_anomaly_cfg.get('max_segment_rtf', 5.0))
            guard_stats = SegmentGuardStats()

            # --- Adaptive Segment Emotion (實驗性) ---
            # 為多段文本的每段生成微幅不同的情緒偏移，提升段落間的語調連續性。
            # 策略：以 base emovec_mat 為中心，沿段落位置施加平滑的 cosine 弧線偏移。
            # 單段文本不受影響。
            adaptive_emotion = (num_segments > 1)
            if adaptive_emotion:
                # 預生成每段的情緒偏移因子（cosine arc: 中間段稍高能量，首尾段回歸基調）
                import math
                segment_emotion_scales = []
                for i in range(num_segments):
                    # Cosine arc: 0 → π 映射，中間段 scale 最大
                    t = i / max(num_segments - 1, 1)
                    # 微幅 arc：基礎 1.0 ± 0.05 的 cosine 波動
                    arc_scale = 1.0 + 0.05 * math.sin(math.pi * t)
                    segment_emotion_scales.append(arc_scale)
                if verbose:
                    debug_log(2, f"      🎭 自適應語感: {num_segments} 段, 偏移範圍 ±5%")

            # Dynamic length config (from config.yaml → gpt.dynamic_length.*)
            _dyn_cfg = self.cfg.get('gpt', {}).get('dynamic_length', {})
            _mel_per_token_ratio = float(_dyn_cfg.get('mel_per_token_ratio', 10.0))
            _safety_margin = int(_dyn_cfg.get('safety_margin', 150))
            _min_gen_len = int(_dyn_cfg.get('min_generate_length', 300))

            for seg_idx, seg_tokens in enumerate(segments):
                if verbose and num_segments > 1:
                    debug_log(3, f"   ── 段落 {seg_idx + 1}/{num_segments} ──")
                    # Show decoded segment text for debugging (decode back to readable text)
                    try:
                        seg_ids = self.tokenizer.convert_tokens_to_ids(seg_tokens)
                        seg_preview = self.tokenizer.decode(seg_ids, do_lower_case=True)
                    except Exception:
                        seg_preview = "".join(seg_tokens).replace("▁", " ").strip()
                    if len(seg_preview) > 100:
                        seg_preview = seg_preview[:97] + "..."
                    debug_log(3, f"      📝 \"{seg_preview}\"")

                text_tokens = self.tokenizer.convert_tokens_to_ids(seg_tokens)
                text_tokens = torch.tensor(text_tokens, dtype=torch.int32, device=self.device).unsqueeze(0)

                # 計算動態 max_generate_length（基於本段 text token 數）
                seg_max_gen_len = calc_dynamic_max_generate_length(
                    num_text_tokens=len(seg_tokens),
                    cfg_max_mel_tokens=self.cfg.gpt.max_mel_tokens,
                    mel_per_token_ratio=_mel_per_token_ratio,
                    safety_margin=_safety_margin,
                    min_generate_length=_min_gen_len,
                )
                if verbose:
                    debug_log(3, f"      📏 動態長度上限: {seg_max_gen_len} mel tokens (text tokens: {len(seg_tokens)})")

                # 4. GPT 生成語義 tokens
                if verbose:
                    debug_log(3, f"   [2/4] GPT 生成中...")

                _seg_t = time.perf_counter()
                with torch.no_grad():
                    dtype = torch.float16 if self.use_fp16 else None
                    with torch.amp.autocast(self.device.type, enabled=dtype is not None, dtype=dtype):
                        emovec = self.gpt.merge_emovec(
                            spk_cond_emb,
                            emo_cond_emb,
                            torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                            torch.tensor([emo_cond_emb.shape[-1]], device=self.device),
                            alpha=self.merge_alpha
                        )

                        # 套用 per-segment 情緒偏移
                        seg_emovec_mat = emovec_mat
                        if adaptive_emotion and emovec_mat is not None:
                            scale = segment_emotion_scales[seg_idx]
                            seg_emovec_mat = emovec_mat * scale

                        if seg_emovec_mat is not None:
                            weight_sum = torch.sum(torch.tensor(normalized_emotion, device=self.device))
                            emovec = seg_emovec_mat + (1 - weight_sum) * emovec

                        codes, speech_conditioning_latent = self.gpt.inference_speech(
                            spk_cond_emb,
                            text_tokens,
                            emo_cond_emb,
                            cond_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                            emo_cond_lengths=torch.tensor([emo_cond_emb.shape[-1]], device=self.device),
                            emo_vec=emovec,
                            num_return_sequences=1,
                            num_beams=num_beams,
                            do_sample=do_sample,
                            temperature=temperature,
                            top_p=top_p,
                            top_k=top_k,
                            length_penalty=length_penalty,
                            repetition_penalty=repetition_penalty,
                            max_generate_length=seg_max_gen_len,
                        )
                        seg_gpt_semantic_ms += (time.perf_counter() - _seg_t) * 1000
                        _seg_t = time.perf_counter()

                        # GPT forward 獲取 latent
                        use_speed = torch.zeros(1, device=self.device).long()
                        latent = self.gpt(
                            speech_conditioning_latent,
                            text_tokens,
                            torch.tensor([text_tokens.shape[-1]], device=self.device),
                            codes,
                            torch.tensor([codes.shape[-1]], device=self.device),
                            spk_cond_emb,
                            cond_mel_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                            emo_cond_mel_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                            emo_vec=seg_emovec_mat if adaptive_emotion else emovec_mat,
                            use_speed=use_speed
                        )
                        seg_gpt_latent_ms += (time.perf_counter() - _seg_t) * 1000
                        _seg_t = time.perf_counter()

                # 找到實際的 code 長度 + 壓縮過長靜音 (參考 infer_v2.py)
                stop_mel_token = self.cfg.gpt.stop_mel_token
                codes, code_lens = self._remove_long_silence(
                    codes, stop_mel_token, silent_token=52,
                    max_consecutive=30, keep_consecutive=10,
                )

                # 5. S2Mel 生成 Mel 頻譜
                if verbose:
                    debug_log(3, f"   [3/4] S2Mel 生成中...")

                with torch.no_grad():
                    dtype = None
                    with torch.amp.autocast(self.device.type, enabled=dtype is not None, dtype=dtype):
                        latent = self.s2mel.models['gpt_layer'](latent)
                        S_infer = self.semantic_codec.quantizer.vq2emb(codes.unsqueeze(1))
                        S_infer = S_infer.transpose(1, 2)

                        if latent.shape[1] > S_infer.shape[1]:
                            latent = latent[:, :S_infer.shape[1], :]

                        S_infer = S_infer + latent
                        target_lengths = (code_lens * self.s2mel_stretch_ratio).long()

                    cond = self.s2mel.models['length_regulator'](
                        S_infer,
                        ylens=target_lengths,
                        n_quantizers=3,
                        f0=None
                    )[0]

                    cat_condition = torch.cat([self.character_features['prompt_condition'], cond], dim=1)

                    vc_target = self.s2mel.models['cfm'].inference(
                        cat_condition,
                        torch.LongTensor([cat_condition.size(1)]).to(self.device),
                        self.character_features['ref_mel'],
                        self.character_features['style'],
                        None,
                        n_timesteps=self.cfm_n_timesteps,
                        inference_cfg_rate=self.cfm_inference_cfg_rate
                    )

                    mel = vc_target[:, :, self.character_features['ref_mel'].size(-1):]
                seg_s2mel_cfm_ms += (time.perf_counter() - _seg_t) * 1000
                _seg_t = time.perf_counter()

                # 6. BigVGAN 生成波形
                if verbose:
                    debug_log(3, f"   [4/4] BigVGAN 生成中...")
                _seg_t = time.perf_counter()  # 重設起點，排除 verbose log 開銷

                with torch.no_grad():
                    seg_audio_gpu = self.bigvgan(mel.float()).squeeze(0)
                seg_vocoder_ms += (time.perf_counter() - _seg_t) * 1000
                seg_audio = seg_audio_gpu.cpu()  # D2H transfer 不計入 vocoder 時間

                # --- Anomaly detection (before trim, so we check raw length) ---
                if _anomaly_enabled:
                    _seg_gpt_time_ms = seg_gpt_semantic_ms  # cumulative, but for this segment only the last delta matters
                    anomaly_result = check_segment_anomaly(
                        audio_samples=seg_audio.shape[-1],
                        sample_rate=22050,
                        num_text_tokens=len(seg_tokens),
                        gpt_gen_time_ms=_seg_gpt_time_ms,
                        max_audio_ratio=_max_audio_ratio,
                        max_segment_rtf=_max_segment_rtf,
                    )
                    if anomaly_result.is_anomalous:
                        guard_stats.anomaly_count += 1
                        guard_stats.anomalous_segments.append(seg_idx)
                        if verbose:
                            debug_log(1, f"   ⚠️  段落 {seg_idx+1} 異常: {anomaly_result.reason}")

                # Trim trailing silence from segment (GPT sometimes generates
                # long silence runs before the stop token)
                seg_audio = self._trim_trailing_silence(seg_audio)

                audio_segments.append(seg_audio)

                # Record first-segment completion time
                if first_segment_done_ms is None:
                    first_segment_done_ms = (time.perf_counter() - wall_t0) * 1000

                # Streaming callback: emit segment audio immediately
                if on_segment_audio is not None:
                    on_segment_audio(seg_audio, seg_idx, num_segments)

            # --- 拼接所有段落的音頻（段間插入短靜音）---
            if len(audio_segments) == 1:
                audio_output = audio_segments[0]
            else:
                # Insert configurable silence between segments (default 200ms, upstream alignment)
                interval_samples = int(self.interval_silence_ms * 22050 / 1000)
                silence = torch.zeros(audio_segments[0].shape[0], interval_samples)
                parts = []
                for i, seg in enumerate(audio_segments):
                    parts.append(seg)
                    if i < len(audio_segments) - 1:
                        parts.append(silence)
                audio_output = torch.cat(parts, dim=-1)
                if verbose:
                    debug_log(3, f"   ✓ 已拼接 {len(audio_segments)} 段音頻（含段間靜音）")

            # Record accumulated stage timings
            if collect_timings:
                timings["gpt_semantic_ms"] = seg_gpt_semantic_ms
                timings["gpt_latent_ms"] = seg_gpt_latent_ms
                timings["s2mel_cfm_ms"] = seg_s2mel_cfm_ms
                timings["vocoder_ms"] = seg_vocoder_ms
                timings["segment_count"] = float(num_segments)
                timings["first_segment_ms"] = first_segment_done_ms or 0.0
                timings["anomaly_count"] = float(guard_stats.anomaly_count)
                timings["retry_count"] = float(guard_stats.retry_count)

            _t = time.perf_counter()

            # 7. 保存音頻
            import torchaudio
            torchaudio.save(
                output_path,
                audio_output,
                sample_rate=22050,
                encoding="PCM_S",
                bits_per_sample=16
            )
            _t = _mark("wav_save_ms", _t)

            if verbose:
                duration = audio_output.shape[-1] / 22050
                debug_log(2, f"   ✓ 合成完成!")
                debug_log(3, f"   📁 保存至: {output_path}")
                debug_log(3, f"   ⏱️  音頻時長: {duration:.2f}秒")

            # Finalize timings
            if collect_timings:
                timings["text_chars"] = float(len(text))
                audio_duration_s = audio_output.shape[-1] / 22050
                timings["audio_duration_ms"] = audio_duration_s * 1000
                timings["total_wall_ms"] = (time.perf_counter() - wall_t0) * 1000

            return True, timings

        except Exception as e:
            error_log(f"❌ 合成失敗: {e}")
            import traceback
            traceback.print_exc()
            if collect_timings:
                timings["total_wall_ms"] = (time.perf_counter() - wall_t0) * 1000
            return False, timings
    
    def get_current_character(self) -> Optional[str]:
        """獲取當前加載的角色名稱"""
        return self.current_character
    
    def is_character_loaded(self) -> bool:
        """檢查是否已加載角色"""
        return self.character_features is not None