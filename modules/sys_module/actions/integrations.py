import feedparser
from numpy import info
import requests
import datetime
import subprocess
import psutil
import ast
from pathlib import Path
from utils.debug_helper import info_log, error_log
import os
import re

def news_summary(
    source: str = "google_news_tw",
    max_items: int = 10,
    rss_url: str = "",
    use_full_crawler: bool = False
) -> dict:
    """擷取並整理新聞摘要
    
    Args:
        source: 新聞來源 ("google_news_tw" 爬取 Google News 台灣首頁, "rss" 使用 RSS feed)
        max_items: 最大新聞數量
        rss_url: RSS feed URL (當 source="rss" 時使用)
        use_full_crawler: 是否使用 Selenium 爬取完整文章內容（僅 RSS 模式支援）
        
    Returns:
        dict: {"status": "ok"|"error", "titles": [標題列表], "count": 數量, "source": 來源}
    """
    try:
        if source == "google_news_tw":
            # Google News 台灣首頁模式
            info_log("[INT] 使用 Selenium 爬取 Google News 台灣首頁")
            return _crawl_google_news_homepage(max_items)
        
        elif source == "rss":
            # RSS 模式
            if not rss_url:
                error_log("[INT] RSS 模式需要提供 rss_url 參數")
                return {
                    "status": "error",
                    "message": "RSS 模式需要提供 rss_url",
                    "titles": []
                }
            
            d = feedparser.parse(rss_url)
            entries = d.entries[:max_items]
            titles = [e.title for e in entries]
            
            info_log(f"[INT] 擷取 {len(titles)} 則新聞 (RSS 模式)")
            return {
                "status": "ok",
                "titles": titles,
                "count": len(titles),
                "source": "rss"
            }
        
        else:
            error_log(f"[INT] 未知的新聞來源：{source}")
            return {
                "status": "error",
                "message": f"未知的新聞來源：{source}",
                "titles": []
            }
            
    except Exception as e:
        error_log(f"[INT] 擷取新聞失敗: {e}")
        return {
            "status": "error",
            "message": str(e),
            "titles": []
        }

