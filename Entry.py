"""
Echo Memory System - 互動式 CLI 入口

提供類似 U.E.P Core Debugger 的互動式測試介面。

使用方式:
    python Entry.py
"""

import sys
from pathlib import Path

# 添加專案路徑
sys.path.insert(0, str(Path(__file__).parent))

import logging

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from llm_integration.llm_client import LLMClient

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("entry")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 彩色輸出工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class Colors:
    """ANSI 顏色碼"""

    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def colored(text: str, color: str) -> str:
    """返回帶顏色的文字"""
    return f"{color}{text}{Colors.ENDC}"


def print_header(text: str):
    """打印標題"""
    print(f"\n{colored('=' * 80, Colors.HEADER)}")
    print(colored(text.center(80), Colors.HEADER))
    print(f"{colored('=' * 80, Colors.HEADER)}\n")


def print_section(text: str):
    """打印章節標題"""
    print(f"\n{colored(text, Colors.OKCYAN)}")
    print(colored("-" * len(text), Colors.OKCYAN))


def print_success(text: str):
    """打印成功訊息"""
    print(colored(f"✓ {text}", Colors.OKGREEN))


def print_warning(text: str):
    """打印警告訊息"""
    print(colored(f"⚠ {text}", Colors.WARNING))


def print_error(text: str):
    """打印錯誤訊息"""
    print(colored(f"✗ {text}", Colors.FAIL))


