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
    from .s2mel.modules.commons import load_checkpoint2, MyModel
    from .s2mel.modules.bigvgan import bigvgan
except ImportError:
    # 回退到絕對導入
    from gpt.model_v2 import UnifiedVoice
    from tts_utils.maskgct_utils import build_semantic_model, build_semantic_codec
    from tts_utils.front import TextNormalizer, TextTokenizer
    from s2mel.modules.commons import load_checkpoint2, MyModel
    from s2mel.modules.bigvgan import bigvgan


class IndexTTSLite:
    """精簡版 IndexTTS2 推論引擎"""
    
    def __init__(
        self,
        cfg_path: str = "checkpoints/config.yaml",
        model_dir: str = "checkpoints",
        use_fp16: bool = True,
        device: Optional[str] = None,
        use_cuda_kernel: bool = False
    ):
        """
        初始化精簡推論引擎
        
        Args:
            cfg_path: 配置文件路徑
            model_dir: 模型目錄
            use_fp16: 是否使用半精度
            device: 設備 (None=自動檢測)
            use_cuda_kernel: 是否使用 CUDA kernel (需要 CUDA Toolkit)
        """
        self.model_dir = model_dir
        self.use_fp16 = use_fp16
        self.use_cuda_kernel = use_cuda_kernel
        
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
        
        # 加載配置
        self.cfg = OmegaConf.load(cfg_path)
        
        # 當前加載的角色特徵
        self.current_character = None
        self.character_features = None
        
        # 初始化模型
        self._init_models()
        
        info_log("✅ IndexTTS Lite Engine 初始化完成!")
    
    def _init_models(self):
        """初始化必要的模型組件"""
        debug_log(2, "🚀 初始化模型...")
        
        # 1. GPT 模型
        debug_log(2, "   [1/4] 加載 GPT 模型...")
        gpt_path = os.path.join(self.model_dir, self.cfg.gpt_checkpoint)
        # 使用字典展開來傳遞所有 GPT 配置參數(包括嵌套的 condition_module 等)
        self.gpt = UnifiedVoice(**self.cfg.gpt).to(self.device)
        
        self.gpt.load_state_dict(torch.load(gpt_path, map_location=self.device, weights_only=True))
        
        if self.use_fp16:
            self.gpt.eval().half()
        else:
            self.gpt.eval()
        
        self.gpt.post_init_gpt2_config(use_deepspeed=False, kv_cache=True, half=self.use_fp16)
        debug_log(3, f"      ✓ GPT 加載完成: {gpt_path}")
        
        # 2. Semantic Codec (用於 GPT 輸出解碼)
        debug_log(2, "   [2/4] 加載 Semantic Codec...")
        semantic_codec = build_semantic_codec(self.cfg.semantic_codec)
        semantic_code_ckpt = hf_hub_download("amphion/MaskGCT", filename="semantic_codec/model.safetensors")
        safetensors.torch.load_model(semantic_codec, semantic_code_ckpt)
        self.semantic_codec = semantic_codec.to(self.device)
        self.semantic_codec.eval()
        debug_log(3, f"      ✓ Semantic Codec 加載完成")
        
        # 3. S2Mel 模型
        debug_log(2, "   [3/4] 加載 S2Mel 模型...")
        s2mel_path = os.path.join(self.model_dir, self.cfg.s2mel_checkpoint)
        s2mel = MyModel(**self.cfg.s2mel, use_gpt_latent=True)
        s2mel, _, _, _ = load_checkpoint2(s2mel, None, s2mel_path)
        self.s2mel = s2mel.to(self.device)
        # 初始化 GPT-Fast cache (參考 infer_v2.py line 139)
        self.s2mel.models['cfm'].estimator.setup_caches(max_batch_size=1, max_seq_length=8192)
        self.s2mel.eval()
        debug_log(3, f"      ✓ S2Mel 加載完成: {s2mel_path}")
        
        # 4. BigVGAN Vocoder
        debug_log(2, "   [4/4] 加載 BigVGAN Vocoder...")
        bigvgan_name = self.cfg.vocoder.name
        self.bigvgan = bigvgan.BigVGAN.from_pretrained(bigvgan_name, use_cuda_kernel=self.use_cuda_kernel)
        self.bigvgan = self.bigvgan.to(self.device)
        self.bigvgan.remove_weight_norm()
        self.bigvgan.eval()
        debug_log(3, f"      ✓ BigVGAN 加載完成: {bigvgan_name}")
        
        # 5. 情感和說話人矩陣 (用於 emo_vector 映射)
        debug_log(2, "   [5/6] 加載情感和說話人矩陣...")
        emo_matrix = torch.load(os.path.join(self.model_dir, self.cfg.emo_matrix), map_location=self.device, weights_only=True)
        spk_matrix = torch.load(os.path.join(self.model_dir, self.cfg.spk_matrix), map_location=self.device, weights_only=True)
        
        self.emo_matrix = emo_matrix.to(self.device)
        self.spk_matrix = spk_matrix.to(self.device)
        self.emo_num = list(self.cfg.emo_num)
        
        # Split 矩陣 (用於按情感類別索引)
        self.emo_matrix = torch.split(self.emo_matrix, self.emo_num)
        self.spk_matrix = torch.split(self.spk_matrix, self.emo_num)
        debug_log(3, f"      ✓ 矩陣加載完成: {self.cfg.emo_matrix}, {self.cfg.spk_matrix}")
        
        # 6. 文本標準化器和 BPE Tokenizer (參考 infer_v2.py line 159-162)
        debug_log(2, "   [6/6] 加載文本處理...")
        bpe_path = os.path.join(self.model_dir, self.cfg.dataset["bpe_model"])
        self.text_normalizer = TextNormalizer()
        self.text_normalizer.load()
        self.tokenizer = TextTokenizer(bpe_path, self.text_normalizer)
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
        max_emotion_strength: float = 0.5,
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
        合成語音 (獨立引擎實現,參考 infer_v2.py 邏輯)
        
        Args:
            text: 要合成的文本
            output_path: 輸出音頻路徑
            emotion_vector: 8維情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
            max_emotion_strength: 最大情感強度 (0.5=保留50%原聲)
            language: 語言 ('en' 或 'zh')
            num_beams: GPT beam search 大小 (1=最快)
            do_sample: 是否採樣
            temperature: 採樣溫度
            top_p: Nucleus 採樣
            top_k: Top-K 採樣
            verbose: 是否打印詳細信息
            
        Returns:
            bool: 是否成功
        """
        if self.character_features is None:
            raise RuntimeError("請先使用 load_character() 加載角色!")
        
        if verbose:
            debug_log(2, f"🎙️  合成語音...")
            debug_log(3, f"   角色: {self.current_character}")
            debug_log(3, f"   文本: {text}")
            debug_log(3, f"   文本長度: {len(text)} 字符")
        
        # 1. 情感向量處理
        if emotion_vector is None:
            emotion_vector = [0.0] * 8  # 中性
        
        if len(emotion_vector) != 8:
            raise ValueError("情感向量必須是 8 維")
        
        # 歸一化情感向量
        normalized_emotion = self.normalize_emotion_vector(emotion_vector, max_emotion_strength, verbose)
        
        try:
            # 2. 文本處理 (參考 infer_v2.py line 487-523)
            if verbose:
                debug_log(3, "   [1/4] 文本處理...")
            
            # 使用 BPE tokenizer 進行正確的 tokenization
            text_tokens_list = self.tokenizer.tokenize(text)
            # 簡化版:只取第一個 segment (完整版應該循環處理所有 segments)
            segments = self.tokenizer.split_segments(text_tokens_list, max_text_tokens_per_segment=200)
            if len(segments) > 1 and verbose:
                debug_log(2, f"      ⚠️  警告: 文本被分割為 {len(segments)} 段,只處理第一段")
            
            # 轉換為 token IDs
            text_tokens = self.tokenizer.convert_tokens_to_ids(segments[0])
            text_tokens = torch.tensor(text_tokens, dtype=torch.int32, device=self.device).unsqueeze(0)
            
            # 3. 處理情感向量映射 (參考 infer_v2.py line 456-462)
            # 使用預計算的 emo_indices 或動態計算
            if 'emo_indices' in self.character_features:
                # 使用預計算的索引
                emo_indices = self.character_features['emo_indices']
            else:
                # 動態計算最相似的索引 (find_most_similar_cosine)
                style = self.character_features['style']
                emo_indices = []
                for spk_mat in self.spk_matrix:
                    # 計算餘弦相似度
                    query_norm = torch.nn.functional.normalize(style.squeeze(0), p=2, dim=-1)
                    matrix_norm = torch.nn.functional.normalize(spk_mat, p=2, dim=-1)
                    similarities = torch.matmul(query_norm, matrix_norm.T)
                    most_similar_idx = torch.argmax(similarities).item()
                    emo_indices.append(most_similar_idx)
            
            # 從 emo_matrix 中獲取對應的情感向量並加權
            weight_vector = torch.tensor(normalized_emotion, device=self.device)
            emo_matrix_selected = [emo_mat[idx].unsqueeze(0) for idx, emo_mat in zip(emo_indices, self.emo_matrix)]
            emo_matrix_cat = torch.cat(emo_matrix_selected, 0)  # [8, hidden_dim]
            emovec_mat = weight_vector.unsqueeze(1) * emo_matrix_cat  # [8, hidden_dim]
            emovec_mat = torch.sum(emovec_mat, 0).unsqueeze(0)  # [1, hidden_dim]
            
            # 確保 dtype 與模型一致
            if self.use_fp16:
                emovec_mat = emovec_mat.half()
            
            # 4. GPT 生成語義 tokens
            if verbose:
                debug_log(3, "   [2/4] GPT 生成中...")
            
            with torch.no_grad():
                # 使用 autocast 處理 FP16 (參考 infer_v2.py line 534)
                dtype = torch.float16 if self.use_fp16 else None
                with torch.amp.autocast(self.device.type, enabled=dtype is not None, dtype=dtype):
                    # 準備 conditioning (參考 infer_v2.py line 419-452)
                    spk_cond_emb = self.character_features['spk_cond_emb']
                    # 簡化版:使用 spk_cond_emb 作為 emo_cond_emb (完整版應從情感參考音頻提取)
                    emo_cond_emb = spk_cond_emb
                    
                    # Merge emovec (參考 infer_v2.py line 535-544)
                    emovec = self.gpt.merge_emovec(
                        spk_cond_emb,
                        emo_cond_emb,
                        torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                        torch.tensor([emo_cond_emb.shape[-1]], device=self.device),
                        alpha=1.0  # emo_alpha
                    )
                    
                    # 如果有情感向量,混合 emovec_mat
                    if emovec_mat is not None:
                        weight_sum = torch.sum(torch.tensor(normalized_emotion, device=self.device))
                        emovec = emovec_mat + (1 - weight_sum) * emovec
                    
                    # inference_speech 返回 (codes, speech_conditioning_latent)
                    codes, speech_conditioning_latent = self.gpt.inference_speech(
                        spk_cond_emb,
                        text_tokens,
                        emo_cond_emb,  # 使用 emo_cond_emb (不是 spk_cond_emb!)
                        cond_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                        emo_cond_lengths=torch.tensor([emo_cond_emb.shape[-1]], device=self.device),
                        emo_vec=emovec,  # 使用merge後的 emovec
                        num_return_sequences=1,
                        num_beams=num_beams,
                        do_sample=do_sample,
                        temperature=temperature,
                        top_p=top_p,
                        top_k=top_k,
                        length_penalty=1.0,
                        repetition_penalty=1.2,
                        max_generate_length=self.cfg.gpt.max_mel_tokens
                    )
                    
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
                        emo_vec=emovec_mat,
                        use_speed=use_speed
                    )
            
            # 找到實際的 code 長度 (參考 infer_v2.py line 583-591)
            stop_mel_token = self.cfg.gpt.stop_mel_token
            code_lens = []
            for code in codes:
                if stop_mel_token not in code:
                    code_len = len(code)
                else:
                    len_ = (code == stop_mel_token).nonzero(as_tuple=False)[0] + 1
                    code_len = len_ - 1
                code_lens.append(code_len)
            
            codes = codes[:, :code_len]  # 裁剪到實際長度
            code_lens = torch.LongTensor(code_lens).to(self.device)
            
            # 5. S2Mel 生成 Mel 頻譜
            if verbose:
                debug_log(3, "   [3/4] S2Mel 生成中...")
            
            with torch.no_grad():
                # 使用 dtype=None 的 autocast (參考 infer_v2.py line 617)
                dtype = None
                with torch.amp.autocast(self.device.type, enabled=dtype is not None, dtype=dtype):
                    latent = self.s2mel.models['gpt_layer'](latent)
                    S_infer = self.semantic_codec.quantizer.vq2emb(codes.unsqueeze(1))
                    S_infer = S_infer.transpose(1, 2)
                    
                    # 裁剪 latent 以匹配 S_infer 的長度
                    if latent.shape[1] > S_infer.shape[1]:
                        latent = latent[:, :S_infer.shape[1], :]
                    
                    S_infer = S_infer + latent
                    target_lengths = (code_lens * 1.72).long()
                
                cond = self.s2mel.models['length_regulator'](
                    S_infer,
                    ylens=target_lengths,
                    n_quantizers=3,
                    f0=None
                )[0]
                
                cat_condition = torch.cat([self.character_features['prompt_condition'], cond], dim=1)
                
                # CFM inference
                vc_target = self.s2mel.models['cfm'].inference(
                    cat_condition,
                    torch.LongTensor([cat_condition.size(1)]).to(self.device),
                    self.character_features['ref_mel'],
                    self.character_features['style'],
                    None,
                    n_timesteps=25,
                    inference_cfg_rate=0.7
                )
                
                # 移除參考音頻部分
                mel = vc_target[:, :, self.character_features['ref_mel'].size(-1):]
            
            # 6. BigVGAN 生成波形
            if verbose:
                debug_log(3, "   [4/4] BigVGAN 生成中...")
            
            with torch.no_grad():
                # BigVGAN 需要 Float32 輸入 (參考 infer_v2.py line 641)
                audio_output = self.bigvgan(mel.float()).squeeze(0).cpu()
            
            # 7. 保存音頻
            import torchaudio
            torchaudio.save(
                output_path,
                audio_output,
                sample_rate=22050,
                encoding="PCM_S",
                bits_per_sample=16
            )
            
            if verbose:
                duration = audio_output.shape[-1] / 22050
                debug_log(2, f"   ✓ 合成完成!")
                debug_log(3, f"   📁 保存至: {output_path}")
                debug_log(3, f"   ⏱️  音頻時長: {duration:.2f}秒")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 合成失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_current_character(self) -> Optional[str]:
        """獲取當前加載的角色名稱"""
        return self.current_character
    
    def is_character_loaded(self) -> bool:
        """檢查是否已加載角色"""
        return self.character_features is not None