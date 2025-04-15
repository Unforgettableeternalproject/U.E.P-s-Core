from core.module_base import BaseModule
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
import os
import json
from typing import List
from .schemas import MEMInput, MEMOutput
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log

class MEMModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or load_module_config("mem_module")
        self.embedding_model = self.config.get("embedding_model", "all-MiniLM-L6-v2")
        self.index_file = self.config.get("index_file", "memory/faiss_index")
        self.metadata_file = self.config.get("metadata_file", "memory/metadata.json")
        self.model = None
        self.index = None
        self.metadata: List[dict] = []
        self.dimension = 384

    def debug(self):
        # Debug level = 1
        debug_log(1, "[MEM] Debug 模式啟用")
        # Debug level = 2
        debug_log(2, f"[MEM] 模組設定: {self.config}")
        # Debug level = 3
        debug_log(3, f"[MEM] 嵌入模型: {self.embedding_model}")
        debug_log(3, f"[MEM] FAISS 索引檔案: {self.index_file}")
        debug_log(3, f"[MEM] 元資料檔案: {self.metadata_file}")

    def initialize(self):
        debug_log(1, "[MEM] 初始化中...")
        self.debug()

        info_log(f"[MEM] 載入嵌入模型中（來自 {self.embedding_model}）...")

        self.model = SentenceTransformer(self.embedding_model)
        try:
            if self._faiss_index_exists():
                info_log("[MEM] 正在載入FAISS索引")
                self._load_index()
            else:
                info_log("[MEM] FAISS 索引不存在，正在創建索引")
                self._create_index()

            info_log(f"[MEM] 初始化完成，使用的嵌入模型: {self.embedding_model}")
        except Exception as e:
            error_log(f"[MEM] 初始化失敗: {e}")
            raise e


    def handle(self, data: dict) -> dict:
        payload = MEMInput(**data)
        debug_log(1, f"[MEM] 接收到的資料: {payload}")

        if payload.mode == "fetch":
            info_log("[MEM] 查詢模式啟用")

            if payload.text is None:
                info_log("[MEM] 查詢文本為空，請提供有效的文本", "WARNING")
                return {"error": "請提供查詢文本"}
            results = self._retrieve_memory(payload.text, payload.top_k)

            if not results:
                info_log("[MEM] 查詢結果為空", "WARNING")
                return {"error": "查詢結果為空"}

            debug_log(1, f"[MEM] 查詢結果: {results}")
            return MEMOutput(results=results).dict()

        elif payload.mode == "store":
            info_log("[MEM] 儲存模式啟用")
            if payload.entry is None:
                info_log("[MEM] 儲存文本為空，請提供有效的文本", "WARNING")
                return {"error": "請提供儲存文本"}

            try:
                entry = payload.entry
                self._add_memory(entry["user"], entry)
                info_log(f"[MEM] 儲存成功: {entry}")
            except Exception as e:
                error_log(f"[MEM] 儲存失敗: {e}")
                return {"error": f"儲存失敗: {e}"}
            return {"status": "stored"}

        else:
            return {"error": f"不支援的模式: {payload.mode}"}

    def _embed_text(self, text):
        embedding = self.model.encode(text)
        return embedding.astype(np.float32)

    def _add_memory(self, text, metadata):
        if self.index is None:
            self._create_index()

        embedding = self._embed_text(text)
        self.index.add(np.array([embedding]))
        self.metadata.append(metadata)

        debug_log_e(1, f"[MEM] 新增記憶: {text}")
        debug_log_e(2, f"[MEM] 新增記憶的元資料: {metadata}")
        debug_log_e(2, f"[MEM] 當前記憶數量: {len(self.metadata)}")
        debug_log_e(2, f"[MEM] 當前索引維度: {self.index.ntotal}")

        faiss.write_index(self.index, self.index_file)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f)
        debug_log(1, "[MEM] 記憶已儲存到索引和元資料檔案中")

    def _retrieve_memory(self, query, top_k=5):
        if self.index is None:
            if self._faiss_index_exists():
                self._load_index()
            else:
                error_log("[MEM] FAISS 索引不存在，無法檢索記憶")
                return []

        query_embedding = self._embed_text(query)

        debug_log(1, f"[MEM] 查詢文本: {query}")
        debug_log(2, f"[MEM] 查詢嵌入: {query_embedding}")

        _, indices = self.index.search(np.array([query_embedding]), top_k)

        debug_log(3, f"[MEM] 檢索到的索引: {indices}")

        return [self.metadata[i] for i in indices[0] if i < len(self.metadata)]

    def _faiss_index_exists(self):
        return os.path.exists(self.index_file)

    def _create_index(self):
        self.index = faiss.IndexFlatL2(self.dimension)
        self.metadata = []
        faiss.write_index(self.index, self.index_file)
        with open(self.metadata_file, "w") as f:
            json.dump(self.metadata, f)

        debug_log(1, "[MEM] FAISS 索引和元資料檔案已創建")

    def _load_index(self):
        self.index = faiss.read_index(self.index_file)
        with open(self.metadata_file, "r") as f:
            self.metadata = json.load(f)

        debug_log(1, "[MEM] FAISS 索引和元資料檔案已載入")

    def shutdown(self):
        info_log("[MEM] 模組關閉")
