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
):
    """擷取並整理新聞摘要
    
    Args:
        source: 新聞來源 ("google_news_tw" 爬取 Google News 台灣首頁, "rss" 使用 RSS feed)
        max_items: 最大新聞數量
        rss_url: RSS feed URL (當 source="rss" 時使用)
        use_full_crawler: 是否使用 Selenium 爬取完整文章內容（僅 RSS 模式支援）
        
    Returns:
        str: 新聞摘要（標題列表，適合給 LLM 總結）
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
                return "錯誤：RSS 模式需要提供 rss_url"
            
            d = feedparser.parse(rss_url)
            entries = d.entries[:max_items]
            
            if not use_full_crawler:
                # 簡易模式：只回傳標題
                summaries = []
                for e in entries:
                    summaries.append(f"- {e.title}")
                info_log(f"[INT] 擷取 {len(summaries)} 則新聞 (RSS 模式)")
                return "\n".join(summaries)
            else:
                # 完整模式：使用 Selenium 爬取文章內容
                info_log("[INT] 使用 Selenium 爬取完整新聞內容")
                return _crawl_full_articles(entries)
        
        else:
            error_log(f"[INT] 未知的新聞來源：{source}")
            return f"錯誤：未知的新聞來源 {source}"
            
    except Exception as e:
        error_log(f"[INT] 擷取新聞失敗: {e}")
        return f"錯誤：{str(e)}"

def _crawl_google_news_homepage(max_items: int = 10):
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
                # 格式化為適合 LLM 的格式（英文開頭，指示用英文回應）
                result = "Here are the latest news headlines from Taiwan (please respond in English):\n"
                for title in news_titles:
                    result += f"- {title}\n"
                return result.strip()
            else:
                error_log("[INT] Google News 爬取失敗，未找到新聞標題")
                return "Unable to fetch news headlines"
        
        finally:
            try:
                driver.quit()
            except:
                pass
    
    except ImportError:
        error_log("[INT] 缺少 Selenium 或 BeautifulSoup")
        return "錯誤：請安裝 selenium 和 beautifulsoup4"
    except Exception as e:
        error_log(f"[INT] Google News 爬取失敗：{e}")
        return f"錯誤：{str(e)}"


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
        target_num: 1=世界標準時間（UTC）, 2=指定時區時間
        tz: 時區名稱（繁體中文），如：台灣、日本、美國東岸等（詳細時區表於 maps/time_zone.py）
        
    Returns:
        時間字串
    """
    from datetime import datetime
    from modules.sys_module.actions.maps.time_zone import timezone_map
    from zoneinfo import ZoneInfo

    try:
        if target_num == 1:
            now = datetime.now()
            info_log(f"[INT] 當前時間（UTC）：{now}")
            return f"當前時間（UTC）：{now.strftime('%Y-%m-%d %H:%M:%S')}"

        elif target_num == 2:
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
        
        else:
            error_log(f"[INT] 無效的 target_num：{target_num}")
            return "錯誤：target_num 必須是 1（UTC）或 2（指定時區）"

    except Exception as e:
        error_log(f"[INT] 取得時間失敗: {e}")
        return f"錯誤：{str(e)}"

def code_analysis(code: str, analysis_type: str = "general") -> str:
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
            return response["text"]
        else:
            error_log(f"[INT] LLM 分析失敗：{response}")
            # 降級為 AST 分析
            return _fallback_ast_analysis(code)
    
    except ImportError:
        error_log("[INT] LLM 模組未安裝，使用 AST 分析")
        return _fallback_ast_analysis(code)
    except Exception as e:
        error_log(f"[INT] 程式碼分析失敗: {e}")
        return _fallback_ast_analysis(code)


def _fallback_ast_analysis(code: str) -> str:
    """備用方案：簡易 AST 分析"""
    try:
        tree = ast.parse(code)
        return f"[簡易 AST 分析]\n\n{ast.dump(tree, indent=2)}"
    except Exception as e:
        error_log(f"[INT] AST 解析失敗: {e}")
        return f"程式碼分析失敗：{str(e)}"

