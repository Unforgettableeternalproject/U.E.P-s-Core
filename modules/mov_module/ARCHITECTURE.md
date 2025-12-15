# MOV 模組架構設計

## 核心理念

MOV 模組作為 UEP 的行為協調器，負責：
1. **動作決策**：根據系統狀態、層級事件、用戶互動決定角色動作
2. **動畫驅動**：選擇並觸發適當的動畫播放
3. **互動處理**：響應拖曳、檔案投放等使用者互動
4. **行為擴展**：支援動態添加新的行為模式

## 架構層級

```
┌─────────────────────────────────────────────────┐
│          MOV Module (mov_module.py)             │
│  ┌───────────────────────────────────────────┐  │
│  │      Behavior Coordinator                 │  │
│  │  - 管理行為狀態機                          │  │
│  │  - 協調多個處理器                          │  │
│  │  - 驅動動畫選擇                            │  │
│  └───────────────────────────────────────────┘  │
│                      ▼                           │
│  ┌─────────────┬──────────────┬──────────────┐  │
│  │  Event      │  Interaction │  Animation   │  │
│  │  Handlers   │  Handlers    │  Controller  │  │
│  └─────────────┴──────────────┴──────────────┘  │
└─────────────────────────────────────────────────┘
            ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│   Behaviors  │ │ Interactions │ │   ANI Module │
│  - Idle      │ │ - Drag       │ │              │
│  - Movement  │ │ - Drop       │ │              │
│  - Transition│ │ - Throw      │ │              │
└──────────────┘ └──────────────┘ └──────────────┘
```

## 模組結構

```
mov_module/
├── mov_module.py              # 主協調器
├── config.yaml                # 配置文件
├── schemas.py                 # 數據模型
├── ARCHITECTURE.md            # 本文件
│
├── core/                      # 核心邏輯
│   ├── position.py           # 位置與速度
│   ├── physics.py            # 物理引擎
│   └── state_machine.py      # 狀態機
│
├── behaviors/                 # 行為模式
│   ├── base_behavior.py      # 行為基類
│   ├── idle_behavior.py      # 閒置行為
│   ├── movement_behavior.py  # 移動行為
│   └── transition_behavior.py # 轉場行為
│
├── handlers/                  # 事件/互動處理器 (新增)
│   ├── base_handler.py       # 處理器基類
│   ├── event_handler.py      # 系統事件處理
│   ├── layer_handler.py      # 層級事件處理
│   └── interaction_handler.py # 用戶互動處理
│
└── strategies/                # 動畫選擇策略 (新增)
    ├── base_strategy.py      # 策略基類
    ├── state_strategy.py     # 狀態驅動策略
    ├── layer_strategy.py     # 層級驅動策略
    └── interaction_strategy.py # 互動驅動策略
```

## 設計模式

### 1. 策略模式 (Strategy Pattern)
不同情境下的動畫選擇邏輯封裝為策略：
- **StateStrategy**: 根據系統狀態 (IDLE/CHAT/WORK) 選擇動畫
- **LayerStrategy**: 根據處理層級 (input/processing/output) 選擇動畫
- **InteractionStrategy**: 根據用戶互動 (drag/drop/throw) 選擇動畫

### 2. 責任鏈模式 (Chain of Responsibility)
事件處理器按順序處理事件，未處理則傳遞給下一個：
```python
Event → DragHandler → DropHandler → DefaultHandler
```

### 3. 觀察者模式 (Observer Pattern)
MOV 訂閱系統事件，收到通知後決定行為：
- 層級完成事件 → 更新動畫
- 狀態變化事件 → 切換行為模式
- UI 互動事件 → 觸發特殊動作

## 擴展性設計

### 添加新行為
1. 創建新的 `Behavior` 類繼承 `BaseBehavior`
2. 實現 `on_enter()`, `on_tick()`, `on_exit()`
3. 在 `BehaviorFactory` 註冊
4. 在狀態機中定義轉換規則

### 添加新互動
1. 創建新的 `InteractionHandler`
2. 定義互動觸發條件和響應
3. 註冊到 MOV 模組
4. 配置對應的動畫策略

### 添加新動畫觸發邏輯
1. 創建新的 `AnimationStrategy`
2. 實現 `select_animation()` 方法
3. 在配置文件中定義規則
4. 註冊到策略管理器

## 未來功能預留

### 1. 檔案拖曳 (File Drop)
```python
# handlers/file_drop_handler.py
class FileDropHandler(InteractionHandler):
    def can_handle(self, event) -> bool:
        return event.type == UIEventType.FILE_DROP
    
    def handle(self, event):
        files = event.data.get('files', [])
        # 處理檔案：顯示特殊動畫、通知後端
        self.coordinator.trigger_animation('receive_file')
        # 發送事件給後端處理
        event_bus.publish(SystemEvent.FILE_RECEIVED, {'files': files})
```

### 2. 角色投擲 (Character Throw)
```python
# handlers/throw_handler.py
class ThrowHandler(InteractionHandler):
    def on_drag_end_with_velocity(self, velocity: Velocity):
        if velocity.magnitude() > THROW_THRESHOLD:
            self.coordinator.set_mode(MovementMode.THROWN)
            self.coordinator.apply_velocity(velocity)
            self.coordinator.trigger_animation('thrown')
```

### 3. 智能動畫選擇
```python
# strategies/context_aware_strategy.py
class ContextAwareStrategy(AnimationStrategy):
    def select_animation(self, context: dict) -> str:
        # 綜合多個因素：狀態、情緒、時間、互動歷史
        mood = context.get('mood', 0)
        time_of_day = context.get('time_of_day')
        last_interaction = context.get('last_interaction')
        
        # 智能選擇最合適的動畫
        return self._calculate_best_animation(mood, time_of_day, last_interaction)
```

### 4. 行為序列編排
```python
# 支援複雜的行為序列
sequence = BehaviorSequence([
    ('look_at_file', duration=1.0),
    ('move_to_file', until=lambda: reached_target()),
    ('pick_up_file', duration=0.5),
    ('celebrate', duration=2.0)
])
coordinator.execute_sequence(sequence)
```

## 配置驅動

所有行為邏輯、動畫映射、互動規則都應該可配置：

```yaml
# config.yaml
behaviors:
  idle:
    min_duration: 3.0
    max_duration: 8.0
    animations:
      - smile_idle_f
      - curious_idle_f
  
interactions:
  drag:
    trigger_threshold: 5  # pixels
    animations:
      start: struggle
      dragging: struggle  # loop
      end_ground: land_ground
      end_float: return_float
  
  file_drop:
    trigger_zones:
      - type: center
        radius: 100
        animation: receive_file_happy
      - type: any
        animation: receive_file_normal
    
animation_strategies:
  priority:
    - interaction  # 最高優先級
    - layer        # 中優先級
    - state        # 基礎優先級
```

## 性能考慮

1. **事件過濾**：只訂閱必要的事件
2. **策略緩存**：緩存動畫選擇結果
3. **懶加載**：按需載入處理器和策略
4. **狀態壓縮**：減少不必要的狀態變化

## 測試策略

1. **單元測試**：每個 Handler 和 Strategy 獨立測試
2. **整合測試**：測試事件流和行為轉換
3. **視覺測試**：使用 animation_tester 驗證動畫效果
4. **壓力測試**：快速事件流下的穩定性