def _crawl_google_news_homepage(max_items: int = 10) -> dict:
    """爬取 Google News 台灣首頁標題（純標題模式，適合給 LLM 總結）"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from bs4 import BeautifulSoup
        import time
        
        # 設定 Chrome 為 headless 模式
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(options=chrome_options)
        
        try:
            # 載入 Google News 台灣首頁
            url = "https://news.google.com/home?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            driver.get(url)
            time.sleep(3)  # 等待頁面載入
            
            # 解析 HTML
            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')
            
            # 提取新聞標題
            news_titles = []
            articles = soup.find_all('article')
            
            for article in articles[:max_items]:
                try:
                    # 嘗試多種標題選擇器
                    title_elem = (
                        article.find('h3') or
                        article.find('h4') or
                        article.find(class_='JtKRv') or
                        article.find(class_='gPFEn') or
                        article.find('a')
                    )
                    
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        if title and len(title) > 5:
                            news_titles.append(title)
                except Exception:
                    continue
            
            driver.quit()
            
            if news_titles:
                info_log(f"[INT] Google News 爬取成功，共 {len(news_titles)} 則標題")
                return {
                    "status": "ok",
                    "titles": news_titles,
                    "count": len(news_titles),
                    "source": "google_news_tw"
                }
            else:
                error_log("[INT] Google News 爬取失敗，未找到新聞標題")
                return {
                    "status": "error",
                    "message": "未找到新聞標題",
                    "titles": []
                }
        
        finally:
            try:
                driver.quit()
            except:
                pass
    
    except ImportError:
        error_log("[INT] 缺少 Selenium 或 BeautifulSoup")
        return {
            "status": "error",
            "message": "請安裝 selenium 和 beautifulsoup4",
            "titles": []
        }
    except Exception as e:
        error_log(f"[INT] Google News 爬取失敗：{e}")
        return {
            "status": "error",
            "message": str(e),
            "titles": []
        }


def _crawl_full_articles(entries):
    """使用 Selenium + BeautifulSoup 爬取完整文章內容"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from bs4 import BeautifulSoup
        
        # 設定 Chrome 為 headless 模式
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        driver = webdriver.Chrome(options=chrome_options)
        results = []
        
        for entry in entries:
            try:
                title = entry.title
                link = entry.link
                
                # 使用 Selenium 載入頁面
                driver.get(link)
                driver.implicitly_wait(3)
                
                # 取得頁面 HTML
                html = driver.page_source
                soup = BeautifulSoup(html, 'html.parser')
                
                # 嘗試提取文章內容（常見的文章標籤）
                article_content = ""
                
                # 嘗試多種選擇器
                article_tags = soup.find_all(['article', 'div'], class_=re.compile(r'(article|content|post|entry|story)'))
                if article_tags:
                    # 取第一個匹配的標籤
                    article_content = article_tags[0].get_text(strip=True, separator='\n')
                else:
                    # 備用方案：取 <p> 標籤
                    paragraphs = soup.find_all('p')
                    article_content = '\n'.join([p.get_text(strip=True) for p in paragraphs[:5]])  # 只取前 5 段
                
                # 限制內容長度
                if len(article_content) > 500:
                    article_content = article_content[:500] + "..."
                
                results.append(f"【{title}】\n{article_content}\n來源：{link}\n")
                info_log(f"[INT] 已爬取：{title}")
                
            except Exception as article_error:
                error_log(f"[INT] 爬取文章失敗 ({entry.title}): {article_error}")
                # 失敗時至少保留標題和連結
                results.append(f"- {entry.title}: {entry.link}\n")
        
        driver.quit()
        info_log(f"[INT] Selenium 爬取完成，共 {len(results)} 則")
        return "\n".join(results)
        
    except ImportError as import_error:
        error_log(f"[INT] 缺少必要套件: {import_error}")
        error_log("[INT] 請安裝: pip install selenium beautifulsoup4")
        # 降級為簡易模式
        summaries = [f"- {e.title}: {e.link}" for e in entries]
        return "\n".join(summaries)
    except Exception as e:
        error_log(f"[INT] Selenium 爬取失敗: {e}")
        # 降級為簡易模式
        summaries = [f"- {e.title}: {e.link}" for e in entries]
        return "\n".join(summaries)

