import speech_recognition as sr

class SpeechToText():
    def listen_for_audio(self):
        # Initialize recognizer
        recognizer = sr.Recognizer()

        # Use the default microphone as the audio source
        with sr.Microphone() as source:
            print("Listening...")

            # Adjust for ambient noise
            recognizer.adjust_for_ambient_noise(source)

            # Capture audio from the user
            audio = recognizer.listen(source)

        return audio

    def transcribe_audio(self, audio):
        # Initialize recognizer
        recognizer = sr.Recognizer()

        try:
            # Use the recognizer to convert speech to text
            text = recognizer.recognize_google(audio)
            return text
        except sr.UnknownValueError:
            print("Sorry, I couldn't understand what you said.")
        except sr.RequestError as e:
            print(f"Could not request results from Google Speech Recognition service; {e}")

class Test():
    
    def STTTest():
        audio_input = SpeechToText().listen_for_audio()
        text_output = SpeechToText().transcribe_audio(audio_input)
        
        if text_output:
            print("You said:", text_output)