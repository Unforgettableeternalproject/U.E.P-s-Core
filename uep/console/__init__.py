"""Core 端到端驗收工具（N2，D104／D105 Q3）：``python -m uep.console``。

in-process 組裝 Core 與 ``EchoPipelineHost``；v0 以 fake stage 驅動 A0 與 Core 層隔離，
真實子系統（real 模式）待 M3／M5 接線。console 屬 uep 內部，不 import 任何子系統。
"""