def print_info(text: str):
    """打印資訊"""
    print(colored(f"ℹ {text}", Colors.OKBLUE))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心類別
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EchoMemoryCLI:
    """Echo Memory System 互動式 CLI"""

    def __init__(self):
        self.config = get_config()
        self.memory_engine: MemoryEngine | None = None
        self.llm_client: LLMClient | None = None
        self.session_id = "cli_session"
        self.conversation_count = 0

    def initialize(self):
        """初始化記憶引擎"""
        print_section("初始化 Echo Memory System")

        try:
            # 初始化記憶引擎（不需要 LLM API key）
            self.memory_engine = MemoryEngine(
                storage_path=self.config.memory_storage_path,
                embedding_model=self.config.embedding_model,
                device=self.config.embedding_device,
                query_prefix=getattr(self.config, 'embedding_query_prefix', ''),
                max_episodes=self.config.memory_max_episodes,
            )

            print_success("記憶引擎初始化成功")
            print_info(f"存儲路徑: {self.config.memory_storage_path}")
            print_info(f"嵌入模型: {self.config.embedding_model}")

            # 顯示統計資訊
            stats = self.memory_engine.get_stats()
            print_info(
                f"已載入 {stats['total_episodes']} 個情節, "
                f"{stats['total_concepts']} 個概念"
            )

            # 初始化 LLM Client（用於對話模式，非必要）
            is_llm_valid, missing = self.config.validate_llm()
            if is_llm_valid:
                try:
                    self.llm_client = LLMClient()
                    print_success("LLM 客戶端初始化成功")
                except Exception as llm_error:
                    self.llm_client = None
                    print_warning(f"LLM 客戶端初始化失敗: {llm_error}")
                    print_info("對話模式將無法使用，但記憶功能正常")
            else:
                self.llm_client = None
                print_warning(f"LLM API 未設定（缺少: {', '.join(missing)}）")
                print_info("對話模式將無法使用，但記憶的存儲/檢索/統計功能正常")

            return True

        except Exception as e:
            print_error(f"初始化失敗: {e}")
            logger.exception("初始化記憶引擎時發生錯誤")
            return False

    def show_menu(self):
        """顯示主選單"""
        print_section("主選單")

        llm_available = self.llm_client is not None
        chat_color = Colors.OKGREEN if llm_available else Colors.ENDC
        chat_icon = "✓" if llm_available else "✗"
        chat_desc = "對話模式（存儲+檢索）" if llm_available else "對話模式（LLM 未設定，無法使用）"

        menu_items = [
            ("store", "存儲對話", Colors.OKGREEN, "✓"),
            ("retrieve", "檢索記憶", Colors.OKGREEN, "✓"),
            ("chat", chat_desc, chat_color, chat_icon),
            ("stats", "查看統計", Colors.OKGREEN, "✓"),
            ("list", "列出情節（支援分頁/session 篩選）", Colors.OKGREEN, "✓"),
            ("delete", "刪除情節或 session", Colors.WARNING, "✗"),
            ("export", "匯出記憶資料", Colors.OKCYAN, "↓"),
            ("config", "顯示配置", Colors.OKCYAN, "ℹ"),
            ("dream", "執行 Dream Engine", Colors.WARNING, "⚠"),
            ("clear", "清空記憶", Colors.FAIL, "✗"),
            ("help", "顯示幫助", Colors.OKBLUE, "?"),
            ("exit", "退出", Colors.ENDC, "←"),
        ]

        for cmd, desc, color, icon in menu_items:
            print(f"  {colored(icon, color)} {colored(cmd.ljust(10), color)} - {desc}")

        print()

    def cmd_store(self):
        """存儲對話命令"""
        print_section("存儲對話")

        user_input = input(colored("用戶輸入: ", Colors.OKCYAN)).strip()
        if not user_input:
            print_warning("用戶輸入不能為空")
            return

        agent_response = input(colored("Agent 回應: ", Colors.OKCYAN)).strip()
        if not agent_response:
            print_warning("Agent 回應不能為空")
            return

        # 詢問顯著性分數
        salience_input = input(
            colored("顯著性分數 (0-1, 默認 0.5): ", Colors.OKCYAN)
        ).strip()
        try:
            salience = float(salience_input) if salience_input else 0.5
            salience = max(0.0, min(1.0, salience))
        except ValueError:
            print_warning("無效的分數，使用默認值 0.5")
            salience = 0.5

        try:
            self.conversation_count += 1
            episode_id = self.memory_engine.store_episode(
                user_input=user_input,
                agent_response=agent_response,
                session_id=self.session_id,
                content_summary=user_input[:50] + "...",
                salience=salience,
            )

            print_success(f"已存儲情節: {episode_id}")
            print_info(f"會話計數: {self.conversation_count}")

        except Exception as e:
            print_error(f"存儲失敗: {e}")
            logger.exception("存儲情節時發生錯誤")

    def cmd_retrieve(self):
        """檢索記憶命令"""
        print_section("檢索記憶")

        query = input(colored("查詢內容: ", Colors.OKCYAN)).strip()
        if not query:
            print_warning("查詢內容不能為空")
            return

        # 詢問神經化學參數（可選）
        dopamine_input = input(
            colored("多巴胺濃度 (0-1, 默認 0.5): ", Colors.OKCYAN)
        ).strip()
        serotonin_input = input(
            colored("血清素濃度 (0-1, 默認 0.5): ", Colors.OKCYAN)
        ).strip()

        try:
            dopamine = float(dopamine_input) if dopamine_input else 0.5
            serotonin = float(serotonin_input) if serotonin_input else 0.5
        except ValueError:
            print_warning("無效的參數，使用默認值")
            dopamine = 0.5
            serotonin = 0.5

        try:
            context = self.memory_engine.retrieve(
                query=query,
                dopamine=dopamine,
                serotonin=serotonin,
                top_k=5,
                min_similarity=self.config.retrieval_min_similarity,
            )

            print_section("檢索結果")
            print(context.to_text())

            if context.is_empty():
                print_warning("未找到相關記憶")
            else:
                print_success(f"找到 {len(context.episodes)} 個相關情節")

        except Exception as e:
            print_error(f"檢索失敗: {e}")
            logger.exception("檢索記憶時發生錯誤")

    def cmd_chat(self):
        """對話模式（組合存儲+檢索+LLM）"""
        print_section("對話模式")

        # 檢查 LLM Client 是否可用
        if self.llm_client is None:
            print_error("LLM 客戶端未初始化，無法使用對話模式")
            print_info("請檢查 GOOGLE_API_KEY 配置")
            return

        print_info("輸入 'exit' 退出對話模式\n")
        print_info(f"系統提示詞: {self.config.system_prompt[:60]}...\n")

        while True:
            user_input = input(colored("你: ", Colors.OKGREEN)).strip()

            if user_input.lower() in ["exit", "quit", "q"]:
                print_info("退出對話模式")
                break

            if not user_input:
                continue

            try:
                # 1. 檢索相關記憶
                context = self.memory_engine.retrieve(
                    query=user_input,
                    dopamine=0.5,
                    serotonin=0.5,
                    top_k=3,
                    min_similarity=self.config.retrieval_min_similarity,
                )

                if not context.is_empty():
                    print_section("相關記憶")
                    print(colored(context.to_text(), Colors.WARNING))

                # 2. 調用 LLM 生成回應
                print(colored("\n⏳ 生成回應中...", Colors.OKCYAN))

                prompt = self.llm_client.build_prompt(
                    user_input=user_input,
                    memory_context=context.to_text() if not context.is_empty() else "",
                    system_instruction=self.config.system_prompt,
                )

                agent_response = self.llm_client.generate(
                    prompt=prompt,
                    temperature=0.7,
                    max_tokens=1024,
                )

                print(colored("\nAgent: ", Colors.OKCYAN) + agent_response)

                # 3. 存儲本次對話
                self.conversation_count += 1
                episode_id = self.memory_engine.store_episode(
                    user_input=user_input,
                    agent_response=agent_response,
                    session_id=self.session_id,
                    salience=0.6,  # 對話模式默認中等重要性
                )

                print(colored(f"\n✓ 已存儲 (ID: {episode_id[:8]}...)\n", Colors.OKGREEN))

            except Exception as e:
                print_error(f"處理失敗: {e}")
                logger.exception("對話模式錯誤")

    def cmd_stats(self):
        """查看統計命令"""
        print_section("記憶統計")

        try:
            stats = self.memory_engine.get_stats()

            print(f"  總情節數:     {colored(str(stats['total_episodes']), Colors.OKGREEN)}")
            print(f"  總概念數:     {colored(str(stats['total_concepts']), Colors.OKGREEN)}")
            print(f"  工作記憶緩存: {colored(str(stats['working_cache_size']), Colors.OKCYAN)}")
            print(f"  當前會話計數: {colored(str(self.conversation_count), Colors.OKCYAN)}")

        except Exception as e:
            print_error(f"獲取統計失敗: {e}")

    def cmd_list(self):
        """列出情節（支援分頁和 session 篩選）"""
        print_section("列出情節")

        # 詢問篩選條件
        session_input = input(
            colored("Session ID 篩選（留空表示全部）: ", Colors.OKCYAN)
        ).strip() or None

        sort_input = input(
            colored("排序方式 (timestamp/salience，預設 timestamp): ", Colors.OKCYAN)
        ).strip()
        sort_by = sort_input if sort_input in ("timestamp", "salience") else "timestamp"

        page_input = input(colored("頁碼（從 1 開始，預設 1）: ", Colors.OKCYAN)).strip()
        try:
            page = max(1, int(page_input)) if page_input else 1
        except ValueError:
            page = 1

        limit = 10
        offset = (page - 1) * limit

        try:
            total = self.memory_engine.episodic.count()
            episodes = self.memory_engine.episodic.list_episodes(
                session_id=session_input,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                ascending=False,
            )

            if not episodes:
                print_warning("沒有符合條件的情節記憶")
                return

            filter_desc = f"session={session_input}" if session_input else "全部"
            print(f"\n總計 {total} 個情節，篩選條件: {filter_desc}，排序: {sort_by}（第 {page} 頁）\n")

            for i, ep in enumerate(episodes, offset + 1):
                salience_color = (
                    Colors.OKGREEN
                    if ep.salience_score >= 0.7
                    else Colors.WARNING if ep.salience_score >= 0.4 else Colors.ENDC
                )

                print(
                    f"{i}. {colored(ep.id[:16], Colors.OKCYAN)} "
                    f"[顯著性: {colored(f'{ep.salience_score:.2f}', salience_color)}] "
                    f"[Session: {colored(ep.session_id[:12], Colors.OKBLUE)}] "
                    f"[{ep.timestamp.strftime('%Y-%m-%d %H:%M')}]"
                )
                print(f"   用戶: {ep.user_input[:60]}...")
                print(f"   Agent: {ep.agent_response[:60]}...\n")

            if offset + len(episodes) < total:
                remaining = total - offset - len(episodes)
                print_info(f"還有 {remaining} 個情節，使用下一頁查看更多")

        except Exception as e:
            print_error(f"列出情節失敗: {e}")

    def cmd_delete(self):
        """刪除情節或整個 session"""
        print_section("刪除記憶")

        print("  1. 刪除單一情節（依 episode ID）")
        print("  2. 刪除整個 session（依 session ID）")
        print()

        choice = input(colored("選擇操作 (1/2): ", Colors.WARNING)).strip()

        if choice == "1":
            self._delete_episode_interactive()
        elif choice == "2":
            self._delete_session_interactive()
        else:
            print_warning("無效的選擇")

    def _delete_episode_interactive(self):
        """互動式刪除單一情節"""
        episode_id = input(colored("輸入 Episode ID（支援前綴，最少 8 個字元）: ", Colors.OKCYAN)).strip()

        if len(episode_id) < 8:
            print_warning("Episode ID 太短，至少需要 8 個字元")
            return

        # 支援前綴搜尋
        matched = [
            ep for ep_id, ep in self.memory_engine.episodic._episodes.items()
            if ep_id.startswith(episode_id)
        ]

        if not matched:
            print_error(f"找不到符合的情節: {episode_id}")
            return

        if len(matched) > 1:
            print_warning(f"找到 {len(matched)} 個符合的情節，請提供更完整的 ID：")
            for ep in matched:
                print(f"  {colored(ep.id, Colors.OKCYAN)} | {ep.user_input[:40]}...")
            return

        ep = matched[0]
        print()
        print_warning("即將刪除以下情節：")
        print(f"  ID:      {colored(ep.id, Colors.OKCYAN)}")
        print(f"  Session: {ep.session_id}")
        print(f"  時間:    {ep.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  用戶:    {ep.user_input[:60]}...")
        print(f"  顯著性:  {ep.salience_score:.2f}")
        print()

        confirm = input(colored("確定刪除? (yes/no): ", Colors.FAIL)).strip().lower()
        if confirm != "yes":
            print_info("已取消")
            return

        try:
            success = self.memory_engine.episodic.delete_episode(ep.id)
            if success:
                print_success(f"已刪除情節: {ep.id}")
            else:
                print_error("刪除失敗（情節可能已不存在）")
        except Exception as e:
            print_error(f"刪除失敗: {e}")
            logger.exception("刪除情節錯誤")

    def _delete_session_interactive(self):
        """互動式刪除整個 session"""
        session_id = input(colored("輸入 Session ID: ", Colors.OKCYAN)).strip()

        if not session_id:
            print_warning("Session ID 不能為空")
            return

        # 預覽受影響的情節
        preview = self.memory_engine.episodic.list_episodes(
            session_id=session_id,
            limit=200,
            offset=0,
            sort_by="timestamp",
            ascending=True,
        )

        if not preview:
            print_warning(f"Session '{session_id}' 沒有任何情節")
            return

        print()
        print_warning(f"即將刪除 Session '{session_id}' 的 {len(preview)} 個情節：")
        for ep in preview[:5]:
            print(
                f"  {colored(ep.id[:16], Colors.OKCYAN)} "
                f"[{ep.timestamp.strftime('%Y-%m-%d %H:%M')}] "
                f"{ep.user_input[:40]}..."
            )
        if len(preview) > 5:
            print_info(f"  ... 還有 {len(preview) - 5} 個情節")
        print()

        confirm1 = input(colored("確定要刪除整個 session 嗎? (yes/no): ", Colors.FAIL)).strip().lower()
        if confirm1 != "yes":
            print_info("已取消")
            return

        confirm2 = input(colored("再次確認，此操作不可撤銷 (輸入 session ID 確認): ", Colors.FAIL)).strip()
        if confirm2 != session_id:
            print_info("確認失敗，已取消")
            return

        try:
            count = self.memory_engine.episodic.delete_session(session_id)
            print_success(f"已刪除 session '{session_id}' 的 {count} 個情節")
        except Exception as e:
            print_error(f"刪除失敗: {e}")
            logger.exception("刪除 session 錯誤")

    def cmd_export(self):
        """匯出記憶資料"""
        print_section("匯出記憶")

        session_input = input(
            colored("Session ID 篩選（留空表示匯出全部）: ", Colors.OKCYAN)
        ).strip() or None

        format_input = input(
            colored("匯出格式 (json/csv，預設 json): ", Colors.OKCYAN)
        ).strip().lower()
        fmt = format_input if format_input in ("json", "csv") else "json"

        output_path = input(
            colored("輸出檔案路徑（留空則輸出到螢幕）: ", Colors.OKCYAN)
        ).strip() or None

        try:
            content = self.memory_engine.episodic.export_episodes(
                session_id=session_input,
                format=fmt,
            )

            if output_path:
                path = Path(output_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                print_success(f"已匯出至: {path.resolve()}")
                line_count = content.count("\n")
                print_info(f"內容大小: {len(content):,} 字元，約 {line_count} 行")
            else:
                print_section(f"匯出結果（{fmt.upper()}）")
                # 螢幕輸出限制 50 行，避免刷屏
                lines = content.splitlines()
                preview_lines = lines[:50]
                print("\n".join(preview_lines))
                if len(lines) > 50:
                    print_warning(f"\n... 共 {len(lines)} 行，只顯示前 50 行")
                    print_info("請指定輸出檔案路徑以保存完整內容")

        except ValueError as e:
            print_error(str(e))
        except Exception as e:
            print_error(f"匯出失敗: {e}")
            logger.exception("匯出記憶錯誤")

    def cmd_config(self):
        """顯示配置"""
        print_section("當前配置")
        print(self.config.summary())

    def cmd_dream(self):
        """執行 Dream Engine"""
        print_section("Dream Engine")
        print_info("簡化版離線記憶鞏固（已實作 ✅）")
        print_info("功能: 圖譜修剪 + 邊權重更新\n")

        confirm = input(colored("確定要執行嗎? (y/n): ", Colors.WARNING)).lower()
        if confirm != "y":
            print_info("已取消")
            return

        try:
            print(colored("\n⏳ Dream Engine 執行中...\n", Colors.OKCYAN))

            report_dict = self.memory_engine.dream(triggered_by="manual")

            if report_dict.get("error"):
                print_error(f"執行失敗: {report_dict['error']}")
                return

            # 顯示詳細報告
            print_success("Dream 完成！")
            print(f"\n  觸發方式: {colored(report_dict['triggered_by'], Colors.OKCYAN)}")
            duration_str = f"{report_dict['duration_seconds']:.1f}s"
            print(f"  執行時長: {colored(duration_str, Colors.OKGREEN)}")
            print(f"  邊權重更新: {colored(str(report_dict['edges_updated']), Colors.OKGREEN)} 條")
            print(f"  弱邊移除: {colored(str(report_dict['edges_pruned']), Colors.OKGREEN)} 條")

            # 顯示圖結構統計
            stats = self.memory_engine.episodic.graph.get_stats()
            print("\n  圖結構統計:")
            print(f"    節點數: {stats['nodes']}")
            print(f"    邊數: {stats['edges']}")
            print(f"    平均權重: {stats['avg_edge_weight']:.3f}")

        except Exception as e:
            print_error(f"Dream 執行失敗: {e}")
            logger.exception("Dream Engine 錯誤")

    def cmd_clear(self):
        """清空記憶"""
        print_section("清空記憶")
        print_error("⚠️ 警告：此操作將刪除所有記憶，無法恢復！")

        confirm1 = input(colored("確定要清空所有記憶嗎? (yes/no): ", Colors.FAIL)).lower()
        if confirm1 != "yes":
            print_info("已取消")
            return

        confirm2 = input(colored("再次確認 (輸入 'DELETE'): ", Colors.FAIL))
        if confirm2 != "DELETE":
            print_info("已取消")
            return

        try:
            self.memory_engine.clear_all()
            self.conversation_count = 0
            print_success("已清空所有記憶")

        except Exception as e:
            print_error(f"清空失敗: {e}")

    def cmd_help(self):
        """顯示幫助"""
        print_section("命令說明")

        help_text = """
  store      - 手動存儲一次對話（會詢問用戶輸入、Agent 回應、顯著性）
  retrieve   - 檢索相關記憶（會詢問查詢內容和神經化學參數）
  chat       - 進入對話模式（自動檢索記憶 + LLM 生成回應 + 存儲對話）
  stats      - 顯示記憶系統統計資訊
  list       - 列出情節（支援分頁、session 篩選、排序）
  delete     - 刪除單一情節（依 ID）或整個 session（需二次確認）
  export     - 匯出記憶資料為 JSON 或 CSV（支援 session 篩選）
  config     - 顯示當前系統配置
  dream      - 手動觸發 Dream Engine（Phase 4 功能）
  clear      - 清空所有記憶（需要二次確認）
  help       - 顯示此幫助訊息
  exit       - 退出 CLI

對話模式 (chat) 流程：
  1. 輸入用戶訊息
  2. 混合檢索相關記憶（向量檢索 + 圖譜展開）
     - NumPy 餘弦相似度檢索定位高相似度種子
     - NetworkX 圖譜沿 PRECEDES 邊多跳展開
     - 動態權重過濾（時間衰減 + 頻率 + 顯著性）
  3. LLM 根據記憶脈絡 + 系統提示詞生成回應
  4. 自動存儲本次對話並更新圖結構

神經化學參數說明：
  - 多巴胺 (dopamine): 影響檢索的探索廣度，值越高越傾向探索新記憶
  - 血清素 (serotonin): 影響檢索的謹慎程度，值越高越嚴格篩選結果

顯著性分數 (salience):
  - 0.0-0.25: 淡薄記憶（不太重要）
  - 0.25-0.5: 一般記憶
  - 0.5-0.75: 重要記憶
  - 0.75-1.0: 核心記憶（非常重要）

系統提示詞配置：
  - 在 .env 文件中設定 SYSTEM_PROMPT
  - 用於指導 LLM 如何回應（例如角色設定、語氣風格）
"""
        print(help_text)

    def run(self):
        """運行主循環"""
        print_header("Echo Memory System - 互動式 CLI")

        # 初始化
        if not self.initialize():
            print_error("初始化失敗，無法繼續")
            return

        print_success("系統就緒！輸入 'help' 查看可用命令\n")

        # 主循環
        while True:
            try:
                self.show_menu()
                command = input(colored("echo> ", Colors.BOLD)).strip().lower()

                if not command:
                    continue

                match command:
                    case "store":
                        self.cmd_store()
                    case "retrieve":
                        self.cmd_retrieve()
                    case "chat":
                        self.cmd_chat()
                    case "stats":
                        self.cmd_stats()
                    case "list":
                        self.cmd_list()
                    case "delete":
                        self.cmd_delete()
                    case "export":
                        self.cmd_export()
                    case "config":
                        self.cmd_config()
                    case "dream":
                        self.cmd_dream()
                    case "clear":
                        self.cmd_clear()
                    case "help" | "?":
                        self.cmd_help()
                    case "exit" | "quit" | "q":
                        print_info("感謝使用 Echo Memory System！")
                        break
                    case _:
                        print_error(f"未知命令: {command}")
                        print_info("輸入 'help' 查看可用命令")

            except KeyboardInterrupt:
                print("\n")
                print_warning("檢測到 Ctrl+C，輸入 'exit' 退出")
            except Exception as e:
                print_error(f"發生錯誤: {e}")
                logger.exception("主循環錯誤")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主程式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def main():
    """主程式入口"""
    cli = EchoMemoryCLI()
    cli.run()


if __name__ == "__main__":
    main()
