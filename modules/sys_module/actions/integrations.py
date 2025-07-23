import feedparser
import requests
import datetime
import subprocess
import psutil
import ast
from utils.debug_helper import info_log, error_log

def news_summary(rss_url: str, max_items: int = 5):
    raise NotImplementedError("news_summary 尚未實作")

    """擷取並整理 RSS 新聞摘要"""
    try:
        d = feedparser.parse(rss_url)
        entries = d.entries[:max_items]
        summaries = []
        for e in entries:
            summaries.append(f"- {e.title}: {e.link}")
        info_log(f"[INT] 擷取 {len(summaries)} 則新聞")
        return "\n".join(summaries)
    except Exception as e:
        error_log(f"[INT] 擷取新聞失敗: {e}")
        return ""

def get_weather(location: str, api_key: str):
    raise NotImplementedError("weather_time 尚未實作")

    """回傳天氣資訊 (OpenWeatherMap)"""
    url = f"https://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric"
    try:
        r = requests.get(url); r.raise_for_status()
        j = r.json()
        info = f"{j['name']}：{j['weather'][0]['description']}，{j['main']['temp']}°C"
        info_log(f"[INT] {info}")
        return info
    except Exception as e:
        error_log(f"[INT] 取得天氣失敗: {e}")
        return ""

def get_world_time(tz: str):
    raise NotImplementedError("get_world_time 尚未實作")

    """回傳指定時區時間"""
    try:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=int(tz))))
        s = now.strftime("%Y-%m-%d %H:%M")
        info_log(f"[INT] {tz} 時區時間：{s}")
        return s
    except Exception as e:
        error_log(f"[INT] 取得時間失敗: {e}")
        return ""

def code_analysis(code: str):
    raise NotImplementedError("code_analysis 尚未實作")

    """簡易 AST 分析，回傳結構樹"""
    try:
        tree = ast.parse(code)
        return ast.dump(tree, indent=2)
    except Exception as e:
        error_log(f"[INT] 解析程式碼失敗: {e}")
        return ""

def media_control(command: str):
    raise NotImplementedError("media_control 尚未實作")

    """透過 psutil 或 subprocess 控制多媒體 (示意)"""
    try:
        # 範例：如果是 Spotify，就呼叫外部指令
        if "spotify" in command.lower():
            subprocess.Popen(["spotify", "--play"])
            info_log("[INT] Spotify 播放指令已發送")
        else:
            info_log(f"[INT] 未知命令：{command}", "WARNING")
    except Exception as e:
        error_log(f"[INT] 媒體控制失敗: {e}")
