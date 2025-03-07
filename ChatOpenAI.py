import os
import requests

# ตั้งค่า API Key และ Endpoint ของ Azure OpenAI
API_KEY = ""
ENDPOINT = ""

headers = {
    "Content-Type": "application/json",
    "api-key": API_KEY
}

# สร้าง context memory สำหรับเก็บประวัติการสนทนา
messages = [
    {"role": "system", "content": "You are a helpful assistant assisting users with travel recommendations."}
]

def chat_with_openai(user_input):
    # เพิ่มข้อความของผู้ใช้ลงใน memory
    messages.append({"role": "user", "content": user_input})

    # สร้าง payload โดยใช้ประวัติการสนทนา
    payload = {
        "messages": messages,
        "max_tokens": 200,  # ให้ AI ตอบยาวขึ้น
        "temperature": 0.7
    }

    response = requests.post(ENDPOINT, headers=headers, json=payload)

    if response.status_code == 200:
        result = response.json()
        ai_response = result["choices"][0]["message"]["content"]
        
        # เพิ่มข้อความของ Assistant ลงใน memory
        messages.append({"role": "assistant", "content": ai_response})

        # ควบคุมขนาดของ messages ไม่ให้ยาวเกินไป (เช่น เก็บแค่ 10 ข้อความล่าสุด)
        if len(messages) > 20:  
            messages.pop(1)  # ลบข้อความแรก (ยกเว้น system message)

        return ai_response
    else:
        return f"Error: {response.status_code} - {response.text}"

# เริ่มต้น Loop สำหรับรับ input จากผู้ใช้
while True:
    user_message = input("User: ")
    if user_message.lower() == "exit":
        print("Ending chat session. Goodbye! 👋")
        break

    response = chat_with_openai(user_message)
    print(f"Assistant: {response}\n")
