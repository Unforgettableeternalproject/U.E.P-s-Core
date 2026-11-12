# Laya ft-r2 評估報告（分支 feature/decision-laya-data-r4）

## 結論

- 三套測試集的 reply_mode 準確率都比 ft-r1 高：en_test 0.643 → 0.708、en_reference 0.649 → 0.714、en_independent 0.581 → 0.788（3 seeds 平均）。獨立英文測試集進步最大，因為弱點類別（passive、defer）在那裡佔比高。
- passive：ft-r1 全部答錯（recall 0）；ft-r2 在 en_independent recall 0.538、precision 0.857，在 84 筆中文測試集只有 recall 0.14～0.19（n=7，樣本太少，波動大）。
- defer：recall 0.458 → 0.875（en_test）、0.218 → 0.839（en_independent）；代價是 84 筆集的 precision 略降，en_independent 的 respond recall 由 0.958 降到 0.826（respond 被誤判成 defer 的次數 6 → 24）。
- 沒有改善：pass 的 recall 在 84 筆中文測試集沒有進步（0.608 → 0.588、0.549 → 0.529）；zt-027／030／076 這三題仍然錯（見各集逐題結果）。
- absent.intent 在 en_independent 略降（0.944 → 0.921，約一個標準差，主要是 task 被判成 message，這個錯誤 ft-r1 也有）；en_reference 0.964 → 0.952。
- 數字是 3 個 seed 平均，seed 變異不小（最多 ±0.04），小於 0.03 的差距不要過度解讀。

## 設定與資料來源

- 訓練：`laya-train --data out/mass/train.jsonl --eval out/mass/val.jsonl --base ~/.uep/models/laya/laya-multilingual --epochs 8 --seed {0,1,2}`，其餘超參數與 ft-r1 相同（預設值，見 ft-r1 train_report 的 laya_train 欄位）。輸出 `~/.uep/models/laya/ft-r2/e8_seed{0,1,2}`，ft-r1 未動。
- 訓練資料：final 2156 筆（train 1975／val 181；ft-r1 為 train 1399／val 138）。既有 1537 筆中 38 筆 passive 因新界線改標 pass；r4 新增 619 筆，其中 3 筆 passive 因能力依據（翻譯）改標 pass。train 的 reply_mode 分布：respond 575、pass 458、defer 187、passive 156。
- 新增 619 筆（過防洩漏與盲標後）：passive 正例 139（三條各有，含資訊成對卡 24）、pass 反例 156（稱讚／回憶／轉述／旁人陳述／自言自語／實體動作請求各 18，資訊已核對／事情已結束各 24）、群組側談祈使 59、defer 100（晚點再說／資訊不足／有風險）、ack 60、MT 雜訊 turn.reply 37、MT 雜訊 absent 68。
- val 整體準確率（laya-train 輸出，item 層級）：ft-r2 0.9687／0.9582／0.9537；ft-r1 seed0 0.9582。兩者 val 集不同，不可直接比較。
- 測試集：en_test（84 筆中文測試集經 opus-mt 英譯，testset/en_test.jsonl，不入版控，取自 synth worktree 11/12 12:35 版，zt-064 已是 pass）；en_reference（人工參考英譯，testset/en_test_reference.jsonl，同上）；en_independent（231 筆，取 feature/decision-laya-testset-v2 的 c1f6f5d，sha256 前 16 碼 ab0d1523e8d1836c；synth worktree 工作樹版本以 JSON 內容比對完全相同）。規則基準 rules.default 只能對中文原句跑，所以只有前兩套有。
- ft-r1 的數字用同一份最新標準答案（zt-064=pass）重算，與舊報告略有出入（reply_mode 0.637 → 0.643）。
- en_independent 目前沒有 meta.pending_review 的列，所以沒有「排除待核列」的另一組數字。

## 比較時要注意的混淆

- ft-r2 的訓練資料同時改了三件事：passive 收窄、新增 619 筆、ack_after_statement 的 addressed 修正（0647ee9，ft-r1 訓練時尚未套用）。沒有做單項消融，無法說各項貢獻多少。
- 新資料的標籤是「卡片＋獨立盲標員（帶上新判準）」一致才收；盲標員與卡片用同一份判準文字，對 passive 邊界的一致不等於艾斯維爾的判斷。
- 中途判準改動（passive 第 1 條擴大、能力依據）後我重訓過一次；改動前訓練的 2 個 seed 模型留在 ~/.uep/models/laya/ft-r2-pre/，不在本報告比較內。

## 逐欄比較


## en_test — 全部列（84 列：turn.reply 56、absent 28；seeds r1=3 r2=3）

