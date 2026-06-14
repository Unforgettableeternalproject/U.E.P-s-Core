# Generation Parity Verification

## 2026-06-11 Phase 1 Smoke

Command:

```powershell
C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe Entry.py generate --text "Hello, this is UEP TTS running from the separate test project." --output outputs\tts\phase1_smoke.wav --device cuda
```

Result:

- Loaded copied GPT checkpoint from `echo_tts/modules/tts_module/checkpoints/gpt.pth`.
- Loaded copied S2Mel checkpoint from `echo_tts/modules/tts_module/checkpoints/s2mel.pth`.
- Loaded copied U.E.P voice asset from `echo_tts/models/tts/uep.pt`.
- Generated `outputs/tts/phase1_smoke.wav`.
- Reported generated audio duration: 4.70 seconds.

Notes:

- First run compiled the BigVGAN custom CUDA extension and ran model warmup, so wall-clock startup was dominated by one-time initialization work.
- The output, checkpoints, and voice weights are intentionally Git ignored.
