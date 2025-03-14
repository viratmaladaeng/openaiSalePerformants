#main_BCK11_redis_okok
import os
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, QuickReply, QuickReplyButton, MessageAction, FollowEvent
import requests
from dotenv import load_dotenv
import openai
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
import datetime
from datetime import datetime, timezone, timedelta
import redis
import json
from linebot.models import TextSendMessage
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
AZURE_SEARCH_INDEX = os.getenv("AZURE_SALES_INDEX")
AZURE_OAI_DEPLOYMENT = os.getenv("AZURE_OAI_DEPLOYMENT")
AZURE_CHAT_HISTORY_INDEX = os.getenv("AZURE_CHAT_HISTORY_INDEX")
AZURE_SALES_INDEX = os.getenv("AZURE_SALES_INDEX")
# โหลดค่า Environment Variables
REDIS_HOST = os.getenv("REDIS_HOST")
#REDIS_PORT = 6380
REDIS_PORT = int(os.getenv("REDIS_PORT"))  # ✅ ตั้งค่าให้แน่ใจว่าใช้ 6380
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")  # ใช้ Primary Key


# ตรวจสอบว่าค่าถูกตั้งไว้
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX,AZURE_OAI_DEPLOYMENT
]):
    raise ValueError("Environment variables not set properly")

# Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-08-01-preview"

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

redis_client = redis.StrictRedis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    ssl=True  # ต้องเปิดใช้งาน SSL
    )

# ฟังก์ชันอ่านไฟล์ข้อความ
def read_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read().strip()
    return ""

# โหลดข้อความจาก system.txt และ grounding.txt
system_message = read_file("system.txt")
grounding_text = read_file("grounding.txt")

# ตั้งค่า Line Messaging API
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.get("/")
async def read_root():
    return {"message": "Hello, world!"}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers["X-Line-Signature"]
    body = await request.body()
    
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(FollowEvent)
def handle_follow(event):
    """ ตอบกลับเมื่อผู้ใช้เพิ่ม Bot ใหม่ หลังจากลบการสนทนา """
    welcome_message = (
        "ขอบคุณที่เพิ่มเราเป็นเพื่อนอีกครั้ง! 😊\n"
        "หากต้องการสอบถามข้อมูลหรือเริ่มต้นสนทนาใหม่ พิมพ์ 'เริ่มการสนทนาใหม่' ได้เลยค่ะ"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome_message)
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_message.lower() in ["เริ่มการสนทนาใหม่", "reset"]:
        # 🔹 **ลบประวัติการสนทนาทั้งหมดของ user ออกจาก Azure Cognitive Search**
        delete_chat_historyR(user_id)
        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"

    else:
        
        # ดึงประวัติการสนทนา
        chat_history = get_chat_history(user_id)
        if not isinstance(chat_history, list):
            chat_history = []  # ตั้งค่าเป็น list ว่างถ้าไม่ได้รับข้อมูลที่ถูกต้อง

        print(f"✅ ประวัติแชท: {chat_history}")  # Debugging

        # เพิ่มข้อความของผู้ใช้ลงไปในแชท
        chat_history.append(f"User: {user_message}")

        # 🔹 แปลง chat_history เป็นข้อความเดียว
        search_query = " ".join(chat_history) if chat_history else user_message

        # 🔹 ค้นหาข้อมูลการขายจาก Azure Cognitive Search
        sales_data = search_sales_data(search_query, top=3)

        print(f"✅ ค้นหาข้อมูลการขายจาก: {search_query}")  # Debugging
        print(f"✅ ผลค้นหาข้อมูลการขายจาก RAG: {sales_data}")  # Debugging


        # เพิ่ม system_message โดยไม่ทำให้กลายเป็น list ซ้อนกัน
        chat_history.append({"role": "system", "content": system_message})

        # สร้าง prompt
        prompt = f"\n\nข้อมูลการขายที่เกี่ยวข้อง:\n"
        prompt += "\n".join([str(item) for item in sales_data])  # ต้องมีข้อมูลนี้
        prompt += "\n\nนี่คือประวัติการสนทนาเดิมของคุณ:\n"
        prompt += "\n".join([json.dumps(item, ensure_ascii=False) for item in chat_history])  
        prompt += f"\n\nผู้ใช้: {user_message}\n AI:"

        # เพิ่ม system_message และ grounding_text เข้าไป
        #prompt += f"\n\n---\n🛠 **System Message**:\n{system_message}"
        #prompt += f"\n\n🌍 **Grounding Information**:\n{grounding_text}"

        prompt += """
            คุณคือ AI ที่ช่วยเหลือในการค้นหาข้อมูล ให้ความสำคัญกับข้อมูลการขายที่เกี่ยวข้องมากกว่าบริบทเดิมของการสนทนา
            นี่คือลำดับความสำคัญของข้อมูล:
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
            "top_p": 0.4,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "stop": ["เริ่มการสนทนาใหม่"],
            "stream": False
        }
        def split_message(text, max_length=5000):
            return [text[i:i + max_length] for i in range(0, len(text), max_length)]
        try:
            response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            openai_response = response.json()

            if "choices" in openai_response and openai_response["choices"]:
                reply_message = openai_response["choices"][0]["message"]["content"]

                # 🔹 เพิ่มข้อมูลการขายที่ค้นพบ
                formatted_sales_data = "\n".join([f"🔹 {item}" for item in sales_data])

                full_message = f"{reply_message}\n\n🔎 ข้อมูลการขายที่พบ:\n{formatted_sales_data}"
                messages = [TextSendMessage(text=msg) for msg in split_message(full_message)]

            else:
                messages = [TextSendMessage(text="ขออภัย ระบบไม่สามารถให้คำตอบได้")]

        except requests.RequestException as e:
            print(f"❌ OpenAI API Error: {e}")
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ AI"


        # บันทึกลง Redis
        save_chatRedis(user_id, f"User: {user_message}")
        save_chatRedis(user_id, f"AI: {reply_message}")


    # สร้างปุ่ม Quick Reply
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

    # ส่งข้อความกลับไปยัง Line พร้อม Quick Reply
    MAX_LENGTH = 5000
    if len(reply_message) > MAX_LENGTH:
        reply_message = reply_message[:MAX_LENGTH] + "\n... (ข้อความยาวเกินไป ถูกตัดออก)"

    # ส่งข้อความกลับไปยัง Line
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

def delete_chat_historyR(user_id):
    """ลบประวัติแชทของผู้ใช้"""
    chat_key = f"chat_history:{user_id}"
    redis_client.delete(chat_key)
    print(f"🗑️ ลบประวัติแชทของ {user_id} สำเร็จ!")


def get_chat_history(user_id):
    """ดึงประวัติแชทจาก Redis"""
    chat_key = f"chat_history:{user_id}"
    chat_history = redis_client.get(chat_key)
    
    if chat_history:
        return json.loads(chat_history)
    return []    

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
