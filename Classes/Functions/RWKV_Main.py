# import jieba
# from keybert import KeyBERT
# from wiki_searcher import WikiSearcher
import os

from torch.nn import init
os.environ['RWKV_JIT_ON'] = '1'
os.environ["RWKV_CUDA_ON"] = '0'
from rwkv.model import RWKV
from rwkv.utils import PIPELINE, PIPELINE_ARGS

# kw_model = KeyBERT(model='shibing624/text2vec-base-chinese')
# jieba.load_userdict('./wiki2jieba_user_dict.txt')
# wiki_searcher = WikiSearcher()

# def get_keywords(input_text):
#     def ws_zh(text):
#         return jieba.lcut(text)

#     vectorizer = CountVectorizer(tokenizer=ws_zh)
#     keywords = kw_model.extract_keywords(input_text, vectorizer=vectorizer)

#     output = ''
#     for x, y in keywords:
#         if y > 0.55:
#             output += x + ' '
#     return output

# Example usage:

class RWKV_Main():
    def __init__(self, model_path='./Models/rwkv_v5.2_7B_role_play_16k.pth') -> None:
        self.model_path = model_path
        print(os.path.isfile("./Models/20B_tokenizer.json"))
        # Initialize models and other necessary objects
        model = RWKV(model=self.model_path, strategy='cuda fp16i8 *3 -> cuda fp16')
        self.pipeline = PIPELINE(model, "./Models/20B_tokenizer.json")
        
    def my_print(self, s):
        print(s, end='', flush=True)
    
    def generate_response(self, input_text, mode='Test'):
    
        if(mode == 'Test'):
            args = PIPELINE_ARGS(temperature=0.8, top_p=0.3, top_k=100,
                                 alpha_frequency=1.2, alpha_presence=0.4, alpha_decay=0.996,
                                 token_ban=[0], token_stop=[187, 24281, 24272, 33161],
                                 chunk_len=256)
            init_ctx = f'''
                Speak to me in English, and only in English.
                In this stage, you can be whatever you wanted to be.
            
                Below is the conversation we have been running:
            
                User: {input_text}

                You: '''
            
            print("Generating response...")
            output = self.pipeline.generate(init_ctx, token_count=3500, args=args, callback=self.my_print)

        return output