def get_weather(location: str):
    """回傳天氣資訊（使用 wttr.in API，無需 API key）
    
    Returns:
        dict: 結構化天氣資訊，包含：
            - location: 地點名稱
            - condition: 天氣狀況
            - temperature: 溫度
            - wind: 風速風向
            - humidity: 濕度
            - raw_text: 原始文字（用於記錄）
    """
    try:
        # 使用 wttr.in 的簡潔格式 API
        # 格式：%l=地點 %C=狀況 %t=溫度 %w=風速 %h=濕度
        url = f"https://wttr.in/{location}?format=%l:+%C+%t+%w+%h"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        weather_text = r.text.strip()
        
        # 解析天氣資訊
        # 格式範例：Taipei: Light rain +23°C ←25km/h 94%
        try:
            parts = weather_text.split(":", 1)
            if len(parts) == 2:
                location_name = parts[0].strip()
                info_parts = parts[1].strip().split()
                
                # 提取各個部分
                weather_data = {
                    "location": location_name,
                    "condition": "",
                    "temperature": "",
                    "wind": "",
                    "humidity": "",
                    "raw_text": weather_text
                }
                
                # 解析各部分（更靈活的解析方式）
                for part in info_parts:
                    if "°C" in part or "°F" in part:
                        weather_data["temperature"] = part
                    elif "%" in part:
                        weather_data["humidity"] = part
                    elif "km/h" in part or "mph" in part:
                        # 風速可能包含方向符號
                        idx = info_parts.index(part)
                        if idx > 0 and info_parts[idx-1] in ["↑", "↓", "←", "→", "↖", "↗", "↙", "↘"]:
                            weather_data["wind"] = info_parts[idx-1] + part
                        else:
                            weather_data["wind"] = part
                    elif part not in ["↑", "↓", "←", "→", "↖", "↗", "↙", "↘"]:
                        # 其他部分視為天氣狀況
                        if weather_data["condition"]:
                            weather_data["condition"] += " " + part
                        else:
                            weather_data["condition"] = part
                
                info_log(f"[INT] 取得天氣：{location_name} - {weather_data['condition']} {weather_data['temperature']}")
                return weather_data
            else:
                # 如果格式不符，返回原始文字
                return {
                    "location": location,
                    "condition": weather_text,
                    "temperature": "",
                    "wind": "",
                    "humidity": "",
                    "raw_text": weather_text
                }
        except Exception as parse_error:
            error_log(f"[INT] 解析天氣資料失敗: {parse_error}，返回原始文字")
            return {
                "location": location,
                "condition": weather_text,
                "temperature": "",
                "wind": "",
                "humidity": "",
                "raw_text": weather_text
            }
    except Exception as e:
        error_log(f"[INT] 取得天氣失敗: {e}")
        return {
            "location": location,
            "condition": "無法取得天氣資訊",
            "temperature": "",
            "wind": "",
            "humidity": "",
            "raw_text": f"錯誤: {str(e)}"
        }

def get_world_time(target_num: int = 1, tz: str = ""):
    """取得當前時間
    
    Args:
        target_num: 
            1 = 世界標準時間（UTC）
            2 = 指定時區時間（需搭配 tz 參數）
            3 = 本地時區時間（自動偵測）
        tz: 時區名稱（繁體中文），如：台灣、日本、美國東岸等（詳細時區表於 maps/time_zone.py）
        
    Returns:
        時間字串
    """
    from datetime import datetime, timezone
    from modules.sys_module.actions.maps.time_zone import timezone_map
    from zoneinfo import ZoneInfo

    try:
        if target_num == 1:
            # UTC 時間
            now = datetime.now(timezone.utc)
            info_log(f"[INT] 世界標準時間（UTC）：{now}")
            return f"世界標準時間（UTC）：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

        elif target_num == 2:
            # 指定時區時間
            if not tz:
                error_log("[INT] 未輸入時區參數")
                raise ValueError("target_num=2 時必須傳入 tz 參數")
            
            tz_key = timezone_map.get(tz)
            if not tz_key:
                error_log(f"[INT] 未知時區：{tz}")
                available_zones = ", ".join(timezone_map.keys())
                return f"錯誤：未知時區 '{tz}'。可用時區：{available_zones}"
            
            assign_time = datetime.now(ZoneInfo(tz_key))
            info_log(f"[INT] {tz} 時區時間：{assign_time}")
            return f"{tz} 當前時間：{assign_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        
        elif target_num == 3:
            # 本地時區時間（使用 tzlocal 自動偵測）
            try:
                from tzlocal import get_localzone
                local_tz = get_localzone()
                local_time = datetime.now(local_tz)
                info_log(f"[INT] 本地時區時間：{local_time} ({local_tz})")
                return f"本地時間：{local_time.strftime('%Y-%m-%d %H:%M:%S %Z')} (時區: {local_tz})"
            except ImportError:
                error_log("[INT] tzlocal 套件未安裝，改用系統時間")
                local_time = datetime.now()
                info_log(f"[INT] 本地時間（系統）：{local_time}")
                return f"本地時間：{local_time.strftime('%Y-%m-%d %H:%M:%S')} (tzlocal 未安裝，無時區資訊)"
        
        else:
            error_log(f"[INT] 無效的 target_num：{target_num}")
            return "錯誤：target_num 必須是 1（UTC）、2（指定時區）或 3（本地時區）"

    except Exception as e:
        error_log(f"[INT] 取得時間失敗: {e}")
        return f"錯誤：{str(e)}"

