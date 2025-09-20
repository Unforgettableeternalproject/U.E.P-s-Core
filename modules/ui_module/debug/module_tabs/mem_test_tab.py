# module_tabs/mem_test_tab.py
"""
MEM 記憶模組測試分頁

提供記憶模組的完整測試功能，包括：
- 身份令牌管理
- 記憶存儲與檢索
- 對話快照管理
- 語義查詢測試
- 完整工作流程測試
"""

import os
import sys
import json
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# 添加當前目錄以導入本地模組
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from base_test_tab import BaseTestTab


class MEMTestTab(BaseTestTab):
    """MEM 記憶模組測試分頁"""
    
    def __init__(self):
        super().__init__("mem")
        self.MODULE_DISPLAY_NAME = "MEM 記憶模組"
        self.current_identity_token = None
        self.test_data = {
            "test_conversations": [
                "你好，今天天氣很不錯呢！",
                "我想了解一些關於人工智能的知識",
                "能告訴我今天的計劃安排嗎？",
                "我對機器學習很感興趣",
                "你能幫我記住這個重要的日期嗎？"
            ],
            "test_queries": [
                "天氣相關的記憶",
                "人工智能相關內容",
                "學習相關的對話",
                "日期和時間",
                "重要的事件"
            ]
        }
    
    def create_control_section(self, main_layout):
        """建立 MEM 記憶模組控制區域"""
        control_group = QGroupBox("MEM 記憶模組測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 身份管理區域
        identity_group = self.create_identity_section()
        control_layout.addWidget(identity_group)
        
        # 記憶操作區域
        memory_group = self.create_memory_section()
        control_layout.addWidget(memory_group)
        
        # 測試場景區域
        scenario_group = self.create_scenario_section()
        control_layout.addWidget(scenario_group)
        
        # 系統管理區域
        system_group = self.create_system_section()
        control_layout.addWidget(system_group)
        
        main_layout.addWidget(control_group)
    
    def create_identity_section(self):
        """創建身份管理區域"""
        identity_group = QGroupBox("身份令牌管理")
        identity_layout = QVBoxLayout(identity_group)
        
        # 身份創建區域
        create_layout = QHBoxLayout()
        
        self.user_name_input = QLineEdit()
        self.user_name_input.setPlaceholderText("輸入用戶名稱...")
        self.user_name_input.setText("測試用戶")
        create_layout.addWidget(QLabel("用戶名稱:"))
        create_layout.addWidget(self.user_name_input)
        
        create_identity_btn = QPushButton("🔑 創建身份令牌")
        create_identity_btn.clicked.connect(self.create_identity_token)
        create_layout.addWidget(create_identity_btn)
        
        identity_layout.addLayout(create_layout)
        
        # 身份管理按鈕組
        identity_btn_layout = QHBoxLayout()
        
        list_identities_btn = QPushButton("📋 列出所有身份")
        list_identities_btn.clicked.connect(self.list_identities)
        identity_btn_layout.addWidget(list_identities_btn)
        
        identity_stats_btn = QPushButton("📊 身份統計")
        identity_stats_btn.clicked.connect(self.show_identity_stats)
        identity_btn_layout.addWidget(identity_stats_btn)
        
        delete_identity_btn = QPushButton("🗑️ 刪除身份")
        delete_identity_btn.clicked.connect(self.delete_identity)
        delete_identity_btn.setStyleSheet("QPushButton { color: #ff6b6b; }")
        identity_btn_layout.addWidget(delete_identity_btn)
        
        identity_layout.addLayout(identity_btn_layout)
        
        # 當前身份顯示
        self.current_identity_label = QLabel("當前身份: 無")
        self.current_identity_label.setStyleSheet("color: #666; font-style: italic; padding: 5px;")
        identity_layout.addWidget(self.current_identity_label)
        
        return identity_group
    
    def create_memory_section(self):
        """創建記憶操作區域"""
        memory_group = QGroupBox("記憶操作")
        memory_layout = QVBoxLayout(memory_group)
        
        # 對話輸入區域
        conversation_layout = QHBoxLayout()
        
        self.conversation_input = QTextEdit()
        self.conversation_input.setMaximumHeight(80)
        self.conversation_input.setPlaceholderText("輸入對話內容...")
        conversation_layout.addWidget(self.conversation_input)
        
        add_conversation_btn = QPushButton("💬 添加對話")
        add_conversation_btn.clicked.connect(self.add_conversation)
        conversation_layout.addWidget(add_conversation_btn)
        
        memory_layout.addLayout(conversation_layout)
        
        # 查詢區域
        query_layout = QHBoxLayout()
        
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("輸入查詢關鍵詞...")
        query_layout.addWidget(QLabel("查詢:"))
        query_layout.addWidget(self.query_input)
        
        query_memory_btn = QPushButton("🔍 查詢記憶")
        query_memory_btn.clicked.connect(self.query_memory)
        query_layout.addWidget(query_memory_btn)
        
        memory_layout.addLayout(query_layout)
        
        # 記憶管理按鈕組
        memory_btn_layout = QHBoxLayout()
        
        create_snapshot_btn = QPushButton("📸 創建快照")
        create_snapshot_btn.clicked.connect(self.create_conversation_snapshot)
        memory_btn_layout.addWidget(create_snapshot_btn)
        
        list_snapshots_btn = QPushButton("📚 列出快照")
        list_snapshots_btn.clicked.connect(self.list_snapshots)
        memory_btn_layout.addWidget(list_snapshots_btn)
        
        export_memory_btn = QPushButton("📤 導出記憶")
        export_memory_btn.clicked.connect(self.export_memory)
        memory_btn_layout.addWidget(export_memory_btn)
        
        memory_layout.addLayout(memory_btn_layout)
        
        return memory_group
    
    def create_scenario_section(self):
        """創建測試場景區域"""
        scenario_group = QGroupBox("測試場景")
        scenario_layout = QVBoxLayout(scenario_group)
        
        # 預設場景按鈕組
        preset_layout = QHBoxLayout()
        
        basic_test_btn = QPushButton("🧪 基本功能測試")
        basic_test_btn.clicked.connect(self.run_basic_test)
        preset_layout.addWidget(basic_test_btn)
        
        integration_test_btn = QPushButton("🔗 NLP 整合測試")
        integration_test_btn.clicked.connect(self.run_nlp_integration_test)
        preset_layout.addWidget(integration_test_btn)
        
        workflow_test_btn = QPushButton("🔄 完整工作流程")
        workflow_test_btn.clicked.connect(self.run_full_workflow_test)
        preset_layout.addWidget(workflow_test_btn)
        
        scenario_layout.addLayout(preset_layout)
        
        # 進階測試按鈕組
        advanced_layout = QHBoxLayout()
        
        llm_context_btn = QPushButton("🤖 LLM 上下文測試")
        llm_context_btn.clicked.connect(self.run_llm_context_test)
        advanced_layout.addWidget(llm_context_btn)
        
        stress_test_btn = QPushButton("⚡ 壓力測試")
        stress_test_btn.clicked.connect(self.run_stress_test)
        advanced_layout.addWidget(stress_test_btn)
        
        performance_test_btn = QPushButton("📈 性能測試")
        performance_test_btn.clicked.connect(self.run_performance_test)
        advanced_layout.addWidget(performance_test_btn)
        
        scenario_layout.addLayout(advanced_layout)
        
        return scenario_group
    
    def create_system_section(self):
        """創建系統管理區域"""
        system_group = QGroupBox("系統管理")
        system_layout = QVBoxLayout(system_group)
        
        # 系統信息按鈕組
        info_layout = QHBoxLayout()
        
        memory_stats_btn = QPushButton("📊 記憶統計")
        memory_stats_btn.clicked.connect(self.show_memory_stats)
        info_layout.addWidget(memory_stats_btn)
        
        storage_info_btn = QPushButton("💾 存儲信息")
        storage_info_btn.clicked.connect(self.show_storage_info)
        info_layout.addWidget(storage_info_btn)
        
        vector_index_btn = QPushButton("🔢 向量索引")
        vector_index_btn.clicked.connect(self.show_vector_index_info)
        info_layout.addWidget(vector_index_btn)
        
        system_layout.addLayout(info_layout)
        
        # 維護操作按鈕組
        maintenance_layout = QHBoxLayout()
        
        rebuild_index_btn = QPushButton("🔧 重建索引")
        rebuild_index_btn.clicked.connect(self.rebuild_vector_index)
        maintenance_layout.addWidget(rebuild_index_btn)
        
        cleanup_btn = QPushButton("🧹 清理過期數據")
        cleanup_btn.clicked.connect(self.cleanup_expired_data)
        maintenance_layout.addWidget(cleanup_btn)
        
        reset_btn = QPushButton("🔄 重置所有數據")
        reset_btn.clicked.connect(self.reset_all_data)
        reset_btn.setStyleSheet("QPushButton { color: #ff6b6b; font-weight: bold; }")
        maintenance_layout.addWidget(reset_btn)
        
        system_layout.addLayout(maintenance_layout)
        
        return system_group
    
    # ===== 身份管理功能 =====
    
    def create_identity_token(self):
        """創建身份令牌"""
        user_name = self.user_name_input.text().strip()
        if not user_name:
            self.append_to_output("❌ 請輸入用戶名稱")
            return
        
        self.append_to_output(f"🔑 正在為用戶 '{user_name}' 創建身份令牌...")
        
        try:
            # 呼叫 debug_api 中的測試函數
            from devtools.debug_api import mem_test_identity_token_creation_wrapper
            result = mem_test_identity_token_creation_wrapper(user_name)
            
            if result.get('success'):
                token = result.get('token')
                if token:
                    self.current_identity_token = token.memory_token
                    self.current_identity_label.setText(f"當前身份: {user_name} ({token.memory_token})")
                    
                    self.append_to_output("✅ 身份令牌創建成功:")
                    self.append_to_output(f"   身份ID: {token.identity_id}")
                    self.append_to_output(f"   顯示名稱: {token.display_name}")
                    self.append_to_output(f"   記憶令牌: {token.memory_token}")
                    self.append_to_output(f"   創建時間: {token.created_at}")
                    self.append_to_output(f"   總互動次數: {token.total_interactions}")
                else:
                    self.append_to_output("❌ 身份令牌創建失敗：無法獲取令牌對象")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 身份令牌創建失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 身份令牌創建異常：{str(e)}")
    
    def list_identities(self):
        """列出所有身份"""
        self.append_to_output("📋 正在列出所有身份...")
        self.run_test("identity_list")
    
    def show_identity_stats(self):
        """顯示身份統計"""
        self.append_to_output("📊 正在獲取身份統計...")
        
        try:
            from devtools.debug_api import mem_test_identity_manager_stats_wrapper
            result = mem_test_identity_manager_stats_wrapper()
            
            if result.get('success'):
                stats = result.get('stats', {})
                self.append_to_output("✅ 身份管理統計:")
                for key, value in stats.items():
                    self.append_to_output(f"   {key}: {value}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 獲取統計失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 統計異常：{str(e)}")
    
    def delete_identity(self):
        """刪除身份"""
        if not self.current_identity_token:
            self.append_to_output("❌ 未選擇要刪除的身份")
            return
            
        reply = QMessageBox.question(self, '確認刪除', 
                                   f'確定要刪除身份令牌 {self.current_identity_token} 嗎？',
                                   QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            self.append_to_output(f"🗑️ 正在刪除身份令牌 {self.current_identity_token}...")
            self.run_test("identity_delete", {"token": self.current_identity_token})
    
    # ===== 記憶操作功能 =====
    
    def add_conversation(self):
        """添加對話記憶"""
        conversation = self.conversation_input.toPlainText().strip()
        if not conversation:
            self.append_to_output("❌ 請輸入對話內容")
            return
            
        if not self.current_identity_token:
            self.append_to_output("❌ 請先創建身份令牌")
            return
        
        self.append_to_output(f"💬 正在添加對話記憶...")
        self.append_to_output(f"   內容: {conversation}")
        
        # 清空輸入框
        self.conversation_input.clear()
        
        self.run_test("conversation_add", {
            "token": self.current_identity_token,
            "conversation": conversation
        })
    
    def query_memory(self):
        """查詢記憶"""
        query = self.query_input.text().strip()
        if not query:
            self.append_to_output("❌ 請輸入查詢關鍵詞")
            return
            
        if not self.current_identity_token:
            self.append_to_output("❌ 請先創建身份令牌")
            return
        
        self.append_to_output(f"🔍 正在查詢記憶...")
        self.append_to_output(f"   關鍵詞: {query}")
        
        try:
            from devtools.debug_api import mem_test_memory_query_wrapper
            result = mem_test_memory_query_wrapper(self.current_identity_token, query)
            
            if result.get('success'):
                memories = result.get('memories', [])
                self.append_to_output(f"✅ 找到 {len(memories)} 條相關記憶:")
                for i, memory in enumerate(memories[:5]):  # 只顯示前5條
                    self.append_to_output(f"   {i+1}. {memory}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 查詢失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 查詢異常：{str(e)}")
    
    def create_conversation_snapshot(self):
        """創建對話快照"""
        conversation = self.conversation_input.toPlainText().strip()
        if not conversation:
            # 使用預設對話
            conversation = "這是一段測試對話內容，用於創建快照。"
            
        if not self.current_identity_token:
            self.append_to_output("❌ 請先創建身份令牌")
            return
        
        self.append_to_output(f"📸 正在創建對話快照...")
        
        try:
            from devtools.debug_api import mem_test_conversation_snapshot_wrapper
            result = mem_test_conversation_snapshot_wrapper(self.current_identity_token, conversation)
            
            if result.get('success'):
                snapshot = result.get('snapshot')
                self.append_to_output("✅ 對話快照創建成功:")
                self.append_to_output(f"   快照ID: {snapshot}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 快照創建失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 快照創建異常：{str(e)}")
    
    def list_snapshots(self):
        """列出所有快照"""
        self.append_to_output("📚 正在列出所有快照...")
        self.run_test("snapshot_list")
    
    def export_memory(self):
        """導出記憶數據"""
        if not self.current_identity_token:
            self.append_to_output("❌ 請先創建身份令牌")
            return
            
        self.append_to_output("📤 正在導出記憶數據...")
        self.run_test("memory_export", {"token": self.current_identity_token})
    
    # ===== 測試場景功能 =====
    
    def run_basic_test(self):
        """執行基本功能測試"""
        self.append_to_output("🧪 開始基本功能測試...")
        
        # 如果沒有身份令牌，先創建一個
        if not self.current_identity_token:
            self.user_name_input.setText("基本測試用戶")
            self.create_identity_token()
            
        # 添加一些測試對話
        test_conversations = [
            "你好，我是新用戶",
            "今天天氣很好",
            "我想學習人工智能"
        ]
        
        for conversation in test_conversations:
            self.conversation_input.setPlainText(conversation)
            self.add_conversation()
        
        # 執行查詢測試
        self.query_input.setText("天氣")
        self.query_memory()
        
        self.append_to_output("✅ 基本功能測試完成")
    
    def run_nlp_integration_test(self):
        """執行 NLP 整合測試"""
        self.append_to_output("🔗 開始 NLP 整合測試...")
        
        try:
            from devtools.debug_api import mem_test_nlp_integration_wrapper
            result = mem_test_nlp_integration_wrapper()
            
            if result.get('success'):
                self.append_to_output("✅ NLP 整合測試成功")
                self.append_to_output(f"   處理結果: {result.get('result', 'N/A')}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ NLP 整合測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ NLP 整合測試異常：{str(e)}")
    
    def run_full_workflow_test(self):
        """執行完整工作流程測試"""
        self.append_to_output("🔄 開始完整工作流程測試...")
        
        try:
            from devtools.debug_api import mem_test_full_workflow_wrapper
            result = mem_test_full_workflow_wrapper("工作流程測試用戶")
            
            if result.get('success'):
                self.append_to_output("✅ 完整工作流程測試成功")
                workflow_results = result.get('workflow_results', {})
                for step, step_result in workflow_results.items():
                    status = "✅" if step_result.get('success') else "❌"
                    self.append_to_output(f"   {status} {step}: {step_result.get('message', 'N/A')}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 完整工作流程測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 完整工作流程測試異常：{str(e)}")
    
    def run_llm_context_test(self):
        """執行 LLM 上下文測試"""
        self.append_to_output("🤖 開始 LLM 上下文測試...")
        
        try:
            from devtools.debug_api import mem_test_llm_context_extraction_wrapper
            result = mem_test_llm_context_extraction_wrapper(
                self.current_identity_token or "test_user", 
                "學習相關內容"
            )
            
            if result.get('success'):
                self.append_to_output("✅ LLM 上下文測試成功")
                context = result.get('context', 'N/A')
                self.append_to_output(f"   提取的上下文: {context}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ LLM 上下文測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ LLM 上下文測試異常：{str(e)}")
    
    def run_stress_test(self):
        """執行壓力測試"""
        self.append_to_output("⚡ 開始壓力測試...")
        self.append_to_output("   正在創建大量測試數據...")
        
        # 創建多個身份並添加大量對話
        stress_test_data = {
            "users": 10,
            "conversations_per_user": 20,
            "queries_per_user": 5
        }
        
        self.append_to_output(f"   測試參數: {stress_test_data}")
        self.run_test("stress_test", stress_test_data)
    
    def run_performance_test(self):
        """執行性能測試"""
        self.append_to_output("📈 開始性能測試...")
        self.append_to_output("   測量響應時間和記憶體使用...")
        self.run_test("performance_test")
    
    # ===== 系統管理功能 =====
    
    def show_memory_stats(self):
        """顯示記憶統計"""
        self.append_to_output("📊 正在獲取記憶統計...")
        self.run_test("memory_stats")
    
    def show_storage_info(self):
        """顯示存儲信息"""
        self.append_to_output("💾 正在獲取存儲信息...")
        self.run_test("storage_info")
    
    def show_vector_index_info(self):
        """顯示向量索引信息"""
        self.append_to_output("🔢 正在獲取向量索引信息...")
        self.run_test("vector_index_info")
    
    def rebuild_vector_index(self):
        """重建向量索引"""
        reply = QMessageBox.question(self, '確認重建', 
                                   '重建向量索引可能需要較長時間，確定要繼續嗎？',
                                   QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            self.append_to_output("🔧 正在重建向量索引...")
            self.run_test("rebuild_index")
    
    def cleanup_expired_data(self):
        """清理過期數據"""
        self.append_to_output("🧹 正在清理過期數據...")
        self.run_test("cleanup_expired")
    
    def reset_all_data(self):
        """重置所有數據"""
        reply = QMessageBox.warning(self, '危險操作', 
                                   '這將刪除所有 MEM 模組數據，包括所有身份和記憶！\n確定要繼續嗎？',
                                   QMessageBox.Yes | QMessageBox.No,
                                   QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            # 二次確認
            text, ok = QInputDialog.getText(self, '最終確認', 
                                          '請輸入 "RESET ALL" 來確認重置操作:')
            
            if ok and text == "RESET ALL":
                self.append_to_output("🔄 正在重置所有數據...")
                self.current_identity_token = None
                self.current_identity_label.setText("當前身份: 無")
                self.run_test("reset_all")
            else:
                self.append_to_output("❌ 重置操作已取消")
    
    # ===== 輔助功能 =====
    
    def get_test_conversation(self):
        """獲取測試對話內容"""
        conversation = self.conversation_input.toPlainText().strip()
        if not conversation:
            # 使用預設測試對話
            import random
            conversation = random.choice(self.test_data["test_conversations"])
            self.conversation_input.setPlainText(conversation)
        
        return conversation
    
    def get_test_query(self):
        """獲取測試查詢內容"""
        query = self.query_input.text().strip()
        if not query:
            # 使用預設測試查詢
            import random
            query = random.choice(self.test_data["test_queries"])
            self.query_input.setText(query)
        
        return query
    
    def handle_test_result(self, test_type: str, result: dict):
        """處理測試結果"""
        if result.get('success'):
            self.append_to_output(f"✅ {test_type} 測試成功")
            
            # 根據不同的測試類型顯示特定信息
            if test_type == "identity_token_creation":
                token = result.get('token')
                if token:
                    self.current_identity_token = token.memory_token
                    self.current_identity_label.setText(f"當前身份: {token.display_name} ({token.memory_token})")
                    
            elif test_type in ["memory_stats", "storage_info", "vector_index_info"]:
                stats = result.get('stats', result.get('info', {}))
                for key, value in stats.items():
                    self.append_to_output(f"   {key}: {value}")
                    
            elif test_type == "performance_test":
                performance = result.get('performance', {})
                self.append_to_output(f"   平均響應時間: {performance.get('avg_response_time', 'N/A')} ms")
                self.append_to_output(f"   記憶體使用: {performance.get('memory_usage', 'N/A')} MB")
                
        else:
            error = result.get('error', '未知錯誤')
            self.append_to_output(f"❌ {test_type} 測試失敗: {error}")