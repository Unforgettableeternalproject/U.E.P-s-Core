# Laya ft-r4 評估報告（分支 feature/decision-laya-data-r4）

第四輪分三個子輪，一次只改一件事：r4a（能力標註改為 route 照意圖分）→ r4b（只修 defer）→ r4c（只修 passive）。每節並列 ft-r1、ft-r2、ft-r3 與該子輪（各 3 個 seed，mean ± std），並附與前一子輪逐 seed 配對差。評估一律用目前的題目說明與預設能力欄位（`predict --schema current`）；ft-r3 及更早的模型用它們訓練時的文字評估（ft-r1／r2 用舊文字、不帶能力欄位）。測試集：en_test、en_reference（zt-004 已改回 task_request）、en_independent（231 題，標準答案沒改）、capability_pairs（21 組 42 題）。

# r4a：route 照意圖分

## 結論

- 與 ft-r3 相比，reply_mode 準確率在 en_test 持平（0.762）、en_reference +0.018、en_independent +0.044（0.742 → 0.786，逐 seed 配對差 +0.044 ± 0.054，約一個標準差）。後兩者的差異都在 seed 變異內，不要過度解讀。
- 能力成對題 A 類（私聊、換清單）：換清單後 reply_mode＋route 不變的比例 ft-r3 0.606 → ft-r4a 1.000，這是這輪最清楚的收穫——清單不再影響直接請求的 route。
- 能力成對題 B 類（群組未點名、有能力 passive／沒能力 pass）：切換比例只有 0.067（ft-r3 0.033），有能力版 passive 的準確率 0.400。模型幾乎不會因為清單在群組未點名請求上改答案；這是 r4c 要處理的部分，r4a 沒有改善。
- zt-004（關燈，task_request）三個 seed 全對；zt-027 2/3、zt-030 2/3、zt-076 3/3（ft-r3 為 3/3、1/3、1/3）。
- 排除標準答案與能力規則衝突的 7 題後（照 r4a 的原則這 7 題其實不再衝突，此欄僅供對照）：reply_mode 0.776 ± 0.007 vs ft-r3 0.748 ± 0.049，配對差 +0.029 ± 0.055。
- defer recall（en_independent）0.529 → 0.701，passive recall 0.346 → 0.423，respond precision 0.654 → 0.711；仍低於 ft-r2 的 defer recall 0.839、passive recall 0.538。

## 資料改動（相對 r3）

- 還原 19 筆能力審核改標（轉帳、付款、重設裝置、關電視、洗衣機等恢復 defer／task_request 或 respond／task_request）；`capability_overrides.json` 只留 drop。
- 孿生句改成「只差清單、標籤相同」：實體裝置請求 20 筆、實體裝置 defer／task_request 12 筆、其他能力類別各 4 筆（calling、booking、messaging、files、media_control、timer_alarm、host_control）。直接請求的同標籤成對樣本共 60 筆；r3 的 48 筆孿生句因此改標，24 筆舊的類別孿生句不再取用。
- 群組未點名的 passive／pass 孿生句維持：實體裝置 5 筆，另新增 9 筆非實體能力（messaging 3、timer_alarm 4、calling 1、booking 1；有該能力 passive，沒有 pass）。
- `laya_schema.py`：reply_mode 說明改為「清單只用於沒點名的請求；直接請求照意圖處理」；respond 改為「即使發現做不到仍要回應」；route 的 direct_chat 去掉「告訴使用者做不到」，task_request 說明改為「照要求的內容分，不論做不做得到」。
- final 2384 筆（turn.reply 1733、absent 651），train 2180／val 204。turn.reply：respond 780、pass 572、defer 204、passive 177。請求型樣本中 defer 占比 30.4%（204/671）。

## 逐欄比較


## en_test — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r1=3, ft-r2=3, ft-r3=3, ft-r4a=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3 | ft-r4a | Δ(ft-r4a-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.643 ± 0.047 | 0.708 ± 0.027 | 0.762 ± 0.037 | 0.762 ± 0.027 | +0.119 | 0.554 |
| reply_mode macro-F1 | 0.488 ± 0.027 | 0.623 ± 0.037 | 0.687 ± 0.039 | 0.679 ± 0.028 | +0.191 | 0.301 |
| addressed acc | 0.768 ± 0.047 | 0.845 ± 0.010 | 0.899 ± 0.021 | 0.899 ± 0.021 | +0.131 | 0.786 |
| route acc | 0.720 ± 0.041 | 0.762 ± 0.010 | 0.786 ± 0.036 | 0.786 ± 0.018 | +0.065 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.952 ± 0.041 | 0.988 ± 0.021 | 0.976 ± 0.021 | 0.976 ± 0.021 | +0.024 | 0.750 |
| absent.target_location acc | 0.929 ± 0.036 | 0.940 ± 0.021 | 0.917 ± 0.021 | 0.929 ± 0.000 | +0.000 | 1.000 |
| absent.compound acc | 0.964 ± 0.000 | 0.976 ± 0.021 | 1.000 ± 0.000 | 0.988 ± 0.021 | +0.024 | n/a |

