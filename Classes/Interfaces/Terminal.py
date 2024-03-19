import Classes.Functions.STT as stt
import threading
import tkinter as tk
import tkinter.font as tkFont
from tkinter import DISABLED, NORMAL, messagebox

SpeechTT = stt.SpeechToText()

class Terminal():
    def __init__(self) -> None:
        self.isActivate = False
        self.app = tk.Tk()
        self.app.title("後台控制器")
        self.app.geometry('500x500')

        self.STTLabel = tk.Label(text="語音轉譯相關功能", height=3, width=25)
        self.STTLabel.pack()
        # Add buttons to start and stop speech recognition
        self.start_button = tk.Button(self.app, text="開始傾聽", command=self.start_recognition)
        self.start_button.pack(pady=10)

        self.stop_button = tk.Button(self.app, text="停止傾聽", command=self.stop_recognition)
        self.stop_button.pack(pady=10)
    
    def recognition_loop(self):
        while getattr(recognition_thread, "do_run", True):
            audio_input = SpeechTT.listen_for_audio()
            text_output = SpeechTT.transcribe_audio(audio_input)
            if text_output:
                print("You said:", text_output)

    def start_recognition(self):
        # Add code here to start speech recognition
        if(not self.isActivate):
            messagebox.showinfo("Speech Recognition", "Speech recognition started.")
            self.isActivate = True
            self.start_button['state'] = DISABLED
            self.stop_button['state'] = NORMAL
            global recognition_thread
            recognition_thread = threading.Thread(target=self.recognition_loop)
            recognition_thread.daemon = True  # Daemonize the thread
            recognition_thread.start()

# Function to stop speech recognition
    def stop_recognition(self):
        # Add code here to stop speech recognition
        if(self.isActivate):
            messagebox.showinfo("Speech Recognition", "Speech recognition stopped.")
            self.isActivate = False
            self.start_button['state'] = NORMAL
            self.stop_button['state'] = DISABLED
            global recognition_thread
            recognition_thread.do_run = False
            recognition_thread.join()
        
    def activate(self):
        self.app.mainloop()