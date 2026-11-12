# Laya ft-r3 評估報告（分支 feature/decision-laya-data-r4）

並列 ft-r1、ft-r2、ft-r3-schema（消融）、ft-r3，各 3 個 seed 平均 ± 標準差。

## 結論

- ft-r3 在 addressed、route、defer precision 上明顯進步，zt-027 三個 seed 全對；但 reply_mode 準確率沒有超過 ft-r2：en_test 0.762（r2 0.708）、en_reference 0.702（0.714）、en_independent 0.742（0.788）。
- 原因主要有兩個：(1) defer 被壓過頭——這輪補了大量資訊完整的 respond 請求並砍掉部分 defer，en_independent 的 defer recall 由 0.839 掉到 0.529（defer→respond 39 次）；(2) 其中 3 題的標準答案和新能力規則本身衝突（轉帳、付款、重設路由器標 defer，而她沒有這些能力），排除後 defer recall 0.821 → 0.577，仍然下降。passive recall 也從 0.538 掉到 0.346（passive→pass 35 次），precision 0.879。
- 消融（ft-r3-schema，ft-r2 資料＋新說明文字＋預設能力欄位）：reply_mode 與 ft-r2 相比 -0.012／-0.035／-0.031，addressed 與 route 持平，都在約一個標準差內。說明文字和能力欄位本身沒有幫助；ft-r3 比 ft-r3-schema 多出來的 addressed（+0.060／+0.042／+0.053）和 route（+0.048／+0.030／+0.031）進步來自資料。
- zt-030／076 型態有改善但不穩：zt-030、zt-076 在 en_test 各 1/3 seed 答對（ft-r2 為 0/3）；zt-027 3/3。
- 數字依 3 個 seed 平均，seed 標準差最大約 0.05，0.03 以內的差距不要過度解讀。

## 設定

- 訓練：`laya-train --data out/mass/train.r3.jsonl --eval out/mass/val.r3.jsonl --base ~/.uep/models/laya/laya-multilingual --epochs 8 --seed {0,1,2}`，輸出 `~/.uep/models/laya/ft-r3/e8_seed{0,1,2}`；ft-r3-schema 用 ft-r2 的 train／val（1975／181 筆）加新題目說明與預設能力欄位，輸出 `~/.uep/models/laya/ft-r3-schema/e8_seed{0,1,2}`。其餘超參數與 ft-r1／r2 相同。訓練一次只跑一個（中途有一段 ft-r3 與 ft-r3-schema 同時跑，依指示停掉並改為依序）。
- 訓練資料：final 2399 筆（turn.reply 1748、absent 651；train 2195／val 204）。turn.reply 的 reply_mode：respond 805、pass 577、defer 189、passive 177。
- 評估輸入：ft-r1／r2 用測試檔原樣（舊題目說明、沒有能力欄位）；ft-r3／ft-r3-schema 用 `predict --schema current`（目前的題目說明，turn.reply 的 state 補現行預設能力清單，不含 device_control）。測試集：en_test／en_reference 取自 synth worktree（zt-004 為 respond／direct_chat，zt-074 前文已改），en_independent 取 feature/decision-laya-testset-v2（80a58c2）；ft-r1／r2 在新測試檔上重跑預測，所以數字與先前報告略有不同（route 欄位尤其受 zt-004 標準答案影響）。
- 規則基準 rules.default 只有前兩套有。

## 各項裁決的實際改動

