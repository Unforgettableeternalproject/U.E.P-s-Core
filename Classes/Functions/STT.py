from asyncio.windows_events import NULL
from collections import Counter
import speech_recognition as sr
from python_speech_features import mfcc
from python_speech_features import delta
import numpy as np
import scipy.io.wavfile as wav
import librosa, io
from sklearn.mixture import GaussianMixture
# import nltk, spacy
# import gensim
# from gensim import corpora
# from gensim.models import LdaModel
# from nltk.tokenize import word_tokenize
# from nltk.corpus import stopwords
# from nltk.stem import WordNetLemmatizer
# import string

class SpeechToText():
    def __init__(self):
        self.gmm_model = None
        
    # def train_gmm(self, features, num_components=3):
    #     gmm = GaussianMixture(n_components=num_components, covariance_type='diag')
    #     gmm.fit(features)
    #     return gmm

    # def extract_mfcc(self, wav_data):
    #     rate, signal = librosa.load(io.BytesIO(wav_data))
    #     mfcc_features = mfcc(signal, rate, nfft=1200)
    #     delta_features = delta(mfcc_features, 2)
    #     return np.hstack([mfcc_features, delta_features])

    # def identify_speaker(self, audio_features):
    #     if self.gmm_model:
    #         speaker_id = self.gmm_model.predict(audio_features)
    #         return speaker_id
    #     else:
    #         return -1
            
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

    # def train_model(self, labeled_data):
    #     features, labels = [], []
    #     for data_point in labeled_data:
    #         filename, speaker_id = data_point
    #         audio_features = self.extract_mfcc(filename)
    #         features.append(audio_features)
    #         labels.append(speaker_id)
        
    #     features = np.vstack(features)
    #     self.gmm_model = self.train_gmm(features, num_components=len(set(labels)))
        
    # def perform_semantic_analysis(self, text):
    #     def preprocess(text):
    #         tokens = word_tokenize(text.lower())
    #         tokens = [token for token in tokens if token not in string.punctuation and token not in stopwords.words("english")]
    #         lemmatizer = WordNetLemmatizer()
    #         tokens = [lemmatizer.lemmatize(token) for token in tokens]
    #         return tokens
        
    #     preprocessed_sentence = preprocess(text)
    #     word_freq = Counter(preprocessed_sentence)
    #     dictionary = corpora.Dictionary([preprocessed_sentence])
    #     bow_corpus = [dictionary.doc2bow(preprocessed_sentence)]
    #     lda_model = LdaModel(bow_corpus, num_topics=1, id2word=dictionary, passes=10)
    #     topic_distribution = lda_model.get_document_topics(bow_corpus[0], minimum_probability=0.0)

    #     main_topic = max(topic_distribution, key=lambda item: item[1])[0]

    #     topic_words = [word for word, weight in lda_model.show_topic(main_topic)]
    #     summary_words = [word for word, freq in word_freq.most_common() if word not in stopwords.words("english")]
    #     summary_sentence = ' '.join(topic_words)
    
    #     return summary_sentence

class Test():
    
    def STTTest():
        audio_input = SpeechToText().listen_for_audio()
        text_output = SpeechToText().transcribe_audio(audio_input)
        
        if text_output:
            print("You said:", text_output)

    # def SemTest():
    #     text = "By keeping the old lines commented out, you can easily compare the previous and current versions of your code and understand the changes made"
    #     print("You probably mean:", SpeechToText().perform_semantic_analysis(text))

        
    # def SpeTest():
    #     audio_input = SpeechToText().listen_for_audio()
    #     audio_features = SpeechToText().extract_mfcc(audio_input.get_wav_data())
    #     speaker_id = SpeechToText().identify_speaker(audio_features)
    #     print("Identified Speaker:", speaker_id)
        
    def AllTest():
        audio_input = SpeechToText().listen_for_audio()
        text_output = SpeechToText().transcribe_audio(audio_input)
        concise_text = SpeechToText().perform_semantic_analysis(text_output)
        
        if text_output:
            print("You said:", text_output)
            print("You probably mean:", concise_text)