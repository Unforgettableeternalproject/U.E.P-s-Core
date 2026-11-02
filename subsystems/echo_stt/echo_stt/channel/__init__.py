"""
echo_stt.channel — 多通道音訊管理

提供：
- AudioChannel: 通道抽象基底
- LocalMicChannel: 本地麥克風通道（PyAudio callback）
- FileChannel: 預錄音檔通道（測試用）
- DiscordChannel: Discord 語音通道（接收 Discord voice sink PCM）
- ChannelManager: 多通道生命週期管理
"""

from echo_stt.channel.base import AudioChannel, FileChannel
from echo_stt.channel.local_mic import LocalMicChannel
from echo_stt.channel.discord_channel import DiscordChannel
from echo_stt.channel.manager import ChannelManager

__all__ = [
    "AudioChannel",
    "FileChannel",
    "LocalMicChannel",
    "DiscordChannel",
    "ChannelManager",
]