1. **能力清單進 schema**：`uep/decision/laya_schema.py` 新增 state 欄位 `available_capabilities`（9 類：query、booking、messaging、calling、files、media_control、timer_alarm、host_control、device_control），預設清單不含 device_control。類別來源：工具層 `CAPABILITY_CEILING`（memory／lore／chatroom／desktop.file_*／web／image 等標籤）歸入 query／messaging／files，其餘類別對應訓練資料出現過的能力與艾斯維爾裁決的範圍（工具層目前沒有 booking／calling／media_control／timer_alarm／host_control／device_control 的標籤，待登記後映射）。reply_mode 說明文字和 passive／pass／defer／respond 的 criteria 改寫成依清單判斷，passive 為新界線精簡版。
2. **實體裝置請求成對**：20 筆對她說的實體裝置請求，原句配 device_control 保留 respond／task_request，孿生句配預設清單改標 respond／direct_chat（群組未點名且無前文則 pass）；12 筆裝置夾帶別的動作或為 defer／agent_loop 的只補 device_control；5 筆群組未點名的實體請求配 device_control 的孿生句標 passive；其他能力類別各取少量成對（booking 8、messaging 8、files 8、media_control 8、timer_alarm 8、host_control 8、calling 4）。能力審核（下一項）另有 12 筆配 device_control 的孿生句。共 89 筆孿生句。
3. **她自己說的話**：掃描所有前文的 Assistant 發言，找出她主動提議或答應做實體操作的句子共 16 筆，不改寫，只補上 device_control 讓清單與前文一致。範例：「Shall I turn off the living room lights now?」（r4-ack-00062）、「Okay, I will switch the hallway lights off at ten each night.」（00357）、「Shall I turn off the living room lights for the night?」（00420）、「Want me to switch off the porch light before you leave for the airport?」（00801）、「Shall I set the thermostat to twenty degrees?」（r4-ack-00064）。en_independent 只列出不改：ei-083（「Do you want me to dim the lights in here?」）。
4. **能力審核**：611 筆 respond／defer 請求（task_request／agent_loop／defer）由 4 位獨立標註員帶清單重標，採用「缺能力→respond／direct_chat」的人工確認後改標 19 筆（轉帳、付款、實體裝置、家電、重設裝置等，記在 `capability_overrides.json`）；其中 12 筆缺的是實體裝置，另配 device_control 孿生句保留原標籤。
5. **38 筆 passive 改標再過濾**：旁人說的內容與她記下的不一致者維持 passive，改回 2 筆（00106、00488）；另有 1 筆（00357）依同一判準加入 pass，淨值 37 筆（`label_rule=passive_narrow`）。
6. **00107** 改 pass，已在 RULINGS 留下紀錄。
7. **與測試集重複**：既有資料 8 筆（字詞層級，含 3 筆與測試集相同的冷啟動裸附和）、正規化字元三連詞比對對測試集多抓 1 筆、訓練資料內部近似重複 10 筆（標籤相同者刪後出現的；標籤不同的 2 筆保留）；合計 19 筆（其中 r4 新增資料 5 筆，都是內部重複的裸附和）（`resolve.drop_overlaps`），新生成的 r3 資料 211 筆對測試集的正規化比對 0 命中。
8. **passive 說明文字**：已改（見 1），訓練與評估都用新文字。
9. **defer**：見下節。
10. **zt-030／076 型態補資料**：(a) 同一個人轉頭用「你」對同伴 29 筆、(b) 用「我們」26 筆、(c) 換另一個人接話 29 筆，皆有她剛回答完的前文，標 pass、addressed=False；盲標淘汰 6 筆。沒有看測試集題目仿寫，生成指示只描述句型與要求題材多樣。

## defer 過頭分析（ft-r2）

- 列出 ft-r2 把標準答案 respond 判成 defer 的題目（en_independent 3 個 seed 共 24 次；en_test 少數）。多數是「祈使句、沒有 Yuki 稱呼」的完整請求：沒有 Yuki 稱呼的請求型 respond 題被判成 defer 的比例 23%（ft-r1 7%），有 Yuki 稱呼的 0%。代名詞（it／that／this）與有無時間詞都不是主要線索（有代名詞 27%、無代名詞 12%；有時間詞 25%、無時間詞 12%，樣本少）。
- 不是「資訊其實夠、卻因 that／it／沒講時間而被判缺資訊」這個假設；較像是模型學到「無稱呼的命令句＝defer」。
- 資料面：r4 新增 619 筆裡 defer 102 筆，對應的 respond 請求型只有 26 筆（比例 4:1）；ft-r2 訓練集的請求型樣本中 defer 占 41%（215/526），ft-r1 為 29%（113/385）。
- 處置：新增 129 筆資訊完整的 respond 請求（資訊完整 46、低風險 44、立即要做 39，約一半無 Yuki 稱呼），再各砍 10 筆資訊不足與高風險 defer；請求型樣本中 defer 占比降到 31%（189/604）。
- 結果：defer 被壓過頭了。ft-r3 在 en_independent 的 defer precision 0.858（r2 0.652），recall 0.529（r2 0.839）；失去的主要是缺指涉對象的請求（「Order more of those.」「Change it to the other time.」「Reply to her and say yes.」）和不可逆操作（「Wipe my laptop's hard drive」「Uninstall everything on the work computer…」「Post my draft resignation letter to the whole company channel.」）。下一步建議：把砍掉的 20 筆補回，並為缺指涉對象與不可逆操作各補成對樣本。

