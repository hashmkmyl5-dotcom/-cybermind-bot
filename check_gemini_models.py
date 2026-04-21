import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/bot_project/bot_config_2.env")
api_key = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=api_key)

try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(m.name)
except Exception as e:
    print(f"Error: {e}")
