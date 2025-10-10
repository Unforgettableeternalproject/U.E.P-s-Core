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

def get_world_time(target_num :int = 1, tz: str = ""):

    """取得當前時間
        輸入1：世界標準時間，
        輸入2：他區標準時間，需搭配傳入時區：如：日本、台灣...(詳細時區表於time_zone.py)

        """

    from datetime import datetime
    from modules.sys_module.actions import time_zone
    from zoneinfo import ZoneInfo

    try:
        if target_num == 1:
            now = datetime.now()
            info_log(f"[INT] 當前時間（UTC）：{now}")
            return f"[INT] 當前時間（UTC）：{now}"

        elif target_num == 2:
            if not tz:
                error_log("[INT] 未輸入他區時區")
                raise ValueError("target_num=2 時必須傳入 tz")
            tz_key = time_zone.timezone_map[tz]
            if not tz_key:
                error_log("[INT] 輸入未知時區")
                raise ValueError(f"未知時區: {tz}")
            assign_time = datetime.now(ZoneInfo(tz_key))
            info_log(f"[INT] {tz} 時區時間：{assign_time}")
            return f"{tz_key} is {assign_time}"

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