逐 seed 配對差（ft-r4a − ft-r3，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.000 ± 0.031 |
| reply_mode macro-F1 | -0.007 ± 0.038 |
| addressed acc | +0.000 ± 0.036 |
| route acc | +0.000 ± 0.031 |
| compound acc (turn.reply) | +0.000 ± 0.000 |
| absent.intent acc | +0.000 ± 0.036 |
| absent.target_location acc | +0.012 ± 0.021 |
| absent.compound acc | -0.012 ± 0.021 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 7 | n/a | 0.722 ± 0.255 | 0.667 ± 0.000 | 0.556 ± 0.096 |
| pass | 17 | 0.694 ± 0.048 | 0.667 ± 0.067 | 0.748 ± 0.048 | 0.746 ± 0.088 |
| defer | 8 | 0.806 ± 0.173 | 0.679 ± 0.037 | 0.821 ± 0.156 | 0.738 ± 0.021 |
| respond | 24 | 0.608 ± 0.076 | 0.745 ± 0.029 | 0.772 ± 0.057 | 0.814 ± 0.027 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 7 | 0.000 ± 0.000 | 0.190 ± 0.082 | 0.286 ± 0.000 | 0.286 ± 0.000 |
| pass | 17 | 0.608 ± 0.090 | 0.588 ± 0.059 | 0.745 ± 0.068 | 0.784 ± 0.034 |
| defer | 8 | 0.458 ± 0.072 | 0.875 ± 0.000 | 0.708 ± 0.072 | 0.708 ± 0.072 |
| respond | 24 | 0.917 ± 0.072 | 0.889 ± 0.024 | 0.931 ± 0.024 | 0.903 ± 0.024 |

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

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 2 | 3 |
| passive | 4 | 6 | 11 | 0 |
| pass | 9 | 3 | 38 | 1 |
| defer | 7 | 0 | 0 | 17 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 65 | 0 | 1 | 6 |
| passive | 4 | 6 | 11 | 0 |
| pass | 7 | 4 | 40 | 0 |
| defer | 4 | 1 | 2 | 17 |


### en_test：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-004 text: Turn off the lights in the living room. (context: (none)) [others=none]
    gold: respond, True, task_request
    ft-r1 seed0: respond, True, task_request  OK
    ft-r1 seed1: respond, True, task_request  OK
    ft-r1 seed2: respond, True, task_request  OK
    ft-r2 seed0: respond, True, task_request  OK
    ft-r2 seed1: respond, True, task_request  OK
    ft-r2 seed2: respond, True, task_request  OK
    ft-r3 seed0: respond, True, task_request  OK
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: respond, True, task_request  OK
    ft-r4a seed1: respond, True, task_request  OK
    ft-r4a seed2: respond, True, task_request  OK
- zt-027 text: Could you open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, task_request
    ft-r1 seed2: respond, True, task_request
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3 seed0: pass, False, ignore  OK
    ft-r3 seed1: pass, False, ignore  OK
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, True, task_request
    ft-r4a seed2: pass, False, ignore  OK