## 標準答案與能力規則衝突的題目（只列出，沒有改）

en_independent 中，依現行預設清單（無 device_control、無轉帳能力）答案會改變的題目：

- 實體裝置請求，標準答案 respond／task_request，預設清單下應為 respond／direct_chat：ei-034（關燈與風扇）、ei-116（鎖門）、ei-125（關走廊燈，另對 Kofi 說話）、ei-150（調高恆溫器）。
- 標準答案 defer（高風險），但她沒有這項能力，預設清單下應為 respond／direct_chat：ei-060（還清信用卡餘額）、ei-072（重設路由器）、ei-099（轉帳）。
- 她自己說的話：ei-083（她提議調暗燈光）。
- 烤箱類（ei-111）是群組未點名，有無能力答案都是 pass。
- 這 7 題上 ft-r3 reply_mode 準確率 0.619（ft-r2 1.000），排除後 ft-r3 0.748、ft-r2 0.779。

## 消融結論

ft-r3-schema 與 ft-r2 使用相同資料，只差題目說明文字與預設能力欄位：reply_mode 準確率 0.696／0.679／0.757（ft-r2 0.708／0.714／0.788），addressed 0.839／0.839／0.895（0.845／0.833／0.920），route 0.744／0.744／0.847（0.744／0.768／0.836）。差異都在約一個標準差內，passive／defer 的 recall 略低。結論：說明文字與能力欄位本身沒有幫助；ft-r3 的進步來自資料。
注意這個消融的能力欄位全部是預設清單，標籤沒有隨清單改變，所以它只量到文字與欄位的影響，量不到「看清單判斷」的能力。要量這項能力需要測試集裡有同一句話配不同清單的成對題。

## 逐欄比較


## en_test — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r1=3, ft-r2=3, ft-r3-schema=3, ft-r3=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 | Δ(ft-r3-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.643 ± 0.047 | 0.708 ± 0.027 | 0.696 ± 0.036 | 0.762 ± 0.037 | +0.119 | 0.554 |
| reply_mode macro-F1 | 0.488 ± 0.027 | 0.623 ± 0.037 | 0.609 ± 0.055 | 0.687 ± 0.039 | +0.198 | 0.301 |
| addressed acc | 0.768 ± 0.047 | 0.845 ± 0.010 | 0.839 ± 0.018 | 0.899 ± 0.021 | +0.131 | 0.786 |
| route acc | 0.702 ± 0.041 | 0.744 ± 0.010 | 0.744 ± 0.010 | 0.792 ± 0.041 | +0.089 | 0.268 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.952 ± 0.041 | 0.988 ± 0.021 | 1.000 ± 0.000 | 0.976 ± 0.021 | +0.024 | 0.750 |
| absent.target_location acc | 0.929 ± 0.036 | 0.940 ± 0.021 | 0.917 ± 0.021 | 0.917 ± 0.021 | -0.012 | 1.000 |
| absent.compound acc | 0.964 ± 0.000 | 0.976 ± 0.021 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.036 | n/a |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 7 | n/a | 0.722 ± 0.255 | 0.533 ± 0.058 | 0.667 ± 0.000 |
| pass | 17 | 0.694 ± 0.048 | 0.667 ± 0.067 | 0.754 ± 0.069 | 0.748 ± 0.048 |
| defer | 8 | 0.806 ± 0.173 | 0.679 ± 0.037 | 0.600 ± 0.000 | 0.821 ± 0.156 |
| respond | 24 | 0.608 ± 0.076 | 0.745 ± 0.029 | 0.720 ± 0.048 | 0.772 ± 0.057 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 7 | 0.000 ± 0.000 | 0.190 ± 0.082 | 0.238 ± 0.165 | 0.286 ± 0.000 |
| pass | 17 | 0.608 ± 0.090 | 0.588 ± 0.059 | 0.588 ± 0.000 | 0.745 ± 0.068 |
| defer | 8 | 0.458 ± 0.072 | 0.875 ± 0.000 | 0.750 ± 0.000 | 0.708 ± 0.072 |
| respond | 24 | 0.917 ± 0.072 | 0.889 ± 0.024 | 0.889 ± 0.048 | 0.931 ± 0.024 |

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