| 欄位 | ft-r1 | ft-r2 | Δ(r2-r1) | rules.default |
|---|---|---|---|---|
| reply_mode acc | 0.643 ± 0.047 | 0.708 ± 0.027 | +0.065 | 0.554 |
| reply_mode macro-F1 | 0.488 ± 0.027 | 0.623 ± 0.037 | +0.134 | 0.301 |
| addressed acc | 0.768 ± 0.047 | 0.845 ± 0.010 | +0.077 | 0.786 |
| route acc | 0.738 ± 0.041 | 0.762 ± 0.010 | +0.024 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.952 ± 0.041 | 0.988 ± 0.021 | +0.036 | 0.750 |
| absent.target_location acc | 0.929 ± 0.036 | 0.940 ± 0.021 | +0.012 | 1.000 |
| absent.compound acc | 0.964 ± 0.000 | 0.976 ± 0.021 | +0.012 | n/a |

reply_mode 各類 precision／recall（mean ± std over seeds）

| 類別 | n_gold | P r1 | P r2 | R r1 | R r2 |
|---|---|---|---|---|---|
| passive | 7 | n/a | 0.722 ± 0.255 | 0.000 ± 0.000 | 0.190 ± 0.082 |
| pass | 17 | 0.694 ± 0.048 | 0.667 ± 0.067 | 0.608 ± 0.090 | 0.588 ± 0.059 |
| defer | 8 | 0.806 ± 0.173 | 0.679 ± 0.037 | 0.458 ± 0.072 | 0.875 ± 0.000 |
| respond | 24 | 0.608 ± 0.076 | 0.745 ± 0.029 | 0.917 ± 0.072 | 0.889 ± 0.024 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r1

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 66 | 0 | 3 | 3 |
| passive | 11 | 0 | 10 | 0 |
| pass | 20 | 0 | 31 | 0 |
| defer | 12 | 0 | 1 | 11 |

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 64 | 0 | 3 | 5 |
| passive | 3 | 4 | 12 | 2 |
| pass | 16 | 2 | 30 | 3 |
| defer | 3 | 0 | 0 | 21 |


### en_test：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-027 text: Could you open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, task_request
    r1 seed1: respond, True, task_request
    r1 seed2: respond, True, task_request
    r2 seed0: defer, True, task_request
    r2 seed1: defer, True, task_request
    r2 seed2: respond, True, task_request
- zt-030 text: Hey, let's go to that store next time. (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, task_request
    r1 seed1: respond, True, direct_chat
    r1 seed2: respond, True, direct_chat
    r2 seed0: respond, True, direct_chat
    r2 seed1: respond, True, direct_chat
    r2 seed2: respond, True, direct_chat
