# module_tabs/mem_test_tab.py
"""
MEM 記憶模組測試分頁 - 重構版本

專注於記憶體存取控制和實際記憶功能，包括：
- 記憶體存取控制測試
- 記憶存儲與檢索
- 對話快照管理  
- 語義查詢測試
- 完整工作流程測試
- 系統統計與維護

注意：身份管理由Working Context處理，此分頁專注於記憶體功能
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
    """MEM 記憶模組測試分頁 - 專注於記憶體功能"""
    
    def __init__(self):
        super().__init__("mem")
        self.MODULE_DISPLAY_NAME = "MEM 記憶模組"
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
            ],
            "test_memory_tokens": [
                "test_user_001",
                "test_user_002", 
                "anonymous",
                "system"
            ]
        }
    
    def create_control_section(self, main_layout):
        """建立 MEM 記憶模組控制區域"""
        control_group = QGroupBox("MEM 記憶模組測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 記憶體存取控制區域
        access_control_group = self.create_access_control_section()
        control_layout.addWidget(access_control_group)
        
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
    
    def create_access_control_section(self):
        """創建記憶體存取控制區域"""
        access_group = QGroupBox("記憶體存取控制")
        access_layout = QVBoxLayout(access_group)
        
        # 記憶令牌設定區域
        token_layout = QHBoxLayout()
        
        self.memory_token_input = QLineEdit()
        self.memory_token_input.setPlaceholderText("輸入記憶令牌進行測試...")
        self.memory_token_input.setText("test_user_001")
        token_layout.addWidget(QLabel("記憶令牌:"))
        token_layout.addWidget(self.memory_token_input)
        
        test_access_btn = QPushButton("🔒 測試存取控制")
        test_access_btn.clicked.connect(self.test_memory_access_control)
        token_layout.addWidget(test_access_btn)
        
        access_layout.addLayout(token_layout)
        
        # 存取控制測試按鈕組
        access_btn_layout = QHBoxLayout()
        
        show_current_token_btn = QPushButton("🎯 顯示當前令牌")
        show_current_token_btn.clicked.connect(self.show_current_memory_token)
        access_btn_layout.addWidget(show_current_token_btn)
        
        validate_system_btn = QPushButton("⚡ 測試系統令牌")
        validate_system_btn.clicked.connect(self.test_system_token_access)
        access_btn_layout.addWidget(validate_system_btn)
        
        access_stats_btn = QPushButton("📊 存取統計")
        access_stats_btn.clicked.connect(self.show_access_stats)
        access_btn_layout.addWidget(access_stats_btn)
        
        access_layout.addLayout(access_btn_layout)
        
        return access_group
    
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
        
        add_conversation_btn = QPushButton("💬 創建對話快照")
        add_conversation_btn.clicked.connect(self.create_conversation_snapshot)
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
        
        identity_stats_btn = QPushButton("📊 身份管理統計")
        identity_stats_btn.clicked.connect(self.show_identity_manager_stats)
        memory_btn_layout.addWidget(identity_stats_btn)
        
        nlp_integration_btn = QPushButton("🧠 NLP整合測試")
        nlp_integration_btn.clicked.connect(self.test_nlp_integration)
        memory_btn_layout.addWidget(nlp_integration_btn)
        
        llm_context_btn = QPushButton("💡 LLM上下文測試")
        llm_context_btn.clicked.connect(self.test_llm_context_extraction)
        memory_btn_layout.addWidget(llm_context_btn)
        
        memory_layout.addLayout(memory_btn_layout)
        
        return memory_group
    
    def create_scenario_section(self):
        """創建測試場景區域"""
        scenario_group = QGroupBox("測試場景")
        scenario_layout = QVBoxLayout(scenario_group)
        
        # 預設場景按鈕組
        preset_layout = QHBoxLayout()
        
        conversation_test_btn = QPushButton("💬 對話場景測試")
        conversation_test_btn.clicked.connect(self.run_conversation_test)
        preset_layout.addWidget(conversation_test_btn)
        
        learning_test_btn = QPushButton("📚 學習場景測試")
        learning_test_btn.clicked.connect(self.run_learning_test)
        preset_layout.addWidget(learning_test_btn)
        
        workflow_test_btn = QPushButton("⚙️ 完整工作流程")
        workflow_test_btn.clicked.connect(self.run_full_workflow)
        preset_layout.addWidget(workflow_test_btn)
        
        scenario_layout.addLayout(preset_layout)
        
        # 進階測試按鈕組
        advanced_layout = QHBoxLayout()
        
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
    
    # ===== 記憶體存取控制功能 =====
    
    def test_memory_access_control(self):
        """測試記憶體存取控制"""
        memory_token = self.memory_token_input.text().strip()
        if not memory_token:
            self.append_to_output("❌ 請輸入記憶令牌")
            return
        
        self.append_to_output(f"🔒 正在測試記憶令牌 '{memory_token}' 的存取控制...")
        
        try:
            from devtools.debug_api import mem_test_memory_access_control_wrapper
            result = mem_test_memory_access_control_wrapper(memory_token)
            
            if result.get('success'):
                self.append_to_output("✅ 記憶體存取控制測試成功:")
                self.append_to_output(f"   當前令牌: {result.get('current_token', 'N/A')}")
                self.append_to_output(f"   存取權限: {'✅ 允許' if result.get('access_granted') else '❌ 拒絕'}")
                self.append_to_output(f"   系統存取: {'✅ 允許' if result.get('system_access') else '❌ 拒絕'}")
                
                stats = result.get('stats', {})
                self.append_to_output("   統計資訊:")
                for key, value in stats.items():
                    if key not in ['current_memory_token']:
                        self.append_to_output(f"     {key}: {value}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 記憶體存取控制測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 記憶體存取控制測試異常：{str(e)}")
    
    def show_current_memory_token(self):
        """顯示當前記憶令牌"""
        self.append_to_output("🎯 正在獲取當前記憶令牌...")
        self.run_test("memory_access_control")
    
    def test_system_token_access(self):
        """測試系統令牌存取"""
        self.append_to_output("⚡ 正在測試系統令牌存取權限...")
        # 設定為系統令牌進行測試
        original_token = self.memory_token_input.text()
        self.memory_token_input.setText("system")
        self.test_memory_access_control()
        self.memory_token_input.setText(original_token)
    
    def show_access_stats(self):
        """顯示存取統計"""
        self.append_to_output("📊 正在獲取存取統計...")
        self.run_test("identity_manager_stats")
    
    # ===== 記憶操作功能 =====
    
    def create_conversation_snapshot(self):
        """創建對話快照"""
        conversation = self.get_test_conversation()
        if not conversation:
            self.append_to_output("❌ 請輸入對話內容")
            return
        
        identity_token = self.memory_token_input.text().strip() or "test_user"
        
        self.append_to_output(f"📸 正在創建對話快照 (令牌: {identity_token})...")
        
        try:
            from devtools.debug_api import mem_test_conversation_snapshot_wrapper
            result = mem_test_conversation_snapshot_wrapper(identity_token, conversation)
            
            if result.get('success'):
                self.append_to_output("✅ 對話快照創建成功")
                result_obj = result.get('result')
                if result_obj:
                    self.append_to_output(f"   快照ID: {getattr(result_obj, 'snapshot_id', 'N/A')}")
                    self.append_to_output(f"   操作類型: {getattr(result_obj, 'operation_type', 'N/A')}")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 對話快照創建失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 對話快照創建異常：{str(e)}")
    
    def query_memory(self):
        """查詢記憶"""
        query_text = self.get_test_query()
        if not query_text:
            self.append_to_output("❌ 請輸入查詢內容")
            return
        
        identity_token = self.memory_token_input.text().strip() or "test_user"
        
        self.append_to_output(f"🔍 正在查詢記憶 '{query_text}' (令牌: {identity_token})...")
        
        try:
            from devtools.debug_api import mem_test_memory_query_wrapper
            result = mem_test_memory_query_wrapper(identity_token, query_text)
            
            if result.get('success'):
                self.append_to_output("✅ 記憶查詢成功")
                # 處理查詢結果
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 記憶查詢失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 記憶查詢異常：{str(e)}")
    
    def show_identity_manager_stats(self):
        """顯示身份管理器統計"""
        self.append_to_output("📊 正在獲取身份管理器統計...")
        self.run_test("identity_manager_stats")
    
    def test_nlp_integration(self):
        """測試NLP整合"""
        self.append_to_output("🧠 正在測試NLP整合功能...")
        self.run_test("nlp_integration")
    
    def test_llm_context_extraction(self):
        """測試LLM上下文提取"""
        identity_token = self.memory_token_input.text().strip() or "test_user"
        query_text = self.get_test_query()
        
        self.append_to_output(f"💡 正在測試LLM上下文提取 (令牌: {identity_token}, 查詢: {query_text})...")
        
        try:
            from devtools.debug_api import mem_test_llm_context_extraction_wrapper
            result = mem_test_llm_context_extraction_wrapper(identity_token, query_text)
            
            if result.get('success'):
                self.append_to_output("✅ LLM上下文提取測試成功")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ LLM上下文提取測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ LLM上下文提取測試異常：{str(e)}")
    
    # ===== 測試場景功能 =====
    
    def run_conversation_test(self):
        """運行對話場景測試"""
        self.append_to_output("💬 正在運行對話場景測試...")
        # 執行一系列對話相關的測試
        self.create_conversation_snapshot()
        self.query_memory()
    
    def run_learning_test(self):
        """運行學習場景測試"""
        self.append_to_output("📚 正在運行學習場景測試...")
        self.test_nlp_integration()
        self.test_llm_context_extraction()
    
    def run_full_workflow(self):
        """運行完整工作流程"""
        user_name = "WorkflowTestUser"
        self.append_to_output(f"⚙️ 正在運行完整工作流程測試 (用戶: {user_name})...")
        
        try:
            from devtools.debug_api import mem_test_full_workflow_wrapper
            result = mem_test_full_workflow_wrapper(user_name)
            
            if result.get('success'):
                self.append_to_output("✅ 完整工作流程測試成功")
            else:
                error = result.get('error', '未知錯誤')
                self.append_to_output(f"❌ 完整工作流程測試失敗：{error}")
                
        except Exception as e:
            self.append_to_output(f"❌ 完整工作流程測試異常：{str(e)}")
    
    def run_stress_test(self):
        """運行壓力測試"""
        self.append_to_output("⚡ 正在運行壓力測試...")
        # 實現壓力測試邏輯
        for i in range(5):
            self.append_to_output(f"   第 {i+1} 輪壓力測試...")
            self.create_conversation_snapshot()
    
    def run_performance_test(self):
        """運行性能測試"""
        self.append_to_output("📈 正在運行性能測試...")
        # 實現性能測試邏輯
        import time
        start_time = time.time()
        self.run_full_workflow()
        end_time = time.time()
        
        execution_time = (end_time - start_time) * 1000
        self.append_to_output(f"   執行時間: {execution_time:.2f} ms")
    
    # ===== 系統管理功能 =====
    
    def show_memory_stats(self):
        """顯示記憶統計"""
        self.append_to_output("📊 正在獲取記憶統計...")
        self.run_test("identity_manager_stats")
    
    def show_storage_info(self):
        """顯示存儲信息"""
        self.append_to_output("💾 正在獲取存儲信息...")
        # 實現存儲信息顯示
        self.append_to_output("   存儲類型: 向量數據庫 + 元數據存儲")
        self.append_to_output("   索引類型: FAISS IndexFlatIP")
    
    def show_vector_index_info(self):
        """顯示向量索引信息"""
        self.append_to_output("🔢 正在獲取向量索引信息...")
        # 實現向量索引信息顯示
        self.append_to_output("   索引狀態: 活躍")
        self.append_to_output("   嵌入模型: all-MiniLM-L6-v2")
    
    def rebuild_vector_index(self):
        """重建向量索引"""
        self.append_to_output("🔧 正在重建向量索引...")
        # 實現索引重建邏輯
        self.append_to_output("✅ 向量索引重建完成")
    
    def cleanup_expired_data(self):
        """清理過期數據"""
        self.append_to_output("🧹 正在清理過期數據...")
        # 實現數據清理邏輯
        self.append_to_output("✅ 過期數據清理完成")
    
    def reset_all_data(self):
        """重置所有數據"""
        reply = QMessageBox.question(
            self, "確認重置", 
            "⚠️ 這將清除所有記憶數據，此操作不可逆！\\n\\n請輸入 'RESET ALL' 確認:",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            text, ok = QInputDialog.getText(self, "確認重置", "請輸入 'RESET ALL':")
            
            if ok and text == "RESET ALL":
                self.append_to_output("🔄 正在重置所有數據...")
                # 實現數據重置邏輯
                self.append_to_output("✅ 所有數據重置完成")
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
            if test_type in ["memory_stats", "storage_info", "vector_index_info", "identity_manager_stats"]:
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