ft-r3-schema

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 64 | 0 | 1 | 7 |
| passive | 4 | 5 | 9 | 3 |
| pass | 15 | 4 | 30 | 2 |
| defer | 6 | 0 | 0 | 18 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 2 | 3 |
| passive | 4 | 6 | 11 | 0 |
| pass | 9 | 3 | 38 | 1 |
| defer | 7 | 0 | 0 | 17 |


### en_test：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-027 text: Could you open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, task_request
    ft-r1 seed2: respond, True, task_request
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3-schema seed0: defer, True, task_request
    ft-r3-schema seed1: defer, True, task_request
    ft-r3-schema seed2: respond, True, task_request
    ft-r3 seed0: pass, False, ignore  OK
    ft-r3 seed1: pass, False, ignore  OK
    ft-r3 seed2: pass, False, ignore  OK
- zt-030 text: Hey, let's go to that store next time. (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3-schema seed0: respond, True, direct_chat
    ft-r3-schema seed1: respond, True, direct_chat
    ft-r3-schema seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. You wanna come with me later? (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, ignore
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3-schema seed0: respond, True, direct_chat
    ft-r3-schema seed1: respond, True, direct_chat
    ft-r3-schema seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, ignore
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK

## en_reference — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r1=3, ft-r2=3, ft-r3-schema=3, ft-r3=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 | Δ(ft-r3-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.649 ± 0.027 | 0.714 ± 0.031 | 0.679 ± 0.036 | 0.702 ± 0.045 | +0.054 | 0.554 |
| reply_mode macro-F1 | 0.487 ± 0.006 | 0.611 ± 0.034 | 0.599 ± 0.056 | 0.596 ± 0.066 | +0.109 | 0.301 |
| addressed acc | 0.708 ± 0.027 | 0.833 ± 0.010 | 0.839 ± 0.000 | 0.881 ± 0.021 | +0.173 | 0.786 |
| route acc | 0.667 ± 0.045 | 0.768 ± 0.036 | 0.744 ± 0.021 | 0.774 ± 0.027 | +0.107 | 0.268 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.964 ± 0.000 | 0.952 ± 0.021 | 0.964 ± 0.000 | 0.964 ± 0.000 | +0.000 | 0.750 |
| absent.target_location acc | 0.952 ± 0.021 | 0.964 ± 0.000 | 0.940 ± 0.041 | 0.940 ± 0.041 | -0.012 | 1.000 |
| absent.compound acc | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 7 | 0.000 (僅 1/3 seeds 有預測) | 0.417 ± 0.144 | 0.511 ± 0.154 | 0.444 ± 0.096 |
| pass | 17 | 0.851 ± 0.136 | 0.674 ± 0.052 | 0.798 ± 0.089 | 0.764 ± 0.068 |
| defer | 8 | 0.850 ± 0.132 | 0.737 ± 0.124 | 0.557 ± 0.063 | 0.778 ± 0.192 |
| respond | 24 | 0.590 ± 0.057 | 0.759 ± 0.023 | 0.708 ± 0.057 | 0.697 ± 0.066 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 7 | 0.000 ± 0.000 | 0.143 ± 0.000 | 0.333 ± 0.165 | 0.190 ± 0.082 |
| pass | 17 | 0.549 ± 0.090 | 0.529 ± 0.059 | 0.451 ± 0.068 | 0.608 ± 0.068 |
| defer | 8 | 0.417 ± 0.072 | 0.875 ± 0.000 | 0.708 ± 0.072 | 0.542 ± 0.072 |
| respond | 24 | 0.986 ± 0.024 | 0.958 ± 0.042 | 0.931 ± 0.024 | 0.972 ± 0.024 |

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

ft-r3-schema

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 0 | 5 |
| passive | 5 | 7 | 6 | 3 |
| pass | 16 | 6 | 23 | 6 |
| defer | 7 | 0 | 0 | 17 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 70 | 0 | 0 | 2 |
| passive | 7 | 4 | 10 | 0 |
| pass | 13 | 5 | 31 | 2 |
| defer | 11 | 0 | 0 | 13 |


### en_reference：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-027 text: Could you go open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, task_request
    ft-r1 seed2: pass, True, task_request
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3-schema seed0: defer, True, task_request
    ft-r3-schema seed1: defer, True, task_request
    ft-r3-schema seed2: defer, True, task_request
    ft-r3 seed0: pass, True, task_request
    ft-r3 seed1: respond, False, ignore
    ft-r3 seed2: pass, True, ignore
- zt-030 text: Hey, let's go to that place together next time. (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: passive, True, agent_loop
    ft-r1 seed2: respond, True, task_request
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, task_request
    ft-r2 seed2: respond, True, direct_chat
    ft-r3-schema seed0: respond, True, direct_chat
    ft-r3-schema seed1: defer, True, direct_chat
    ft-r3-schema seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. Shall we go together later? (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, direct_chat
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3-schema seed0: respond, True, direct_chat
    ft-r3-schema seed1: respond, True, direct_chat
    ft-r3-schema seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat

## en_independent — 全部列（231 列：turn.reply 159、absent 72；seeds ft-r1=3, ft-r2=3, ft-r3-schema=3, ft-r3=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 | Δ(ft-r3-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.581 ± 0.035 | 0.788 ± 0.028 | 0.757 ± 0.024 | 0.742 ± 0.049 | +0.161 | n/a |
| reply_mode macro-F1 | 0.427 ± 0.020 | 0.764 ± 0.030 | 0.728 ± 0.030 | 0.686 ± 0.054 | +0.259 | n/a |
| addressed acc | 0.711 ± 0.056 | 0.920 ± 0.013 | 0.895 ± 0.022 | 0.948 ± 0.016 | +0.237 | n/a |
| route acc | 0.721 ± 0.046 | 0.836 ± 0.029 | 0.847 ± 0.015 | 0.878 ± 0.031 | +0.157 | n/a |
| compound acc (turn.reply) | 0.973 ± 0.007 | 0.979 ± 0.004 | 0.979 ± 0.004 | 0.964 ± 0.013 | -0.008 | n/a |
| absent.intent acc | 0.944 ± 0.024 | 0.921 ± 0.035 | 0.903 ± 0.028 | 0.903 ± 0.014 | -0.042 | n/a |
| absent.target_location acc | 0.944 ± 0.014 | 0.977 ± 0.008 | 0.954 ± 0.008 | 0.977 ± 0.021 | +0.032 | n/a |
| absent.compound acc | 0.977 ± 0.008 | 0.986 ± 0.000 | 0.981 ± 0.008 | 0.986 ± 0.014 | +0.009 | n/a |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 26 | n/a | 0.857 ± 0.038 | 0.774 ± 0.074 | 0.879 ± 0.210 |
| pass | 56 | 0.826 ± 0.108 | 0.826 ± 0.029 | 0.831 ± 0.020 | 0.792 ± 0.066 |
| defer | 29 | 0.616 ± 0.063 | 0.652 ± 0.022 | 0.708 ± 0.045 | 0.858 ± 0.072 |
| respond | 48 | 0.467 ± 0.054 | 0.826 ± 0.035 | 0.717 ± 0.044 | 0.654 ± 0.029 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3-schema | ft-r3 |
|---|---|---|---|---|---|
| passive | 26 | 0.000 ± 0.000 | 0.538 ± 0.038 | 0.474 ± 0.097 | 0.346 ± 0.077 |
| pass | 56 | 0.714 ± 0.078 | 0.845 ± 0.027 | 0.768 ± 0.064 | 0.845 ± 0.098 |
| defer | 29 | 0.218 ± 0.020 | 0.839 ± 0.020 | 0.770 ± 0.020 | 0.529 ± 0.053 |
| respond | 48 | 0.958 ± 0.021 | 0.826 ± 0.048 | 0.889 ± 0.012 | 0.965 ± 0.012 |

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

ft-r3-schema

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 128 | 0 | 2 | 14 |
| passive | 15 | 37 | 23 | 3 |
| pass | 19 | 9 | 129 | 11 |
| defer | 17 | 2 | 1 | 67 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 139 | 0 | 0 | 5 |
| passive | 16 | 27 | 35 | 0 |
| pass | 19 | 4 | 142 | 3 |
| defer | 39 | 0 | 2 | 46 |

