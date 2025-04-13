from core.registry import get_module

stt = get_module("stt_module")     # �^�Ǫ��O�ŦX BaseModule �����
nlp = get_module("nlp_module")
llm = get_module("llm_module")
mem = get_module("mem_module")
tts = get_module("tts_module")
sysmod = get_module("sys_module")

def handle_user_input():
    # Step 1: ���o�y����J���ର��r
    audio_text = stt.handle({})["text"]

    # Step 2: NLP �ҲէP�_ intent
    nlp_result = nlp.handle({"text": audio_text})
    intent = nlp_result.get("intent")
    detail = nlp_result.get("detail")

    # Step 3: ���y�B�z�]��ѩΫ��O�^
    if intent == "chat":
        snapshot = mem.handle({"mode": "fetch", "text": audio_text})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "chat",
            "snapshot": snapshot
        })
        mem.handle({"mode": "store", "entry": llm_result["log"]})
    elif intent == "command":
        available_sys = sysmod.handle({"mode": "list_functions"})
        llm_result = llm.handle({
            "text": audio_text,
            "intent": "command",
            "available": available_sys,
            "detail": detail
        })
        sysmod.handle(llm_result.get("sys_action", {}))
    else:
        llm_result = {"text": "�ڤ��ө��էA���N��..."}

    # Step 4: ��X�� TTS �M UI
    tts.handle({
        "text": llm_result.get("text"),
        "emotion": llm_result.get("emotion", "neutral")
    })

    return llm_result.get("text")


def stt_test():
    # ���� STT �Ҳ�
    result = stt.handle({"audio": "test_audio.wav"})
    print("STT Result:", result)