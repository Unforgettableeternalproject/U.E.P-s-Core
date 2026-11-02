"""下載 OKHand/Clean_Common_Voice_Speech_24.0-TW（CC0）到 gitignore 的 outputs/tw_lora_data/raw。
Common Voice 條款：禁止重新散布、禁止識別語者，資料只留本機。"""
import os
from huggingface_hub import snapshot_download

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
snapshot_download("OKHand/Clean_Common_Voice_Speech_24.0-TW", repo_type="dataset",
                  local_dir=os.path.join(ROOT, "outputs", "tw_lora_data", "raw"))
snapshot_download("JacobLinCool/TaiMECS", repo_type="dataset",
                  local_dir=os.path.join(ROOT, "outputs", "tw_lora_data", "taimecs"))
print("downloaded")