- zt-030 text: Hey, let's go to that store next time. (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, True, ignore
    ft-r4a seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. You wanna come with me later? (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, ignore
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, ignore
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, False, ignore  OK
    ft-r4a seed2: pass, False, ignore  OK

## en_reference — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r1=3, ft-r2=3, ft-r3=3, ft-r4a=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3 | ft-r4a | Δ(ft-r4a-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.649 ± 0.027 | 0.714 ± 0.031 | 0.702 ± 0.045 | 0.720 ± 0.021 | +0.071 | 0.554 |
| reply_mode macro-F1 | 0.487 ± 0.006 | 0.611 ± 0.034 | 0.596 ± 0.066 | 0.632 ± 0.038 | +0.145 | 0.301 |
| addressed acc | 0.708 ± 0.027 | 0.833 ± 0.010 | 0.881 ± 0.021 | 0.893 ± 0.018 | +0.185 | 0.786 |
| route acc | 0.685 ± 0.045 | 0.786 ± 0.036 | 0.768 ± 0.031 | 0.839 ± 0.000 | +0.155 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.994 ± 0.010 | -0.006 | n/a |
| absent.intent acc | 0.964 ± 0.000 | 0.952 ± 0.021 | 0.964 ± 0.000 | 0.952 ± 0.021 | -0.012 | 0.750 |
| absent.target_location acc | 0.952 ± 0.021 | 0.964 ± 0.000 | 0.940 ± 0.041 | 0.964 ± 0.000 | +0.012 | 1.000 |
| absent.compound acc | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |

逐 seed 配對差（ft-r4a − ft-r3，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.018 ± 0.047 |
| reply_mode macro-F1 | +0.036 ± 0.066 |
| addressed acc | +0.012 ± 0.037 |
| route acc | +0.071 ± 0.031 |
| compound acc (turn.reply) | -0.006 ± 0.010 |
| absent.intent acc | -0.012 ± 0.021 |
| absent.target_location acc | +0.024 ± 0.041 |
| absent.compound acc | +0.000 ± 0.000 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 7 | 0.000 (僅 1/3 seeds 有預測) | 0.417 ± 0.144 | 0.444 ± 0.096 | 0.439 ± 0.211 |
| pass | 17 | 0.851 ± 0.136 | 0.674 ± 0.052 | 0.764 ± 0.068 | 0.712 ± 0.147 |
| defer | 8 | 0.850 ± 0.132 | 0.737 ± 0.124 | 0.778 ± 0.192 | 0.722 ± 0.096 |
| respond | 24 | 0.590 ± 0.057 | 0.759 ± 0.023 | 0.697 ± 0.066 | 0.780 ± 0.036 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 7 | 0.000 ± 0.000 | 0.143 ± 0.000 | 0.190 ± 0.082 | 0.238 ± 0.082 |
| pass | 17 | 0.549 ± 0.090 | 0.529 ± 0.059 | 0.608 ± 0.068 | 0.627 ± 0.034 |
| defer | 8 | 0.417 ± 0.072 | 0.875 ± 0.000 | 0.542 ± 0.072 | 0.708 ± 0.072 |
| respond | 24 | 0.986 ± 0.024 | 0.958 ± 0.042 | 0.972 ± 0.024 | 0.931 ± 0.024 |

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

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 70 | 0 | 0 | 2 |
| passive | 7 | 4 | 10 | 0 |
| pass | 13 | 5 | 31 | 2 |
| defer | 11 | 0 | 0 | 13 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 1 | 4 |
| passive | 5 | 5 | 11 | 0 |
| pass | 9 | 7 | 32 | 3 |
| defer | 5 | 0 | 2 | 17 |


### en_reference：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-004 text: Turn off the lights in the living room. (context: (none)) [others=none]
    gold: respond, True, task_request
    ft-r1 seed0: respond, True, task_request  OK
    ft-r1 seed1: respond, True, task_request  OK
    ft-r1 seed2: respond, True, task_request  OK
    ft-r2 seed0: respond, True, task_request  OK
    ft-r2 seed1: respond, True, task_request  OK
    ft-r2 seed2: respond, True, task_request  OK
    ft-r3 seed0: respond, True, task_request  OK
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: respond, True, task_request  OK
    ft-r4a seed1: respond, True, task_request  OK
    ft-r4a seed2: respond, True, task_request  OK
- zt-027 text: Could you go open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: respond, True, task_request
    ft-r1 seed2: pass, True, task_request
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3 seed0: pass, True, task_request
    ft-r3 seed1: respond, False, ignore
    ft-r3 seed2: pass, True, ignore
    ft-r4a seed0: pass, False, task_request
    ft-r4a seed1: pass, True, task_request
    ft-r4a seed2: pass, False, ignore  OK
- zt-030 text: Hey, let's go to that place together next time. (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, task_request
    ft-r1 seed1: passive, True, agent_loop
    ft-r1 seed2: respond, True, task_request
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, task_request
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, False, ignore  OK
    ft-r4a seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. Shall we go together later? (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r1 seed0: respond, True, direct_chat
    ft-r1 seed1: respond, True, direct_chat
    ft-r1 seed2: respond, True, direct_chat
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: respond, True, ignore
    ft-r4a seed2: pass, True, ignore

## en_independent — 全部列（231 列：turn.reply 159、absent 72；seeds ft-r1=3, ft-r2=3, ft-r3=3, ft-r4a=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3 | ft-r4a | Δ(ft-r4a-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.581 ± 0.035 | 0.788 ± 0.028 | 0.742 ± 0.049 | 0.786 ± 0.006 | +0.205 | n/a |
| reply_mode macro-F1 | 0.427 ± 0.020 | 0.764 ± 0.030 | 0.686 ± 0.054 | 0.747 ± 0.016 | +0.320 | n/a |
| addressed acc | 0.711 ± 0.056 | 0.920 ± 0.013 | 0.948 ± 0.016 | 0.935 ± 0.038 | +0.224 | n/a |
| route acc | 0.721 ± 0.046 | 0.836 ± 0.029 | 0.878 ± 0.031 | 0.870 ± 0.007 | +0.149 | n/a |
| compound acc (turn.reply) | 0.973 ± 0.007 | 0.979 ± 0.004 | 0.964 ± 0.013 | 0.971 ± 0.010 | -0.002 | n/a |
| absent.intent acc | 0.944 ± 0.024 | 0.921 ± 0.035 | 0.903 ± 0.014 | 0.907 ± 0.021 | -0.037 | n/a |
| absent.target_location acc | 0.944 ± 0.014 | 0.977 ± 0.008 | 0.977 ± 0.021 | 0.977 ± 0.021 | +0.032 | n/a |
| absent.compound acc | 0.977 ± 0.008 | 0.986 ± 0.000 | 0.986 ± 0.014 | 0.986 ± 0.014 | +0.009 | n/a |

逐 seed 配對差（ft-r4a − ft-r3，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.044 ± 0.054 |
| reply_mode macro-F1 | +0.061 ± 0.068 |
| addressed acc | -0.013 ± 0.023 |
| route acc | -0.008 ± 0.035 |
| compound acc (turn.reply) | +0.006 ± 0.022 |
| absent.intent acc | +0.005 ± 0.021 |
| absent.target_location acc | +0.000 ± 0.042 |
| absent.compound acc | +0.000 ± 0.024 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 26 | n/a | 0.857 ± 0.038 | 0.879 ± 0.210 | 0.805 ± 0.115 |
| pass | 56 | 0.826 ± 0.108 | 0.826 ± 0.029 | 0.792 ± 0.066 | 0.836 ± 0.046 |
| defer | 29 | 0.616 ± 0.063 | 0.652 ± 0.022 | 0.858 ± 0.072 | 0.873 ± 0.032 |
| respond | 48 | 0.467 ± 0.054 | 0.826 ± 0.035 | 0.654 ± 0.029 | 0.711 ± 0.043 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 26 | 0.000 ± 0.000 | 0.538 ± 0.038 | 0.346 ± 0.077 | 0.423 ± 0.067 |
| pass | 56 | 0.714 ± 0.078 | 0.845 ± 0.027 | 0.845 ± 0.098 | 0.863 ± 0.041 |
| defer | 29 | 0.218 ± 0.020 | 0.839 ± 0.020 | 0.529 ± 0.053 | 0.701 ± 0.053 |
| respond | 48 | 0.958 ± 0.021 | 0.826 ± 0.048 | 0.965 ± 0.012 | 0.944 ± 0.043 |

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

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 139 | 0 | 0 | 5 |
| passive | 16 | 27 | 35 | 0 |
| pass | 19 | 4 | 142 | 3 |
| defer | 39 | 0 | 2 | 46 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 136 | 0 | 2 | 6 |
| passive | 20 | 33 | 25 | 0 |
| pass | 12 | 8 | 145 | 3 |
| defer | 24 | 0 | 2 | 61 |


## en_independent — 排除標準答案與能力規則衝突的題目（7 題：ei-034, ei-060, ei-072, ei-099, ei-116, ei-125, ei-150）（224 列：turn.reply 152、absent 72；seeds ft-r1=3, ft-r2=3, ft-r3=3, ft-r4a=3）

| 欄位 | ft-r1 | ft-r2 | ft-r3 | ft-r4a | Δ(ft-r4a-ft-r1) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.579 ± 0.034 | 0.779 ± 0.030 | 0.748 ± 0.049 | 0.776 ± 0.007 | +0.197 | n/a |
| reply_mode macro-F1 | 0.428 ± 0.017 | 0.754 ± 0.031 | 0.696 ± 0.052 | 0.736 ± 0.015 | +0.309 | n/a |
| addressed acc | 0.697 ± 0.058 | 0.917 ± 0.014 | 0.945 ± 0.017 | 0.932 ± 0.040 | +0.235 | n/a |
| route acc | 0.708 ± 0.048 | 0.829 ± 0.030 | 0.884 ± 0.030 | 0.864 ± 0.008 | +0.156 | n/a |
| compound acc (turn.reply) | 0.971 ± 0.008 | 0.978 ± 0.004 | 0.965 ± 0.010 | 0.969 ± 0.010 | -0.002 | n/a |
| absent.intent acc | 0.944 ± 0.024 | 0.921 ± 0.035 | 0.903 ± 0.014 | 0.907 ± 0.021 | -0.037 | n/a |
| absent.target_location acc | 0.944 ± 0.014 | 0.977 ± 0.008 | 0.977 ± 0.021 | 0.977 ± 0.021 | +0.032 | n/a |
| absent.compound acc | 0.977 ± 0.008 | 0.986 ± 0.000 | 0.986 ± 0.014 | 0.986 ± 0.014 | +0.009 | n/a |

逐 seed 配對差（ft-r4a − ft-r3，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.029 ± 0.055 |
| reply_mode macro-F1 | +0.041 ± 0.067 |
| addressed acc | -0.013 ± 0.024 |
| route acc | -0.020 ± 0.035 |
| compound acc (turn.reply) | +0.004 ± 0.019 |
| absent.intent acc | +0.005 ± 0.021 |
| absent.target_location acc | +0.000 ± 0.042 |
| absent.compound acc | +0.000 ± 0.024 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 26 | n/a | 0.857 ± 0.038 | 0.879 ± 0.210 | 0.805 ± 0.115 |
| pass | 56 | 0.826 ± 0.108 | 0.826 ± 0.029 | 0.797 ± 0.072 | 0.836 ± 0.046 |
| defer | 26 | 0.604 ± 0.061 | 0.622 ± 0.024 | 0.855 ± 0.073 | 0.854 ± 0.036 |
| respond | 44 | 0.457 ± 0.056 | 0.810 ± 0.038 | 0.656 ± 0.029 | 0.692 ± 0.044 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|---|
| passive | 26 | 0.000 ± 0.000 | 0.538 ± 0.038 | 0.346 ± 0.077 | 0.423 ± 0.067 |
| pass | 56 | 0.714 ± 0.078 | 0.845 ± 0.027 | 0.845 ± 0.098 | 0.863 ± 0.041 |
| defer | 26 | 0.231 ± 0.000 | 0.821 ± 0.022 | 0.577 ± 0.038 | 0.667 ± 0.059 |
| respond | 44 | 0.955 ± 0.023 | 0.811 ± 0.052 | 0.962 ± 0.013 | 0.939 ± 0.047 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r1

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 126 | 0 | 0 | 6 |
| passive | 52 | 0 | 26 | 0 |
| pass | 42 | 0 | 120 | 6 |
| defer | 58 | 0 | 2 | 18 |

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 107 | 0 | 1 | 24 |
| passive | 6 | 42 | 28 | 2 |
| pass | 6 | 7 | 142 | 13 |
| defer | 13 | 0 | 1 | 64 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 127 | 0 | 0 | 5 |
| passive | 16 | 27 | 35 | 0 |
| pass | 19 | 4 | 142 | 3 |
| defer | 32 | 0 | 1 | 45 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 124 | 0 | 2 | 6 |
| passive | 20 | 33 | 25 | 0 |
| pass | 12 | 8 | 145 | 3 |
| defer | 24 | 0 | 2 | 52 |


## capability_pairs — 能力成對題（42 列：turn.reply 42、absent 0；seeds ft-r1=3, ft-r2=3, ft-r3=3, ft-r4a=3）

A 類 11 組、B 類 10 組（mean ± std over seeds）

| 指標 | ft-r1 | ft-r2 | ft-r3 | ft-r4a |
|---|---|---|---|---|
| A 類：換清單後 reply_mode＋route 不變的比例 | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.606 ± 0.052 | 1.000 ± 0.000 |
| A 類：reply_mode＋route 都對 | 0.727 ± 0.000 | 0.636 ± 0.000 | 0.636 ± 0.000 | 0.818 ± 0.000 |
| B 類：有能力 passive 且沒能力 pass 的比例 | 0.000 ± 0.000 | 0.000 ± 0.000 | 0.033 ± 0.058 | 0.067 ± 0.058 |
| B 類：有能力版 reply_mode 準確率 | 0.000 ± 0.000 | 0.600 ± 0.100 | 0.400 ± 0.100 | 0.400 ± 0.100 |
| B 類：沒能力版 reply_mode 準確率 | 0.100 ± 0.100 | 0.333 ± 0.153 | 0.500 ± 0.100 | 0.467 ± 0.058 |

# r4b：只修 defer（以 r4a 為基礎）

## 結論

- defer recall 回升，reply_mode 與 route 沒有顯著退步：
  - en_independent defer recall 0.701 → 0.793（逐 seed 配對方向一致），en_test 0.708 → 0.875，en_reference 0.708 → 0.792；
  - defer precision 隨之下降：en_independent 0.873 → 0.796，en_test 0.738 → 0.666，en_reference 0.722 → 0.594。補的是 recall，代價是多判 defer。
- reply_mode 準確率：en_test 0.762 → 0.762（配對差 +0.000 ± 0.062）、en_reference 0.720 → 0.732（+0.012 ± 0.045）、en_independent 0.786 → 0.786（+0.000 ± 0.022）；排除 7 題後 0.776 → 0.776（+0.000 ± 0.023）。沒有變化。
- route 準確率三套都略降，但在誤差內：-0.024 ± 0.010（en_test）、-0.030 ± 0.041（en_reference）、-0.023 ± 0.037（en_independent）。addressed 同樣小幅下降（-0.012～-0.018）。
- 沒改善或變差的地方：
  - en_independent passive recall 0.423 → 0.333（passive→respond 20 → 25 次），passive 是 r4c 的主題；
  - zt-027 由 2/3 退到 1/3、zt-030 由 2/3 退到 1/3、zt-076 由 3/3 退到 1/3（zt-004 仍 3/3）。這三題在 r4a 才剛修好，r4b 只動了 defer 資料，這個回退可能是 seed 變異，也可能是 defer 偏多把「對旁人說的請求」拉向 defer／respond；單看 3 個 seed 分不出來；
  - capability_pairs A 類「換清單後不變」的比例 1.000 → 0.939 ± 0.052，B 類切換比例 0.100 ± 0.100（r4a 0.067）仍然接近 0。
- en_independent 混淆（3 seed 合計）：respond→defer 6 次（r4a 也是 6），defer→respond 24 → 17 次。

## 資料改動（相對 r4a）

- 補回 r3 砍掉的 20 筆 defer（`DEFER_TRIM` 清空）。
- 最小差異成對樣本 134 組（268 列）：
  - 指涉對象 42 組：「Send the report to Lena」respond ↔「Send it to her」（無前文）defer；
  - 可逆性與影響範圍 49 組：「Archive the old backups」respond ↔「Delete the old backups」defer，另含低風險 delete（單一草稿、移到垃圾桶）respond ↔ 大範圍或永久刪除 defer，要學的是影響範圍和能不能復原，不是 delete 這個字；
  - 稱呼 43 組（只放私聊）：同一句加不加「Yuki,」答案相同，完整請求 38 組都 respond、資訊不足 5 組都 defer。
  - 生成 153 組，盲標與重複過濾後剩 134 組；一邊被淘汰則整對拿掉。
- 在 r4a 資料上重算後，請求型樣本 defer 占比 33.9%（325/959；r4a 30.4%）。生成後曾高於 35%，依 sha1 順序拿掉稱呼成對的資訊不足組 5 組達標。
- final 2672 筆，train 2423／val 249。reply_mode：respond 947、pass 572、defer 325、passive 177。

## 逐欄比較（ft-r2、ft-r3、ft-r4a、ft-r4b）


## en_test — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r2=3, ft-r3=3, ft-r4a=3, ft-r4b=3）

| 欄位 | ft-r2 | ft-r3 | ft-r4a | ft-r4b | Δ(ft-r4b-ft-r2) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.708 ± 0.027 | 0.762 ± 0.037 | 0.762 ± 0.027 | 0.762 ± 0.063 | +0.054 | 0.554 |
| reply_mode macro-F1 | 0.623 ± 0.037 | 0.687 ± 0.039 | 0.679 ± 0.028 | 0.706 ± 0.099 | +0.083 | 0.301 |
| addressed acc | 0.845 ± 0.010 | 0.899 ± 0.021 | 0.899 ± 0.021 | 0.887 ± 0.010 | +0.042 | 0.786 |
| route acc | 0.762 ± 0.010 | 0.786 ± 0.036 | 0.786 ± 0.018 | 0.762 ± 0.010 | +0.000 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.988 ± 0.021 | 0.976 ± 0.021 | 0.976 ± 0.021 | 1.000 ± 0.000 | +0.012 | 0.750 |
| absent.target_location acc | 0.940 ± 0.021 | 0.917 ± 0.021 | 0.929 ± 0.000 | 0.917 ± 0.021 | -0.024 | 1.000 |
| absent.compound acc | 0.976 ± 0.021 | 1.000 ± 0.000 | 0.988 ± 0.021 | 1.000 ± 0.000 | +0.024 | n/a |

逐 seed 配對差（ft-r4b − ft-r4a，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.000 ± 0.062 |
| reply_mode macro-F1 | +0.027 ± 0.104 |
| addressed acc | -0.012 ± 0.010 |
| route acc | -0.024 ± 0.010 |
| compound acc (turn.reply) | +0.000 ± 0.000 |
| absent.intent acc | +0.024 ± 0.021 |
| absent.target_location acc | -0.012 ± 0.021 |
| absent.compound acc | +0.012 ± 0.021 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 7 | 0.722 ± 0.255 | 0.667 ± 0.000 | 0.556 ± 0.096 | 0.600 ± 0.240 |
| pass | 17 | 0.667 ± 0.067 | 0.748 ± 0.048 | 0.746 ± 0.088 | 0.797 ± 0.058 |
| defer | 8 | 0.679 ± 0.037 | 0.821 ± 0.156 | 0.738 ± 0.021 | 0.666 ± 0.101 |
| respond | 24 | 0.745 ± 0.029 | 0.772 ± 0.057 | 0.814 ± 0.027 | 0.808 ± 0.038 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 7 | 0.190 ± 0.082 | 0.286 ± 0.000 | 0.286 ± 0.000 | 0.429 ± 0.247 |
| pass | 17 | 0.588 ± 0.059 | 0.745 ± 0.068 | 0.784 ± 0.034 | 0.686 ± 0.068 |
| defer | 8 | 0.875 ± 0.000 | 0.708 ± 0.072 | 0.708 ± 0.072 | 0.875 ± 0.000 |
| respond | 24 | 0.889 ± 0.024 | 0.931 ± 0.024 | 0.903 ± 0.024 | 0.875 ± 0.042 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 64 | 0 | 3 | 5 |
| passive | 3 | 4 | 12 | 2 |
| pass | 16 | 2 | 30 | 3 |
| defer | 3 | 0 | 0 | 21 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 2 | 3 |
| passive | 4 | 6 | 11 | 0 |
| pass | 9 | 3 | 38 | 1 |
| defer | 7 | 0 | 0 | 17 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 65 | 0 | 1 | 6 |
| passive | 4 | 6 | 11 | 0 |
| pass | 7 | 4 | 40 | 0 |
| defer | 4 | 1 | 2 | 17 |

ft-r4b

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 63 | 0 | 1 | 8 |
| passive | 3 | 9 | 7 | 2 |
| pass | 10 | 5 | 35 | 1 |
| defer | 2 | 0 | 1 | 21 |


### en_test：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-004 text: Turn off the lights in the living room. (context: (none)) [others=none]
    gold: respond, True, task_request
    ft-r2 seed0: respond, True, task_request  OK
    ft-r2 seed1: respond, True, task_request  OK
    ft-r2 seed2: respond, True, task_request  OK
    ft-r3 seed0: respond, True, task_request  OK
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: respond, True, task_request  OK
    ft-r4a seed1: respond, True, task_request  OK
    ft-r4a seed2: respond, True, task_request  OK
    ft-r4b seed0: respond, True, task_request  OK
    ft-r4b seed1: respond, True, task_request  OK
    ft-r4b seed2: respond, True, task_request  OK
- zt-027 text: Could you open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3 seed0: pass, False, ignore  OK
    ft-r3 seed1: pass, False, ignore  OK
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, True, task_request
    ft-r4a seed2: pass, False, ignore  OK
    ft-r4b seed0: defer, True, task_request
    ft-r4b seed1: pass, False, ignore  OK
    ft-r4b seed2: respond, True, task_request
- zt-030 text: Hey, let's go to that store next time. (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, True, ignore
    ft-r4a seed2: pass, False, ignore  OK
    ft-r4b seed0: pass, True, ignore
    ft-r4b seed1: passive, True, ignore
    ft-r4b seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. You wanna come with me later? (context: Another user: Yuki, recommend a pasta shop.
Assistant: Many people suggested the station.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, ignore
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, False, ignore  OK
    ft-r4a seed2: pass, False, ignore  OK
    ft-r4b seed0: passive, True, ignore
    ft-r4b seed1: respond, True, ignore
    ft-r4b seed2: pass, False, ignore  OK

## en_reference — 全部列（84 列：turn.reply 56、absent 28；seeds ft-r2=3, ft-r3=3, ft-r4a=3, ft-r4b=3）

| 欄位 | ft-r2 | ft-r3 | ft-r4a | ft-r4b | Δ(ft-r4b-ft-r2) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.714 ± 0.031 | 0.702 ± 0.045 | 0.720 ± 0.021 | 0.732 ± 0.047 | +0.018 | 0.554 |
| reply_mode macro-F1 | 0.611 ± 0.034 | 0.596 ± 0.066 | 0.632 ± 0.038 | 0.655 ± 0.060 | +0.044 | 0.301 |
| addressed acc | 0.833 ± 0.010 | 0.881 ± 0.021 | 0.893 ± 0.018 | 0.875 ± 0.018 | +0.042 | 0.786 |
| route acc | 0.786 ± 0.036 | 0.768 ± 0.031 | 0.839 ± 0.000 | 0.810 ± 0.041 | +0.024 | 0.250 |
| compound acc (turn.reply) | 1.000 ± 0.000 | 1.000 ± 0.000 | 0.994 ± 0.010 | 1.000 ± 0.000 | +0.000 | n/a |
| absent.intent acc | 0.952 ± 0.021 | 0.964 ± 0.000 | 0.952 ± 0.021 | 0.964 ± 0.000 | +0.012 | 0.750 |
| absent.target_location acc | 0.964 ± 0.000 | 0.940 ± 0.041 | 0.964 ± 0.000 | 0.964 ± 0.000 | +0.000 | 1.000 |
| absent.compound acc | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | 1.000 ± 0.000 | +0.000 | n/a |

逐 seed 配對差（ft-r4b − ft-r4a，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.012 ± 0.045 |
| reply_mode macro-F1 | +0.024 ± 0.076 |
| addressed acc | -0.018 ± 0.031 |
| route acc | -0.030 ± 0.041 |
| compound acc (turn.reply) | +0.006 ± 0.010 |
| absent.intent acc | +0.012 ± 0.021 |
| absent.target_location acc | +0.000 ± 0.000 |
| absent.compound acc | +0.000 ± 0.000 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 7 | 0.417 ± 0.144 | 0.444 ± 0.096 | 0.439 ± 0.211 | 0.561 ± 0.211 |
| pass | 17 | 0.674 ± 0.052 | 0.764 ± 0.068 | 0.712 ± 0.147 | 0.773 ± 0.082 |
| defer | 8 | 0.737 ± 0.124 | 0.778 ± 0.192 | 0.722 ± 0.096 | 0.594 ± 0.046 |
| respond | 24 | 0.759 ± 0.023 | 0.697 ± 0.066 | 0.780 ± 0.036 | 0.787 ± 0.056 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 7 | 0.143 ± 0.000 | 0.190 ± 0.082 | 0.238 ± 0.082 | 0.333 ± 0.165 |
| pass | 17 | 0.529 ± 0.059 | 0.608 ± 0.068 | 0.627 ± 0.034 | 0.608 ± 0.090 |
| defer | 8 | 0.875 ± 0.000 | 0.542 ± 0.072 | 0.708 ± 0.072 | 0.792 ± 0.072 |
| respond | 24 | 0.958 ± 0.042 | 0.972 ± 0.024 | 0.931 ± 0.024 | 0.917 ± 0.042 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 69 | 0 | 0 | 3 |
| passive | 3 | 3 | 13 | 2 |
| pass | 16 | 5 | 27 | 3 |
| defer | 3 | 0 | 0 | 21 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 70 | 0 | 0 | 2 |
| passive | 7 | 4 | 10 | 0 |
| pass | 13 | 5 | 31 | 2 |
| defer | 11 | 0 | 0 | 13 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 67 | 0 | 1 | 4 |
| passive | 5 | 5 | 11 | 0 |
| pass | 9 | 7 | 32 | 3 |
| defer | 5 | 0 | 2 | 17 |

ft-r4b

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 66 | 0 | 0 | 6 |
| passive | 3 | 7 | 9 | 2 |
| pass | 10 | 5 | 31 | 5 |
| defer | 5 | 0 | 0 | 19 |


### en_reference：指定題逐題結果（格式 reply_mode, addressed, route）

- zt-004 text: Turn off the lights in the living room. (context: (none)) [others=none]
    gold: respond, True, task_request
    ft-r2 seed0: respond, True, task_request  OK
    ft-r2 seed1: respond, True, task_request  OK
    ft-r2 seed2: respond, True, task_request  OK
    ft-r3 seed0: respond, True, task_request  OK
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: respond, True, task_request  OK
    ft-r4a seed1: respond, True, task_request  OK
    ft-r4a seed2: respond, True, task_request  OK
    ft-r4b seed0: respond, True, task_request  OK
    ft-r4b seed1: respond, True, task_request  OK
    ft-r4b seed2: respond, True, task_request  OK
- zt-027 text: Could you go open the window for me, please? (context: (none)) [others=3 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: defer, True, task_request
    ft-r2 seed1: defer, True, task_request
    ft-r2 seed2: respond, True, task_request
    ft-r3 seed0: pass, True, task_request
    ft-r3 seed1: respond, False, ignore
    ft-r3 seed2: pass, True, ignore
    ft-r4a seed0: pass, False, task_request
    ft-r4a seed1: pass, True, task_request
    ft-r4a seed2: pass, False, ignore  OK
    ft-r4b seed0: defer, True, task_request
    ft-r4b seed1: pass, True, task_request
    ft-r4b seed2: defer, True, task_request
- zt-030 text: Hey, let's go to that place together next time. (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, task_request
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: defer, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: pass, False, ignore  OK
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: pass, False, ignore  OK
    ft-r4a seed2: pass, False, ignore  OK
    ft-r4b seed0: pass, False, ignore  OK
    ft-r4b seed1: passive, True, agent_loop
    ft-r4b seed2: pass, False, ignore  OK
- zt-076 text: I want ramen too. Shall we go together later? (context: Another user: Yuki, recommend a ramen place.
Assistant: The one next to the station is highly recommended by a lot of people.) [others=2 other people present]
    gold: pass, False, ignore
    ft-r2 seed0: respond, True, direct_chat
    ft-r2 seed1: respond, True, direct_chat
    ft-r2 seed2: respond, True, direct_chat
    ft-r3 seed0: respond, True, direct_chat
    ft-r3 seed1: respond, True, direct_chat
    ft-r3 seed2: respond, True, direct_chat
    ft-r4a seed0: pass, False, ignore  OK
    ft-r4a seed1: respond, True, ignore
    ft-r4a seed2: pass, True, ignore
    ft-r4b seed0: pass, True, ignore
    ft-r4b seed1: respond, True, direct_chat
    ft-r4b seed2: pass, False, ignore  OK

## en_independent — 全部列（231 列：turn.reply 159、absent 72；seeds ft-r2=3, ft-r3=3, ft-r4a=3, ft-r4b=3）

| 欄位 | ft-r2 | ft-r3 | ft-r4a | ft-r4b | Δ(ft-r4b-ft-r2) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.788 ± 0.028 | 0.742 ± 0.049 | 0.786 ± 0.006 | 0.786 ± 0.017 | -0.002 | n/a |
| reply_mode macro-F1 | 0.764 ± 0.030 | 0.686 ± 0.054 | 0.747 ± 0.016 | 0.733 ± 0.033 | -0.031 | n/a |
| addressed acc | 0.920 ± 0.013 | 0.948 ± 0.016 | 0.935 ± 0.038 | 0.920 ± 0.024 | +0.000 | n/a |
| route acc | 0.836 ± 0.029 | 0.878 ± 0.031 | 0.870 ± 0.007 | 0.847 ± 0.030 | +0.010 | n/a |
| compound acc (turn.reply) | 0.979 ± 0.004 | 0.964 ± 0.013 | 0.971 ± 0.010 | 0.977 ± 0.013 | -0.002 | n/a |
| absent.intent acc | 0.921 ± 0.035 | 0.903 ± 0.014 | 0.907 ± 0.021 | 0.940 ± 0.021 | +0.019 | n/a |
| absent.target_location acc | 0.977 ± 0.008 | 0.977 ± 0.021 | 0.977 ± 0.021 | 0.981 ± 0.008 | +0.005 | n/a |
| absent.compound acc | 0.986 ± 0.000 | 0.986 ± 0.014 | 0.986 ± 0.014 | 0.981 ± 0.008 | -0.005 | n/a |

逐 seed 配對差（ft-r4b − ft-r4a，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.000 ± 0.022 |
| reply_mode macro-F1 | -0.015 ± 0.049 |
| addressed acc | -0.015 ± 0.016 |
| route acc | -0.023 ± 0.037 |
| compound acc (turn.reply) | +0.006 ± 0.022 |
| absent.intent acc | +0.032 ± 0.021 |
| absent.target_location acc | +0.005 ± 0.016 |
| absent.compound acc | -0.005 ± 0.016 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 26 | 0.857 ± 0.038 | 0.879 ± 0.210 | 0.805 ± 0.115 | 0.780 ± 0.131 |
| pass | 56 | 0.826 ± 0.029 | 0.792 ± 0.066 | 0.836 ± 0.046 | 0.851 ± 0.022 |
| defer | 29 | 0.652 ± 0.022 | 0.858 ± 0.072 | 0.873 ± 0.032 | 0.796 ± 0.045 |
| respond | 48 | 0.826 ± 0.035 | 0.654 ± 0.029 | 0.711 ± 0.043 | 0.727 ± 0.036 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 26 | 0.538 ± 0.038 | 0.346 ± 0.077 | 0.423 ± 0.067 | 0.333 ± 0.118 |
| pass | 56 | 0.845 ± 0.027 | 0.845 ± 0.098 | 0.863 ± 0.041 | 0.845 ± 0.010 |
| defer | 29 | 0.839 ± 0.020 | 0.529 ± 0.053 | 0.701 ± 0.053 | 0.793 ± 0.060 |
| respond | 48 | 0.826 ± 0.048 | 0.965 ± 0.012 | 0.944 ± 0.043 | 0.958 ± 0.000 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 119 | 0 | 1 | 24 |
| passive | 6 | 42 | 28 | 2 |
| pass | 6 | 7 | 142 | 13 |
| defer | 13 | 0 | 1 | 73 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 139 | 0 | 0 | 5 |
| passive | 16 | 27 | 35 | 0 |
| pass | 19 | 4 | 142 | 3 |
| defer | 39 | 0 | 2 | 46 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 136 | 0 | 2 | 6 |
| passive | 20 | 33 | 25 | 0 |
| pass | 12 | 8 | 145 | 3 |
| defer | 24 | 0 | 2 | 61 |

ft-r4b

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 138 | 0 | 0 | 6 |
| passive | 25 | 26 | 24 | 3 |
| pass | 10 | 7 | 142 | 9 |
| defer | 17 | 0 | 1 | 69 |


## en_independent — 排除標準答案與能力規則衝突的題目（7 題：ei-034, ei-060, ei-072, ei-099, ei-116, ei-125, ei-150）（224 列：turn.reply 152、absent 72；seeds ft-r2=3, ft-r3=3, ft-r4a=3, ft-r4b=3）

| 欄位 | ft-r2 | ft-r3 | ft-r4a | ft-r4b | Δ(ft-r4b-ft-r2) | rules.default |
|---|---|---|---|---|---|---|
| reply_mode acc | 0.779 ± 0.030 | 0.748 ± 0.049 | 0.776 ± 0.007 | 0.776 ± 0.017 | -0.002 | n/a |
| reply_mode macro-F1 | 0.754 ± 0.031 | 0.696 ± 0.052 | 0.736 ± 0.015 | 0.723 ± 0.033 | -0.030 | n/a |
| addressed acc | 0.917 ± 0.014 | 0.945 ± 0.017 | 0.932 ± 0.040 | 0.917 ± 0.025 | +0.000 | n/a |
| route acc | 0.829 ± 0.030 | 0.884 ± 0.030 | 0.864 ± 0.008 | 0.840 ± 0.031 | +0.011 | n/a |
| compound acc (turn.reply) | 0.978 ± 0.004 | 0.965 ± 0.010 | 0.969 ± 0.010 | 0.976 ± 0.014 | -0.002 | n/a |
| absent.intent acc | 0.921 ± 0.035 | 0.903 ± 0.014 | 0.907 ± 0.021 | 0.940 ± 0.021 | +0.019 | n/a |
| absent.target_location acc | 0.977 ± 0.008 | 0.977 ± 0.021 | 0.977 ± 0.021 | 0.981 ± 0.008 | +0.005 | n/a |
| absent.compound acc | 0.986 ± 0.000 | 0.986 ± 0.014 | 0.986 ± 0.014 | 0.981 ± 0.008 | -0.005 | n/a |

逐 seed 配對差（ft-r4b − ft-r4a，同 seed 編號相減；mean ± std）

| 欄位 | 差值 |
|---|---|
| reply_mode acc | +0.000 ± 0.023 |
| reply_mode macro-F1 | -0.013 ± 0.047 |
| addressed acc | -0.015 ± 0.017 |
| route acc | -0.024 ± 0.039 |
| compound acc (turn.reply) | +0.007 ± 0.023 |
| absent.intent acc | +0.032 ± 0.021 |
| absent.target_location acc | +0.005 ± 0.016 |
| absent.compound acc | -0.005 ± 0.016 |

reply_mode 各類 precision（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 26 | 0.857 ± 0.038 | 0.879 ± 0.210 | 0.805 ± 0.115 | 0.780 ± 0.131 |
| pass | 56 | 0.826 ± 0.029 | 0.797 ± 0.072 | 0.836 ± 0.046 | 0.851 ± 0.022 |
| defer | 26 | 0.622 ± 0.024 | 0.855 ± 0.073 | 0.854 ± 0.036 | 0.773 ± 0.048 |
| respond | 44 | 0.810 ± 0.038 | 0.656 ± 0.029 | 0.692 ± 0.044 | 0.709 ± 0.037 |

reply_mode 各類 recall（mean ± std over seeds）

| 類別 | n_gold | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|---|
| passive | 26 | 0.538 ± 0.038 | 0.346 ± 0.077 | 0.423 ± 0.067 | 0.333 ± 0.118 |
| pass | 56 | 0.845 ± 0.027 | 0.845 ± 0.098 | 0.863 ± 0.041 | 0.845 ± 0.010 |
| defer | 26 | 0.821 ± 0.022 | 0.577 ± 0.038 | 0.667 ± 0.059 | 0.769 ± 0.067 |
| respond | 44 | 0.811 ± 0.052 | 0.962 ± 0.013 | 0.939 ± 0.047 | 0.955 ± 0.000 |

reply_mode 混淆矩陣（列＝標準答案，欄＝預測；各 seed 加總）

ft-r2

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 107 | 0 | 1 | 24 |
| passive | 6 | 42 | 28 | 2 |
| pass | 6 | 7 | 142 | 13 |
| defer | 13 | 0 | 1 | 64 |

ft-r3

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 127 | 0 | 0 | 5 |
| passive | 16 | 27 | 35 | 0 |
| pass | 19 | 4 | 142 | 3 |
| defer | 32 | 0 | 1 | 45 |

ft-r4a

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 124 | 0 | 2 | 6 |
| passive | 20 | 33 | 25 | 0 |
| pass | 12 | 8 | 145 | 3 |
| defer | 24 | 0 | 2 | 52 |

ft-r4b

| gold \ pred | respond | passive | pass | defer |
|---|---|---|---|---|
| respond | 126 | 0 | 0 | 6 |
| passive | 25 | 26 | 24 | 3 |
| pass | 10 | 7 | 142 | 9 |
| defer | 17 | 0 | 1 | 60 |


## capability_pairs — 能力成對題（42 列：turn.reply 42、absent 0；seeds ft-r2=3, ft-r3=3, ft-r4a=3, ft-r4b=3）

A 類 11 組、B 類 10 組（mean ± std over seeds）

| 指標 | ft-r2 | ft-r3 | ft-r4a | ft-r4b |
|---|---|---|---|---|
| A 類：換清單後 reply_mode＋route 不變的比例 | 1.000 ± 0.000 | 0.606 ± 0.052 | 1.000 ± 0.000 | 0.939 ± 0.052 |
| A 類：reply_mode＋route 都對 | 0.636 ± 0.000 | 0.636 ± 0.000 | 0.818 ± 0.000 | 0.773 ± 0.045 |
| B 類：有能力 passive 且沒能力 pass 的比例 | 0.000 ± 0.000 | 0.033 ± 0.058 | 0.067 ± 0.058 | 0.100 ± 0.100 |
| B 類：有能力版 reply_mode 準確率 | 0.600 ± 0.100 | 0.400 ± 0.100 | 0.400 ± 0.100 | 0.500 ± 0.100 |
| B 類：沒能力版 reply_mode 準確率 | 0.333 ± 0.153 | 0.500 ± 0.100 | 0.467 ± 0.058 | 0.533 ± 0.153 |

