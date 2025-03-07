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
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
AZURE_OAI_DEPLOYMENT = os.getenv("AZURE_OAI_DEPLOYMENT")

# ตรวจสอบค่าที่จำเป็น
if not all([
    LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, 
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX, AZURE_OAI_DEPLOYMENT
]):
    raise ValueError("Environment variables not set properly")

# Initialize Azure OpenAI
openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-02-15-preview"

# ตั้งค่า Azure Cognitive Search
search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT,
    index_name=AZURE_SEARCH_INDEX,
    credential=AzureKeyCredential(AZURE_SEARCH_KEY)
)

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
    """ ตอบกลับเมื่อผู้ใช้เพิ่ม Bot ใหม่ """
    welcome_message = "ขอบคุณที่เพิ่มเราเป็นเพื่อน! 😊\nหากต้องการเริ่มต้นสนทนาใหม่ พิมพ์ 'เริ่มการสนทนาใหม่' ค่ะ"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=welcome_message))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_message.lower() in ["เริ่มการสนทนาใหม่", "reset"]:
        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"
    else:
        # 🔹 **ค้นหาประวัติการสนทนา**
        chat_history = search_chat_history(user_id, user_message, top=5)
        
        # 🔹 **สร้างข้อความ Context สำหรับ AI**
        prompt = "นี่คือประวัติการสนทนาเดิมของคุณ:\n"
        prompt += "\n".join(chat_history)
        prompt += f"\n\nผู้ใช้: {user_message}\nAI:"

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
            "stream": False
        }

        response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)

        if response.status_code == 200:
            openai_response = response.json()
            reply_message = openai_response["choices"][0]["message"]["content"]
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

        # 🔹 **บันทึกการสนทนาเข้า Azure Cognitive Search**
        save_chat(user_id, user_message)
        save_chat(user_id, reply_message)

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

# 🔹 **บันทึกข้อความลง Azure Cognitive Search**
def save_chat(user_id, message):
    """บันทึกข้อความสนทนาไปยัง Azure Cognitive Search"""
    document = {
        "id": f"{user_id}-{datetime.datetime.utcnow().isoformat()}",
        "user_id": user_id,
        "timestamp": datetime.datetime.utcnow(),
        "message": message
    }
    search_client.upload_documents(documents=[document])
    print(f"✅ บันทึกข้อความของ {user_id} แล้ว")

# 🔹 **ค้นหาประวัติการสนทนา**
def search_chat_history(user_id, query, top=5):
    """ค้นหาข้อความเก่าของผู้ใช้จาก Azure Cognitive Search"""
    try:
        results = search_client.search(
            search_text=query,
            filter=f"user_id eq '{user_id}'",
            top=top,
            orderby="timestamp desc"
        )
        
        chat_history = [result["message"] for result in results]
        return chat_history if chat_history else ["ไม่มีประวัติการสนทนา"]
    except Exception as e:
        print(f"Error fetching chat history: {e}")
        return ["ขออภัย ไม่สามารถดึงประวัติการสนทนาได้"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
