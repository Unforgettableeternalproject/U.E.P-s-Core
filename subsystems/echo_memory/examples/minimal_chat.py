"""
最小對話示例 - MVP 測試

展示 Echo Memory System 的基本使用流程。
"""

import logging
import sys
from pathlib import Path

# 添加專案路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

from echo_memory import MemoryEngine
from echo_memory.config import get_config

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("example")


def main():
    """最小對話循環示例"""

    # 0. 載入配置
    config = get_config()
    logger.info("=== 配置摘要 ===")
    print(config.summary())

    # 驗證配置
    is_valid, missing = config.validate()
    if not is_valid:
        logger.error(f"配置驗證失敗，缺少: {', '.join(missing)}")
        logger.error("請先設定 .env 文件（參考 .env.example）")
        return

    # 1. 初始化記憶引擎
    logger.info("=== 初始化 Echo Memory System ===")
    memory = MemoryEngine(
        storage_path=config.memory_storage_path,
        embedding_model=config.embedding_model,
        device=config.embedding_device,
    )

    # 2. 模擬對話並存儲
    logger.info("\n=== 模擬對話 ===")
    conversations = [
        {
            "user": "今天天氣如何？",
            "agent": "台北今天晴朗，氣溫 25°C。",
            "salience": 0.3,
        },
        {
            "user": "我喜歡晴天",
            "agent": "了解！我會記住你喜歡晴朗的天氣。",
            "salience": 0.7,
        },
        {
            "user": "推薦我一部電影",
            "agent": "考慮到你喜歡晴朗天氣，推薦《陽光普照》。",
            "salience": 0.5,
        },
    ]

    for i, conv in enumerate(conversations, 1):
        episode_id = memory.store_episode(
            user_input=conv["user"],
            agent_response=conv["agent"],
            session_id="demo_session",
            content_summary=conv["user"],
            salience=conv["salience"],
        )
        logger.info(f"存儲對話 {i}: {episode_id}")

    # 3. 檢索相關記憶
    logger.info("\n=== 檢索記憶 ===")
    query = "我喜歡什麼天氣？"
    logger.info(f"查詢: {query}")

    context = memory.retrieve(
        query=query,
        dopamine=0.5,
        serotonin=0.5,
        top_k=3,
    )

    logger.info(f"\n檢索結果:\n{context.to_text()}")

    # 4. 顯示統計資訊
    stats = memory.get_stats()
    logger.info("\n=== 記憶統計 ===")
    logger.info(f"總情節數: {stats['total_episodes']}")
    logger.info(f"總概念數: {stats['total_concepts']}")
    logger.info(f"工作記憶緩存: {stats['working_cache_size']}")

    # 5. 模擬使用檢索結果構建 LLM Prompt
    logger.info("\n=== 模擬 LLM Prompt 構建 ===")
    prompt = f"""
你是一個有記憶的 AI 助手。

【相關記憶】
{context.to_text()}

【當前問題】
用戶問: {query}

請基於記憶回答問題。
"""
    logger.info(prompt)


if __name__ == "__main__":
    main()