def media_control(
    action: str,
    song_query: str = "",
    music_folder: str = "",
    youtube: bool = False,
    spotify: bool = False
) -> str:
    """
    音樂播放控制器 - 支援本地音樂、YouTube、Spotify
    
    Args:
        action: 動作指令 (play, pause, stop, next, previous, search, youtube, spotify)
        song_query: 歌曲查詢關鍵字
        music_folder: 本地音樂資料夾路徑
        youtube: 是否使用 YouTube 播放
        spotify: 是否使用 Spotify 播放
        
    Returns:
        操作結果訊息
    """
    global _music_player
    
    try:
        if action == "youtube" or youtube:
            return _play_youtube(song_query)
        elif action == "spotify" or spotify:
            return _play_spotify(song_query)
        else:
            # 本地播放
            if not music_folder:
                music_folder = str(Path.home() / "Music")
            
            if _music_player is None:
                _music_player = MusicPlayer(music_folder)
                info_log("[INT] 音樂播放器已初始化")
            
            if action == "play":
                if song_query:
                    # 搜尋並播放
                    found = _music_player.search_and_play(song_query)
                    if found:
                        return f"正在播放：{song_query}"
                    else:
                        return f"找不到歌曲：{song_query}"
                else:
                    # 繼續播放
                    _music_player.play()
                    return "繼續播放"
            
            elif action == "pause":
                _music_player.pause()
                return "已暫停"
            
            elif action == "stop":
                _music_player.stop()
                return "已停止"
            
            elif action == "next":
                _music_player.next_song()
                return "下一首"
            
            elif action == "previous":
                _music_player.previous_song()
                return "上一首"
            
            elif action == "search":
                results = _music_player.search_song(song_query)
                if results:
                    return f"找到 {len(results)} 首歌曲：" + ", ".join(results[:5])
                else:
                    return f"找不到：{song_query}"
            
            else:
                return f"未知指令：{action}"
    
    except Exception as e:
        error_log(f"[INT] 媒體控制失敗: {e}")
        return f"錯誤：{str(e)}"


def _play_youtube(query: str) -> str:
    """使用 pywhatkit 播放 YouTube"""
    try:
        import pywhatkit
        info_log(f"[INT] 正在 YouTube 上播放：{query}")
        pywhatkit.playonyt(query)
        return f"已在 YouTube 上播放：{query}"
    except ImportError:
        error_log("[INT] pywhatkit 未安裝，請執行：pip install pywhatkit")
        return "錯誤：需要安裝 pywhatkit"
    except Exception as e:
        error_log(f"[INT] YouTube 播放失敗：{e}")
        return f"YouTube 播放失敗：{str(e)}"


def _play_spotify(query: str) -> str:
    """開啟 Spotify 搜尋（使用瀏覽器）"""
    try:
        import webbrowser
        search_url = f"https://open.spotify.com/search/{query.replace(' ', '%20')}"
        webbrowser.open(search_url)
        info_log(f"[INT] 已在 Spotify 上搜尋：{query}")
        return f"已在 Spotify 上搜尋：{query}"
    except Exception as e:
        error_log(f"[INT] Spotify 搜尋失敗：{e}")
        return f"Spotify 搜尋失敗：{str(e)}"


# 全域音樂播放器實例
_music_player = None