- zt-076 text: I want ramen too. You wanna come with me later? (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, ignore
    r1 seed1: respond, True, direct_chat
    r1 seed2: respond, True, direct_chat
    r2 seed0: respond, True, direct_chat
    r2 seed1: respond, True, direct_chat
    r2 seed2: respond, True, direct_chat

## en_reference — 全部列（84 列：turn.reply 56、absent 28；seeds r1=3 r2=3）

| 欄位 | ft-r1 | ft-r2 | Δ(r2-r1) | rules.default |
|---|---|---|---|---|
| reply_mode acc | 0.649 ± 0.027 | 0.714 ± 0.031 | +0.065 | 0.554 |
| reply_mode macro-F1 | 0.487 ± 0.006 | 0.611 ± 0.034 | +0.124 | 0.301 |
| addressed acc | 0.708 ± 0.027 | 0.833 ± 0.010 | +0.125 | 0.786 |
| route acc | 0.690 ± 0.037 | 0.786 ± 0.036 | +0.095 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.964 ± 0.000 | 0.952 ± 0.021 | -0.012 | 0.750 |
| absent.target_location acc | 0.952 ± 0.021 | 0.964 ± 0.000 | +0.012 | 1.000 |
| absent.compound acc | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |

reply_mode 各類 precision／recall（mean ± std over seeds）

| 類別 | n_gold | P r1 | P r2 | R r1 | R r2 |
|---|---|---|---|---|---|
| passive | 7 | 0.000 (僅 1/3 seeds 有預測) | 0.417 ± 0.144 | 0.000 ± 0.000 | 0.143 ± 0.000 |
| pass | 17 | 0.851 ± 0.136 | 0.674 ± 0.052 | 0.549 ± 0.090 | 0.529 ± 0.059 |
| defer | 8 | 0.850 ± 0.132 | 0.737 ± 0.124 | 0.417 ± 0.072 | 0.875 ± 0.000 |
| respond | 24 | 0.590 ± 0.057 | 0.759 ± 0.023 | 0.986 ± 0.024 | 0.958 ± 0.042 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r1

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 71 | 0 | 0 | 1 |
| passive | 16 | 0 | 5 | 0 |
| pass | 21 | 1 | 28 | 1 |
| defer | 13 | 0 | 1 | 10 |

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 69 | 0 | 0 | 3 |
| passive | 3 | 3 | 13 | 2 |
| pass | 16 | 5 | 27 | 3 |
| defer | 3 | 0 | 0 | 21 |


### en_reference：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-027 text: Could you go open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, task_request
    r1 seed1: respond, True, task_request
    r1 seed2: pass, True, task_request
    r2 seed0: defer, True, task_request
    r2 seed1: defer, True, task_request
    r2 seed2: respond, True, task_request
- zt-030 text: Hey, let's go to that place together next time. (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, task_request
    r1 seed1: passive, True, agent_loop
    r1 seed2: respond, True, task_request
    r2 seed0: respond, True, direct_chat
    r2 seed1: respond, True, task_request
    r2 seed2: respond, True, direct_chat
- zt-076 text: I want ramen too. Shall we go together later? (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    r1 seed0: respond, True, direct_chat
    r1 seed1: respond, True, direct_chat
    r1 seed2: respond, True, direct_chat
    r2 seed0: respond, True, direct_chat
    r2 seed1: respond, True, direct_chat
    r2 seed2: respond, True, direct_chat

## en_independent — 全部列（231 列：turn.reply 159、absent 72；seeds r1=3 r2=3）

| 欄位 | ft-r1 | ft-r2 | Δ(r2-r1) | rules.default |
|---|---|---|---|---|
| reply_mode acc | 0.581 ± 0.035 | 0.788 ± 0.028 | +0.208 | n/a |
| reply_mode macro-F1 | 0.427 ± 0.020 | 0.764 ± 0.030 | +0.337 | n/a |
| addressed acc | 0.711 ± 0.056 | 0.920 ± 0.013 | +0.210 | n/a |
| route acc | 0.721 ± 0.046 | 0.836 ± 0.029 | +0.115 | n/a |
| compound acc (turn.reply) | 0.973 ± 0.007 | 0.979 ± 0.004 | +0.006 | n/a |
| absent.intent acc | 0.944 ± 0.024 | 0.921 ± 0.035 | -0.023 | n/a |
| absent.target_location acc | 0.944 ± 0.014 | 0.977 ± 0.008 | +0.032 | n/a |
| absent.compound acc | 0.977 ± 0.008 | 0.986 ± 0.000 | +0.009 | n/a |

reply_mode 各類 precision／recall（mean ± std over seeds）

| 類別 | n_gold | P r1 | P r2 | R r1 | R r2 |
|---|---|---|---|---|---|
| passive | 26 | n/a | 0.857 ± 0.038 | 0.000 ± 0.000 | 0.538 ± 0.038 |
| pass | 56 | 0.826 ± 0.108 | 0.826 ± 0.029 | 0.714 ± 0.078 | 0.845 ± 0.027 |
| defer | 29 | 0.616 ± 0.063 | 0.652 ± 0.022 | 0.218 ± 0.020 | 0.839 ± 0.020 |
| respond | 48 | 0.467 ± 0.054 | 0.826 ± 0.035 | 0.958 ± 0.021 | 0.826 ± 0.048 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r1

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 138 | 0 | 0 | 6 |
| passive | 52 | 0 | 26 | 0 |
| pass | 42 | 0 | 120 | 6 |
| defer | 66 | 0 | 2 | 19 |

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 119 | 0 | 1 | 24 |
| passive | 6 | 42 | 28 | 2 |
| pass | 6 | 7 | 142 | 13 |
| defer | 13 | 0 | 1 | 73 |


## zt-027／030／076 的觀察

- 三題在 en_test 與 en_reference 都沒有改善：zt-030、zt-076（前文是另一位使用者問、她回答，現在第三人說話者邀同伴）六次預測（2 集 × 3 seed）全部是 respond；zt-027（對在場多人說「Could you open the window」）ft-r2 變成 defer 或 respond，仍非 pass。
- 可能原因：r4 側談卡的祈使句大多有人名稱呼或明確物件動作，沒有覆蓋「前文是另一使用者與她的對話，之後說話者對同伴說『你要不要一起去』」這類以 you 稱呼旁人、沒有人名的邀約句；zt-027 的「Could you …」對在場者說但沒有人名，與「Yuki, could you …」的字面差異只有稱呼。要改善需要補這兩種型態的資料，這次沒有做。

## 實體裝置控制樣本（依指示只列出、未刪除）

- 用動詞＋裝置詞掃描 final 2156 筆，命中 78 筆，明細在 out/device_samples.json（不入版控）。其中 respond／task_request 45、pass／ignore 20、passive 3、其餘為 defer 或 agent_loop。多數來自既有的 private_task、group_named_task、filler_then_task、ack_after_solicit_task、single_multi_object 等卡片（開關燈、鎖門、調恆溫器、關電視／喇叭），另有 r4 的 pass_physical_request 與 side 卡（那些本來就是 pass）。依新的能力範圍，前者的標籤（respond／task_request）與她做不到的結論矛盾，待艾斯維爾裁示是否改標或移除。
