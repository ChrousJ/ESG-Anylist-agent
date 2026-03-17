import requests
import sys

try:
    print("Sending request to /chat...")
    resp = requests.post("http://127.0.0.1:8000/chat", json={"query": "请分析苹果公司的ESG表现。"}, timeout=120)
    print("Status:", resp.status_code)
    try:
        print(resp.json())
    except Exception as e:
        print("Raw text:", resp.text)
except Exception as e:
    print("Error:", e)
