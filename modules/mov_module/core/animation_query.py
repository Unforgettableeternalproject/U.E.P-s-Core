"""
動畫查詢輔助器

提供從 ANI 模組查詢動畫的便利方法
"""

from typing import Optional, List
from utils.debug_helper import debug_log


class AnimationQueryHelper:
    """
    動畫查詢輔助器
    
    職責：
    1. 從 ANI 的 state_animations.yaml 查詢狀態相關動畫
    2. 根據移動模式自動選擇對應後綴的動畫 (_g/_f)
    3. 獲取動畫持續時間等元數據
    4. 提供回退機制，確保總能返回有效動畫名稱
    """
    
    def __init__(self, ani_module=None, state_animation_config: Optional[dict] = None):
        """
        初始化
        
        Args:
            ani_module: ANI 模組實例（可選）
            state_animation_config: 狀態動畫配置（從 state_animations.yaml 加載）
        """
        self.ani_module = ani_module
        self.state_animation_config = state_animation_config or {}
    
    def get_entry_animation(self) -> str:
        """
        獲取入場動畫名稱
        
        Returns:
            動畫名稱，預設為 "enter"
        """
        idle_config = self.state_animation_config.get("IDLE", {})
        return idle_config.get("transition_in", "enter")
    
    def get_exit_animation(self) -> str:
        """
        獲取出場動畫名稱
        
        Returns:
            動畫名稱，預設為 "leave"
        """
        idle_config = self.state_animation_config.get("IDLE", {})
        return idle_config.get("transition_out", "leave")
    
    def get_idle_animations(self, mode_suffix: Optional[str] = None) -> List[str]:
        """
        獲取閒置動畫列表
        
        Args:
            mode_suffix: 模式後綴（"_g" 或 "_f"），如果提供則過濾
            
        Returns:
            動畫名稱列表
        """
        idle_config = self.state_animation_config.get("IDLE", {})
        idle_anims = idle_config.get("idle_animations", [])
        
        if mode_suffix:
            # 只返回符合模式的動畫
            return [anim for anim in idle_anims if anim.endswith(mode_suffix)]
        
        return idle_anims
    
    def get_idle_animation_for_mode(self, is_ground: bool = False) -> str:
        """
        根據移動模式獲取閒置動畫
        
        Args:
            is_ground: 是否為地面模式
            
        Returns:
            動畫名稱
        """
        suffix = "_g" if is_ground else "_f"
        idle_anims = self.get_idle_animations(suffix)
        
        if idle_anims:
            return idle_anims[0]
        
        # 回退到預設
        return "stand_idle_g" if is_ground else "smile_idle_f"
    
    def get_layer_animation(self, layer: str, movement_mode: Optional[str] = None) -> Optional[str]:
        """
        獲取層級對應的動畫
        
        Args:
            layer: 層級名稱 ("input", "processing", "output")
            movement_mode: 移動模式（"GROUND" 或 "FLOAT"），用於自動添加後綴
            
        Returns:
            動畫名稱，如果未配置則返回 None
        """
        layers_config = self.state_animation_config.get("LAYERS", {})
        layer_config = layers_config.get(layer, {})
        
        if layer == "output":
            # output 層根據 mood 選擇
            # 這裡返回預設的正面情緒動畫
            anim = layer_config.get("positive_mood")
        else:
            anim = layer_config.get("default")
        
        if anim and movement_mode:
            # 自動根據模式替換後綴
            suffix = "_g" if movement_mode == "GROUND" else "_f"
            # 如果動畫名稱已經有後綴，替換它
            if anim.endswith("_g") or anim.endswith("_f"):
                anim = anim[:-2] + suffix
        
        return anim
    
    def get_transition_animation(self, from_mode: str, to_mode: str) -> Optional[str]:
        """
        獲取模式轉換動畫
        
        Args:
            from_mode: 起始模式 ("GROUND", "FLOAT", "LYING")
            to_mode: 目標模式 ("GROUND", "FLOAT", "LYING")
            
        Returns:
            動畫名稱，如果沒有對應的轉換動畫則返回 None
        """
        # 簡化模式名稱為單字母
        mode_map = {
            "GROUND": "g",
            "FLOAT": "f",
            "LYING": "l"
        }
        
        from_letter = mode_map.get(from_mode, "g")
        to_letter = mode_map.get(to_mode, "f")
        
        # 構建轉換動畫名稱
        transition_name = f"{from_letter}_to_{to_letter}"
        
        # 檢查動畫是否存在（通過 ANI 模組）
        if self.ani_module and hasattr(self.ani_module, 'config'):
            clips = self.ani_module.config.get("resources", {}).get("clips", {})
            if transition_name in clips:
                return transition_name
        
        return None
    
    def get_animation_duration(self, anim_name: str) -> float:
        """
        獲取動畫持續時間
        
        Args:
            anim_name: 動畫名稱
            
        Returns:
            持續時間（秒），如果無法獲取則返回 2.0
        """
        if not self.ani_module:
            debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): ani_module 不存在")
            return 2.0
        
        # 優先從 manager.clips 直接讀取（避免 config 未就緒）
        if hasattr(self.ani_module, 'manager') and hasattr(self.ani_module.manager, 'clips'):
            clips = self.ani_module.manager.clips
            if anim_name in clips:
                clip = clips[anim_name]
                duration = clip.total_frames * clip.frame_duration()
                debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): {clip.total_frames} 幀 × {clip.frame_duration():.4f}s = {duration:.2f}s (fps={clip.fps})")
                return duration
            else:
                debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): clip 不在 manager.clips 中（共 {len(clips)} 個）")
        else:
            debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): manager 或 clips 屬性不存在")
        
        # 回退：從 config 讀取
        if hasattr(self.ani_module, 'config'):
            ani_config = self.ani_module.config
            clips_config = ani_config.get("resources", {}).get("clips", {})
            
            if anim_name in clips_config:
                clip_info = clips_config[anim_name]
                total_frames = clip_info.get("total_frames", 0)
                frame_duration = clip_info.get("frame_duration", 0.1)
                duration = total_frames * frame_duration
                debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): 從 config 讀取 {total_frames} 幀 × {frame_duration}s = {duration:.2f}s")
                return duration
            else:
                debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): config 中找不到此動畫")
        
        debug_log(2, f"[AnimQuery] get_animation_duration({anim_name}): 使用預設值 2.0s")
        return 2.0
    
    def animation_exists(self, anim_name: str) -> bool:
        """
        檢查動畫是否存在
        
        Args:
            anim_name: 動畫名稱
            
        Returns:
            是否存在
        """
        if not self.ani_module or not hasattr(self.ani_module, 'config'):
            return False
        
        clips = self.ani_module.config.get("resources", {}).get("clips", {})
        return anim_name in clips
    
    def get_fallback_animation(self, animation_type: str) -> str:
        """
        獲取回退動畫
        
        Args:
            animation_type: 動畫類型（"idle", "thinking", "talking" 等）
            
        Returns:
            動畫名稱
        """
        fallbacks = self.state_animation_config.get("fallbacks", {})
        return fallbacks.get(animation_type, "smile_idle_f")
    
    def get_struggle_animation(self) -> str:
        """
        獲取掙扎動畫（拖曳時使用）
        
        Returns:
            動畫名稱，預設為 "struggle"
        """
        # 從 ANI config 的 aliases 查詢，如果沒有則返回預設值
        return "struggle"  # ANI config 中 struggle 是 struggle_f 的別名
    
    def get_push_animation(self, direction: str) -> str:
        """
        獲取推動動畫（根據拖曳方向）
        
        Args:
            direction: 方向 ("up", "down", "left", "right")
            
        Returns:
            動畫名稱
        """
        direction_map = {
            "up": "push_up",
            "down": "push_down",
            "left": "push_left",
            "right": "push_right"
        }
        return direction_map.get(direction.lower(), "struggle")
    
    def get_tease_animation(self, variant: int = 1) -> str:
        """
        獲取捉弄動畫（投擲後等情境）
        
        Args:
            variant: 變體編號（1 或 2）
            
        Returns:
            動畫名稱
        """
        if variant == 2:
            return "tease2_f"
        return "tease_f"
    
    def get_turn_animation(self, direction: str, is_ground: bool = True) -> Optional[str]:
        """
        獲取轉向動畫
        
        Args:
            direction: 轉向方向 ("left" 或 "right")
            is_ground: 是否為地面模式
            
        Returns:
            動畫名稱，如果沒有對應動畫則返回 None
        """
        if not is_ground:
            # 浮空模式沒有轉向動畫，直接切換面向
            return None
        
        if direction.lower() == "left":
            return "turn_left_g"
        elif direction.lower() == "right":
            return "turn_right_g"
        
        return None
    
    def get_walk_animation(self, direction: str) -> str:
        """
        獲取行走動畫
        
        Args:
            direction: 行走方向 ("left" 或 "right")
            
        Returns:
            動畫名稱
        """
        if direction.lower() == "left":
            return "walk_left_g"
        elif direction.lower() == "right":
            return "walk_right_g"
        
        return "walk_right_g"
    
    def get_turn_head_animation(self, is_ground: bool = False) -> str:
        """
        獲取轉頭動畫（滑鼠跟隨）
        
        Args:
            is_ground: 是否為地面模式
            
        Returns:
            動畫名稱
        """
        return "turn_head_g" if is_ground else "turn_head_f"
    
    def get_easter_egg_animation(self, is_ground: bool = False, status_manager=None) -> Optional[str]:
        """
        獲取彩蛋動畫（根據條件隨機選擇）
        
        Args:
            is_ground: 是否為地面模式
            status_manager: 狀態管理器（用於檢查情緒值等狀態）
            
        Returns:
            動畫名稱，如果沒有可用的彩蛋動畫則返回 None
            
        彩蛋條件：
        - yawn_g: 需要 boredom > 0.6（厭煩時打哈欠）
        - dance2_g: 需要 mood > 0.6 或 pride > 0.6（心情好或自信時跳舞）
        - dance_f: 需要 mood > 0.6 或 pride > 0.6（心情好或自信時跳舞）
        - chilling_f: 無條件（放鬆狀態）
        """
        import random
        
        available = []
        
        if is_ground:
            # 地面模式的彩蛋動畫
            # dance2_g: 需要心情好或自信
            if self.animation_exists("dance2_g"):
                if status_manager:
                    try:
                        status = status_manager.status
                        mood = status.get("mood", 0.5)
                        pride = status.get("pride", 0.5)
                        # 需要心情好或自信才跳舞
                        if mood > 0.6 or pride > 0.6:
                            available.append("dance2_g")
                    except Exception:
                        pass  # 無法獲取狀態時跳過此動畫
            
            # yawn_g: 需要 boredom > 0.6
            if self.animation_exists("yawn_g"):
                if status_manager:
                    try:
                        status = status_manager.status
                        boredom = status.get("boredom", 0.0)
                        if boredom > 0.6:
                            available.append("yawn_g")
                    except Exception:
                        pass  # 無法獲取狀態時跳過此動畫
        else:
            # 浮空模式的彩蛋動畫
            # dance_f: 需要心情好或自信
            if self.animation_exists("dance_f"):
                if status_manager:
                    try:
                        status = status_manager.status
                        mood = status.get("mood", 0.5)
                        pride = status.get("pride", 0.5)
                        # 需要心情好或自信才跳舞
                        if mood > 0.6 or pride > 0.6:
                            available.append("dance_f")
                    except Exception:
                        pass  # 無法獲取狀態時跳過此動畫
            
            # chilling_f: 需要正在播放音樂（放鬆聽歌狀態）
            if self.animation_exists("chilling_f"):
                try:
                    from modules.sys_module.actions.automation_helper import get_music_player_status
                    music_status = get_music_player_status()
                    # 只有在音樂播放中才會觸發 chilling
                    if music_status.get("is_playing", False):
                        available.append("chilling_f")
                except Exception:
                    # 無法獲取音樂狀態時跳過此動畫（不要在沒有音樂時 chill）
                    pass
        
        if available:
            return random.choice(available)
        
        return None
    
    def get_mood_based_idle_animation(self, mood: float, pride: float, is_ground: bool = False) -> Optional[str]:
        """
        根據情緒值獲取對應的閒置動畫
        
        Args:
            mood: 情緒值 (0.0 - 1.0)
            pride: 自尊值 (0.0 - 1.0)
            is_ground: 是否為地面模式
            
        Returns:
            動畫名稱，如果沒有特殊情緒動畫則返回 None（使用預設 idle）
            
        情緒動畫邏輯：
        - angry_idle_f: mood < 0.4 且 pride > 0.6 (不爽但自尊高 → 生氣)
        - awkward_f: mood > 0.6 且 pride < 0.4 (開心但自尊低 → 尷尬/不好意思)
        - curious_idle_f: mood > 0.6 且 pride > 0.6 (開心且自尊高 → 好奇/興奮)
        """
        # 目前情緒動畫只有浮空模式
        if is_ground:
            return None
        
        # 根據 mood 和 pride 選擇動畫
        if mood < 0.4 and pride > 0.6:
            # 不爽但自尊高 → 生氣
            anim = "angry_idle_f"
        elif mood > 0.6 and pride < 0.4:
            # 開心但自尊低 → 尷尬
            anim = "awkward_f"
        elif mood > 0.6 and pride > 0.6:
            # 開心且自尊高 → 好奇/興奮
            anim = "curious_idle_f"
        else:
            # 中性情緒，使用預設 idle
            return None
        
        # 檢查動畫是否存在
        if self.animation_exists(anim):
            return anim
        
        return None
