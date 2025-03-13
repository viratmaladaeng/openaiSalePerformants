import os
import datetime
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from datetime import datetime, timezone, timedelta
import redis
from dotenv import load_dotenv

# โหลดค่า Environment Variables
load_dotenv()

# สร้าง FastAPI instance
app = FastAPI()

# ดึงค่าจาก Environment Variables
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_CHAT_HISTORY_INDEX = os.getenv("AZURE_CHAT_HISTORY_INDEX")
AZURE_SALES_INDEX = os.getenv("AZURE_SALES_INDEX")
# โหลดค่า Environment Variables
REDIS_HOST = os.getenv("REDIS_HOST")
#REDIS_PORT = 6380
REDIS_PORT = int(os.getenv("REDIS_PORT"))  # ✅ ตั้งค่าให้แน่ใจว่าใช้ 6380
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")  # ใช้ Primary Key



# ตรวจสอบค่าที่จำเป็น
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY
]):
    raise ValueError("Environment variables not set properly")

# Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-02-15-preview"

import json
from datetime import timedelta

redis_client = redis.Redis(
    host=REDIS_HOST,  # ✅ ชื่อเซิร์ฟเวอร์ Redis (ต้องเป็นค่า Azure Redis Cache)
    port=REDIS_PORT,  # ✅ ต้องใช้ `port=6380` (SSL Port)
    password=REDIS_PASSWORD,  # ✅ ใส่ Primary Key ของ Redis
    ssl=True,  # ✅ ใช้ SSL/TLS (จำเป็น เพราะ Non-SSL Port ถูกปิด)
    decode_responses=True  # ✅ แปลงค่าที่ได้จาก Redis เป็น string อัตโนมัติ
)



# ตั้งค่า Azure Cognitive Search
chat_history_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT,
    index_name=AZURE_CHAT_HISTORY_INDEX,
    credential=AzureKeyCredential(AZURE_SEARCH_KEY)
)

sales_data_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT,
    index_name=AZURE_SALES_INDEX,
    credential=AzureKeyCredential(AZURE_SEARCH_KEY)
)

# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers["X-Line-Signature"]
    body = await request.body()
    
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_message.lower() in ["เริ่มการสนทนาใหม่", "reset"]:
        # 🔹 **ลบประวัติการสนทนาทั้งหมดของ user ออกจาก Azure Cognitive Search**
        delete_chat_historyR(user_id)
        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"

    else:
        chat_history = get_chat_history(user_id)
        print(f"✅ ผลการค้นหา (get_chat_history): {chat_history}")
        chat_history.append(f"User: {user_message}")
        
       

        if chat_history:
            print(f"✅ ข้อความที่ใช้ค้นหา (จากเก่าสุด): {chat_history}")  # Debugging print
            sales_data = search_sales_data(chat_history, top=3)
        else:
            print("⚠️ ไม่มีข้อมูลประวัติ ใช้ข้อความ User ปัจจุบันแทน")
            sales_data = search_sales_data(user_message, top=3)

        print(f"✅ ผลค้นหาข้อมูลการขายจาก RAG: {sales_data}")        



        #🔹 **สร้างข้อความ Context สำหรับ AI**
        prompt = f"\n\nข้อมูลการขายที่เกี่ยวข้อง:\n"
        prompt += "\n".join(sales_data)
        prompt += "นี่คือประวัติการสนทนาเดิมของคุณ:\n"
        prompt += "\n".join(chat_history)
        prompt += f"\n\nผู้ใช้: {user_message}\n AI:"
        prompt += """
            คุณคือ AI ที่ช่วยเหลือในการค้นหาข้อมูล ให้ความสำคัญกับข้อมูลการขายที่เกี่ยวข้องมากกว่าบริบทเดิมของการสนทนานี่คือประวัติการสนทนาเดิมของคุณ
                    1️⃣ **ข้อมูลการขายที่เกี่ยวข้อง** (สำคัญที่สุด)
                    2️⃣ **นี่คือประวัติการสนทนาเดิมของคุณ** (อ้างอิงเพิ่มเติม)
                    3️⃣ **ข้อความที่ผู้ใช้ถามล่าสุด**
                    """
        
        # 🔹 **ส่งข้อความไปยัง Azure OpenAI**
        headers = {
            "Content-Type": "application/json",
            "api-key": AZURE_OPENAI_API_KEY
        }

        payload = {
            "messages": [{"role": "system", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.0,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "stop": ["เริ่มการสนทนาใหม่"],
            "stream": False
        }

        #response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)
        try:
            response = requests.post(
            AZURE_OPENAI_ENDPOINT, headers=headers, json=payload, timeout=10
            )
            response.raise_for_status()  # เช็คว่าไม่มี error ใน response
        except requests.RequestException as e:
            print(f"❌ OpenAI API Error: {e}")
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ AI"


        if response.status_code == 200:
            openai_response = response.json()
            reply_message = openai_response["choices"][0]["message"]["content"]
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

        # 🔹 **บันทึกการสนทนาเข้า Azure Cognitive Search**
        save_chatRedis(user_id, f"User: {user_message}")
        save_chatRedis(user_id, f"AI: {reply_message}")




    # สร้างปุ่ม Quick Reply
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

    # ส่งข้อความกลับไปยัง Line พร้อม Quick Reply
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons)
    )

def search_sales_data(query, top=3):
    try:
        results = sales_data_client.search(search_text=query, top=top)
        return [result["chunk"] for result in results] if results else ["ไม่พบข้อมูลการขายที่เกี่ยวข้อง"]
    except Exception as e:
        print(f"❌ Error fetching sales data: {e}")
        return ["เกิดข้อผิดพลาดในการค้นหาข้อมูล"]

def save_chatRedis(user_id, message):
    """บันทึกข้อความสนทนาลง Redis"""
    chat_key = f"chat_history:{user_id}"
    
    # ดึงประวัติแชทเก่าจาก Redis
    chat_history = redis_client.get(chat_key)
    chat_history = json.loads(chat_history) if chat_history else []

    # เพิ่มข้อความใหม่
    chat_history.append(message)

    # บันทึกกลับเข้า Redis พร้อมกำหนด TTL (เช่น 24 ชั่วโมง)
    redis_client.setex(chat_key, timedelta(hours=24), json.dumps(chat_history))
    
    print(f"✅ บันทึกข้อความสำเร็จ: {message}")

# 🔹 ค้นหาประวัติการสนทนา
def get_chat_history(user_id):
    """ดึงประวัติแชทจาก Redis"""
    chat_key = f"chat_history:{user_id}"
    chat_history = redis_client.get(chat_key)
    
    if chat_history:
        return json.loads(chat_history)
    return []

def delete_chat_historyR(user_id):
    """ลบประวัติแชทของผู้ใช้"""
    chat_key = f"chat_history:{user_id}"
    redis_client.delete(chat_key)
    print(f"🗑️ ลบประวัติแชทของ {user_id} สำเร็จ!")




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
