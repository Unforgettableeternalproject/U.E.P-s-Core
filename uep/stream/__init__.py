"""Echo Stream 併核層（N1；D104／D105）：串流語音對話管線的契約與核心，原 ES repo 的 ``echo_stream``。

來源：Echo-Stream develop 07e3ff8（複製後在 Core 內演進，Core 為權威）。

單向依賴：本套件不 import ``uep`` 其他部分；``uep/echo/`` 依賴它。子套件規則：

* ``contracts/``、本層檔案（coordinator、channel、splitter、tracer、policies、turn_detector、emotion、
  gap_filler、pipeline）、``audio/``、``io/``：零子系統依賴，只用標準函式庫與 numpy。
* ``adapters/``：唯一可以 import 子系統（echo_stt／echo_tts／echo_memory／echo_thought_core／llm_integration）
  的地方；原樣自 ES 搬入，隨 M3／M5／N3／N5 重寫。
"""