class MusicPlayer:
    """本地音樂播放器（無 UI 版本）"""
    
    def __init__(self, music_folder: str):
        self.music_folder = Path(music_folder)
        self.playlist = []
        self.current_index = 0
        self.is_playing = False
        self.is_looping = False
        self.current_song = None
        self.playback_obj = None
        
        # 載入播放清單
        self._load_playlist()
        
    def _load_playlist(self):
        """載入音樂資料夾中的歌曲"""
        if not self.music_folder.exists():
            error_log(f"[INT] 音樂資料夾不存在：{self.music_folder}")
            return
        
        # 支援的音樂格式
        audio_formats = ['.mp3', '.wav', '.flac', '.ogg', '.m4a']
        
        for file in self.music_folder.rglob('*'):
            if file.suffix.lower() in audio_formats:
                self.playlist.append(str(file))
        
        info_log(f"[INT] 載入 {len(self.playlist)} 首歌曲")
    
    def search_song(self, query: str) -> list:
        """搜尋歌曲（使用模糊比對）"""
        try:
            from rapidfuzz import process, fuzz
            
            # 從檔名中提取歌曲名稱
            song_names = [Path(song).stem for song in self.playlist]
            
            # 使用 rapidfuzz 進行模糊搜尋
            results = process.extract(
                query, 
                song_names, 
                scorer=fuzz.WRatio, 
                limit=10
            )
            
            # 回傳相似度 > 60 的結果
            matched = [r[0] for r in results if r[1] > 60]
            return matched
        
        except ImportError:
            info_log("[INT] rapidfuzz 未安裝，使用簡單搜尋")
            # 簡單字串比對
            matched = []
            query_lower = query.lower()
            for song in self.playlist:
                song_name = Path(song).stem.lower()
                if query_lower in song_name:
                    matched.append(Path(song).stem)
            return matched
    
    def search_and_play(self, query: str) -> bool:
        """搜尋並播放歌曲"""
        results = self.search_song(query)
        if results:
            # 找到第一首符合的歌曲
            for i, song in enumerate(self.playlist):
                if Path(song).stem == results[0]:
                    self.current_index = i
                    self.play()
                    return True
        return False
    
    def play(self):
        """播放當前歌曲"""
        if not self.playlist:
            error_log("[INT] 播放清單為空")
            return
        
        try:
            from pydub import AudioSegment
            from pydub.playback import _play_with_simpleaudio
            
            if self.current_index >= len(self.playlist):
                self.current_index = 0
            
            song_path = self.playlist[self.current_index]
            self.current_song = Path(song_path).stem
            
            info_log(f"[INT] 正在播放：{self.current_song}")
            
            # 載入音訊
            audio = AudioSegment.from_file(song_path)
            
            # 播放（在背景執行緒）
            import threading
            self.is_playing = True
            
            def _play_thread():
                try:
                    self.playback_obj = _play_with_simpleaudio(audio)
                    self.playback_obj.wait_done()
                    
                    # 播放完成後
                    if self.is_looping:
                        self.play()
                    else:
                        self.next_song()
                except Exception as e:
                    error_log(f"[INT] 播放錯誤：{e}")
                    self.is_playing = False
            
            play_thread = threading.Thread(target=_play_thread, daemon=True)
            play_thread.start()
            
        except ImportError:
            error_log("[INT] pydub 未安裝，請執行：pip install pydub simpleaudio")
        except Exception as e:
            error_log(f"[INT] 播放失敗：{e}")
    
    def pause(self):
        """暫停播放"""
        if self.playback_obj:
            self.playback_obj.stop()
            self.is_playing = False
            info_log("[INT] 已暫停")
    
    def stop(self):
        """停止播放"""
        if self.playback_obj:
            self.playback_obj.stop()
            self.is_playing = False
            self.current_index = 0
            info_log("[INT] 已停止")
    
    def next_song(self):
        """下一首"""
        self.stop()
        self.current_index = (self.current_index + 1) % len(self.playlist)
        self.play()
    
    def previous_song(self):
        """上一首"""
        self.stop()
        self.current_index = (self.current_index - 1) % len(self.playlist)
        self.play()
    
    def toggle_loop(self):
        """切換單曲循環"""
        self.is_looping = not self.is_looping
        info_log(f"[INT] 單曲循環：{'開啟' if self.is_looping else '關閉'}")


def google_calendar_agent(
    action: str,
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    event_id: str = "",
    credentials_path: str = "configs/credentials.json",
    token_path: str = "configs/token.json"
) -> dict:
    """
    Google Calendar 整合 - 支援事件建立、查詢、刪除
    
    Args:
        action: 動作 (auth, create, list, delete)
        summary: 事件標題
        start_time: 開始時間（ISO 格式或相對時間）
        end_time: 結束時間（ISO 格式或相對時間）
        description: 事件描述
        event_id: 事件 ID（用於刪除）
        credentials_path: OAuth credentials 檔案路徑
        token_path: OAuth token 快取路徑
        
    Returns:
        操作結果（dict，包含 status 和 data/message）
    """
    try:
        # 初始化 Google Calendar 服務
        service = _get_calendar_service(credentials_path, token_path)
        
        if action == "auth":
            # 僅驗證授權
            return {"status": "ok", "message": "已授權"}
        
        elif action == "create":
            # 建立事件
            if not summary or not start_time:
                return {"status": "error", "message": "缺少必要參數：summary, start_time"}
            
            # 解析時間
            start_dt = _parse_time_string(start_time)
            if not end_time:
                # 預設 1 小時後結束
                from datetime import timedelta
                end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = _parse_time_string(end_time)
            
            # 建立事件
            event = {
                'summary': summary,
                'description': description,
                'start': {
                    'dateTime': start_dt.isoformat(),
                    'timeZone': str(_get_local_timezone()),
                },
                'end': {
                    'dateTime': end_dt.isoformat(),
                    'timeZone': str(_get_local_timezone()),
                },
            }
            
            result = service.events().insert(calendarId='primary', body=event).execute()
            info_log(f"[INT] 已建立日曆事件：{summary}")
            return {
                "status": "ok",
                "message": f"已建立事件：{summary}",
                "event_id": result.get('id'),
                "event_link": result.get('htmlLink')
            }
        
        elif action == "list":
            # 列出近期事件
            from datetime import datetime
            now = datetime.utcnow().isoformat() + 'Z'
            
            events_result = service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            if not events:
                return {"status": "ok", "message": "沒有即將到來的事件", "events": []}
            
            event_list = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                event_list.append({
                    "id": event['id'],
                    "summary": event.get('summary', '無標題'),
                    "start": start,
                    "link": event.get('htmlLink')
                })
            
            info_log(f"[INT] 查詢到 {len(event_list)} 個事件")
            return {"status": "ok", "events": event_list}
        
        elif action == "delete":
            # 刪除事件
            if not event_id:
                return {"status": "error", "message": "缺少 event_id"}
            
            service.events().delete(calendarId='primary', eventId=event_id).execute()
            info_log(f"[INT] 已刪除日曆事件：{event_id}")
            return {"status": "ok", "message": f"已刪除事件：{event_id}"}
        
        else:
            return {"status": "error", "message": f"未知動作：{action}"}
    
    except FileNotFoundError as e:
        error_log(f"[INT] Google Calendar 授權檔案不存在：{e}")
        return {
            "status": "error",
            "message": f"缺少授權檔案，請確認 {credentials_path} 存在"
        }
    except Exception as e:
        error_log(f"[INT] Google Calendar 操作失敗：{e}")
        return {"status": "error", "message": str(e)}


