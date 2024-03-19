import Classes.Interfaces.Terminal as terminal
import Classes.Functions.STT as stt
import Classes.Functions.RWKV_Main as rwkv
import os

# Main function
if __name__ == "__main__":
    test = rwkv.RWKV_Main()
    input_text = "Hello, how are you today?"
    print(os.path.isfile('./Models/rwkv_v5.2_7B_role_play_16k.pth'))
   
    print("Initialized, begining testing process:")
    while(True):
        m = input()
        if(m == '0'): break
        print("Response: ", test.generate_response(m));
    print("Testing process ended.")
   # New_Ter = terminal.Terminal()
   # New_Ter.activate()
    