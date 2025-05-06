# core/state_manager.py
from enum import Enum, auto

class UEPState(Enum):
    IDLE      = auto()  # 閒置
    CHAT      = auto()  # 聊天
    WORK      = auto()  # 工作（執行指令）
    MISCHIEF  = auto()  # 搗蛋（暫略）
    SLEEP     = auto()  # 睡眠（暫略）
    ERROR     = auto()  # 錯誤

class StateManager:
    """
    管理 U.E.P 五種狀態（搗蛋暫略）。
    接受事件，並在需要時切換 state。
    """

    def __init__(self):
        self._state = UEPState.IDLE

    def get_state(self) -> UEPState:
        return self._state

    def set_state(self, new_state: UEPState):
        self._state = new_state

    def on_event(self, intent: str, result: dict):
        """
        根據意圖與執行結果決定是否切換狀態。
        範例邏輯：
          - chat → 切到 CHAT
          - command 成功 → 切到 WORK
          - command 失敗 → 切到 ERROR
          - 其他 → 切回 IDLE
        """
        if intent == "chat":
            self._state = UEPState.CHAT
        elif intent == "command":
            if result.get("status") == "success":
                self._state = UEPState.WORK
            else:
                self._state = UEPState.ERROR
        else:
            self._state = UEPState.IDLE