def _get_calendar_service(credentials_path: str, token_path: str):
    """取得 Google Calendar API 服務（處理 OAuth）"""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        
        creds = None
        # 檢查是否有已儲存的 token
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
        # 如果沒有有效的憑證，執行授權流程
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, SCOPES
                )
                # 使用本地伺服器授權（無需 CLI 互動）
                creds = flow.run_local_server(port=0)
            
            # 儲存憑證
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        
        service = build('calendar', 'v3', credentials=creds)
        info_log("[INT] Google Calendar 服務已初始化")
        return service
    
    except ImportError:
        error_log("[INT] 缺少 Google Calendar 套件")
        raise ImportError("請安裝：pip install google-auth google-auth-oauthlib google-api-python-client")


def _parse_time_string(time_str: str):
    """解析時間字串（支援相對時間和絕對時間）"""
    from datetime import datetime, timedelta
    import re
    
    # 處理相對時間（例如：明天 3pm, 1小時後）
    relative_patterns = {
        r'(\d+)\s*(小時|hour)後': lambda m: timedelta(hours=int(m.group(1))),
        r'(\d+)\s*(分鐘|minute)後': lambda m: timedelta(minutes=int(m.group(1))),
        r'(\d+)\s*(天|day)後': lambda m: timedelta(days=int(m.group(1))),
        r'明天': lambda m: timedelta(days=1),
        r'後天': lambda m: timedelta(days=2),
    }
    
    for pattern, delta_func in relative_patterns.items():
        match = re.search(pattern, time_str)
        if match:
            delta = delta_func(match)
            base_time = datetime.now(_get_local_timezone())
            
            # 如果有指定時間（例如：明天 3pm）
            time_match = re.search(r'(\d{1,2})\s*(am|pm)?', time_str, re.IGNORECASE)
            if time_match:
                hour = int(time_match.group(1))
                if time_match.group(2) and time_match.group(2).lower() == 'pm' and hour < 12:
                    hour += 12
                return (base_time + delta).replace(hour=hour, minute=0, second=0, microsecond=0)
            
            return base_time + delta
    
    # 嘗試解析 ISO 格式
    try:
        return datetime.fromisoformat(time_str)
    except ValueError:
        pass
    
    # 嘗試常見格式
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%m/%d %H:%M",
        "%d %H:%M"
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            # 如果沒有年份，使用當前年份
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            # 加上時區
            return dt.replace(tzinfo=_get_local_timezone())
        except ValueError:
            continue
    
    # 如果都失敗，回傳當前時間
    error_log(f"[INT] 無法解析時間：{time_str}，使用當前時間")
    return datetime.now(_get_local_timezone())


def _get_local_timezone():
    """取得本地時區"""
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except ImportError:
        error_log("[INT] tzlocal 未安裝，使用 UTC")
        from datetime import timezone
        return timezone.utc

