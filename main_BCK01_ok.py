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

load_dotenv()

app = FastAPI()

# ดึงค่าจาก Environment Variables
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
AZURE_OAI_DEPLOYMENT = os.getenv("AZURE_OAI_DEPLOYMENT")

if not all([LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
            AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX, AZURE_OAI_DEPLOYMENT]):
    raise ValueError("Environment variables not set properly")

openai.api_type = "azure"
openai.api_base = AZURE_OPENAI_ENDPOINT
openai.api_key = AZURE_OPENAI_API_KEY
openai.api_version = "2024-02-15-preview"

# ตั้งค่า memory สำหรับเก็บประวัติการสนทนา
chat_memory = {}

def read_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as file:
            return file.read().strip()
    return ""

system_message = read_file("system.txt")
grounding_text = read_file("grounding.txt")

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

    # ตรวจสอบว่าผู้ใช้ต้องการเริ่มต้นใหม่หรือไม่
    if user_message.lower() in ["เริ่มการสนทนาใหม่", "reset"]:
        chat_memory[user_id] = [{"role": "system", "content": system_message}]
        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"
    else:
        # ใช้ memory เก็บประวัติการสนทนา
        if user_id not in chat_memory:
            chat_memory[user_id] = [{"role": "system", "content": system_message}]

        chat_memory[user_id].append({"role": "user", "content": user_message})

        # จำกัด memory เก็บไม่เกิน 20 ข้อความ
        if len(chat_memory[user_id]) > 5:
            chat_memory[user_id].pop(1)

        # ค้นหาเอกสารจาก Azure Cognitive Search
        search_results = search_documents(user_message)
        grounding_message = grounding_text if not search_results or "Error" in search_results[0] else "\n\n".join(search_results)
        chat_memory[user_id].append({"role": "assistant", "content": grounding_message})

        headers = {
            "Content-Type": "application/json",
            "api-key": AZURE_OPENAI_API_KEY
        }

        payload = {
            "messages": chat_memory[user_id],
            "max_tokens": 800,
            "temperature": 0.0,
            "top_p": 0.4,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "stop": ["เริ่มการสนทนาใหม่", "admin", "ผู้ดูแลระบบ", "ไม่มีข้อมูลในระบบ"],
            "stream": False
        }

        response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload)

        if response.status_code == 200:
            openai_response = response.json()
            reply_message = openai_response["choices"][0]["message"]["content"]
            chat_memory[user_id].append({"role": "assistant", "content": reply_message})
        else:
            reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ Azure OpenAI"

    # สร้างปุ่ม Quick Reply
    quick_reply_buttons = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔄 เริ่มใหม่", text="เริ่มการสนทนาใหม่")),
        #QuickReplyButton(action=MessageAction(label="🔍 ค้นหาสินค้า", text="ค้นหาสินค้าใหม่")),
        QuickReplyButton(action=MessageAction(label="📞 ติดต่อเจ้าหน้าที่", text="ติดต่อเจ้าหน้าที่"))
    ])

    # ส่งข้อความกลับไปยัง Line พร้อม Quick Reply
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_message, quick_reply=quick_reply_buttons)
    )

def search_documents(query, top=5):
    """ ค้นหาข้อมูลจาก Azure Cognitive Search """
    try:
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=AZURE_SEARCH_INDEX,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        results = search_client.search(search_text=query, top=top)
        
        documents = []
        for result in results:
            title = result.get("title", "No Title")
            chunk = result.get("chunk", "No Content")
            documents.append(f"Title: {title}\nContent: {chunk}")
        
        return documents if documents else ["ไม่พบข้อมูลที่เกี่ยวข้องค่ะ"]
    except Exception as e:
        return [f"ขออภัย ไม่สามารถเรียกข้อมูลได้ค่ะ: {e}"]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