def code_analysis(code: str, analysis_type: str = "general") -> dict:
    """
    程式碼分析 - 使用 LLM 進行智能程式碼分析
    
    Args:
        code: 要分析的程式碼
        analysis_type: 分析類型 (general=一般分析, security=安全檢查, optimize=優化建議, explain=解釋說明)
        
    Returns:
        分析結果
    """
    try:
        from modules.llm_module.llm_module import LLMModule
        from configs.config_loader import load_module_config
        
        # 初始化 LLM 模組
        config = load_module_config("llm_module")
        if "use_prompt_caching" in config:
            config["use_prompt_caching"] = False
        llm_module = LLMModule(config)
        
        # 根據分析類型構建提示詞
        prompts = {
            "general": f"""請分析以下程式碼並提供：
1. 程式碼功能摘要
2. 主要邏輯結構
3. 潛在問題或改進建議

程式碼：
```
{code}
```

請用繁體中文回應，格式清晰易讀。""",
            
            "security": f"""請從安全角度分析以下程式碼，檢查：
1. 潛在的安全漏洞（SQL injection, XSS, CSRF 等）
2. 不安全的函數使用
3. 輸入驗證問題
4. 權限控制問題
5. 安全改進建議

程式碼：
```
{code}
```

請用繁體中文回應，重點標註高風險問題。""",
            
            "optimize": f"""請分析以下程式碼的效能和優化空間：
1. 時間複雜度分析
2. 空間複雜度分析
3. 可能的效能瓶頸
4. 具體優化建議（附程式碼範例）
5. 最佳實踐建議

程式碼：
```
{code}
```

請用繁體中文回應，提供具體可行的優化方案。""",
            
            "explain": f"""請詳細解釋以下程式碼：
1. 每個部分的作用
2. 使用的演算法或設計模式
3. 資料流向
4. 適合初學者理解的逐行說明

程式碼：
```
{code}
```

請用繁體中文回應，用淺顯易懂的方式說明。"""
        }
        
        prompt = prompts.get(analysis_type, prompts["general"])
        
        # 使用內部呼叫模式
        request_data = {
            "text": prompt,
            "intent": "chat",
            "is_internal": True
        }
        
        response = llm_module.handle(request_data)
        
        if response and response.get("status") == "ok" and "text" in response:
            info_log(f"[INT] 程式碼分析完成（類型：{analysis_type}）")
            return {
                "status": "ok",
                "analysis": response["text"],
                "analysis_type": analysis_type
            }
        else:
            error_log(f"[INT] LLM 分析失敗：{response}")
            # 降級為 AST 分析
            fallback_result = _fallback_ast_analysis(code)
            return {
                "status": "ok",
                "analysis": fallback_result,
                "analysis_type": "fallback_ast"
            }
    
    except ImportError:
        error_log("[INT] LLM 模組未安裝，使用 AST 分析")
        fallback_result = _fallback_ast_analysis(code)
        return {
            "status": "ok",
            "analysis": fallback_result,
            "analysis_type": "fallback_ast"
        }
    except Exception as e:
        error_log(f"[INT] 程式碼分析失敗: {e}")
        return {
            "status": "error",
            "message": str(e)
        }


def _fallback_ast_analysis(code: str) -> str:
    """備用方案：簡易 AST 分析"""
    try:
        tree = ast.parse(code)
        return f"[簡易 AST 分析]\n\n{ast.dump(tree, indent=2)}"
    except Exception as e:
        error_log(f"[INT] AST 解析失敗: {e}")
        return f"程式碼分析失敗：{str(e)}"


# google_calendar_agent 已移除，請使用 automation_helper.local_calendar 替代

