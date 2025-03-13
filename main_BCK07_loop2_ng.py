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
from datetime import datetime, timezone

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
        delete_chat_history(user_id)

        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"
    else:
        # 🔹 **ค้นหาประวัติการสนทนา**
        chat_history = search_chat_history(user_id, user_message, top=5)
        print(f"✅ ผลค้นหาประวัติการสนทนา: {chat_history}")
        # 🔹 **ค้นหาข้อมูลการขายจาก RAG**
        
        sales_data = get_combined_user_history(user_id, user_message, top=5)
        #sales_data = search_sales_data(user_message, top=3)
        print(f"✅ ผลค้นหาข้อมูลการขายจาก RAG: {sales_data}")

        # 🔹 **สร้างข้อความ Context สำหรับ AI**
        # prompt = "นี่คือประวัติการสนทนาเดิมของคุณ:\n"
        # prompt += "\n".join(chat_history)
        # prompt += f"\n\nข้อมูลการขายที่เกี่ยวข้อง:\n"
        # prompt += "\n".join(sales_data)
        # prompt += f"\n\nผู้ใช้: {user_message}\nAI:"
        prompt = """
                คุณคือ AI ที่ช่วยเหลือในการค้นหาข้อมูล ให้ความสำคัญกับบริบทเดิมของการสนทนามากกว่าข้อมูลใหม่
                1️⃣ **บทสนทนาเดิมที่เคยคุยกับผู้ใช้** (สำคัญที่สุด)
                2️⃣ **ข้อมูลที่ค้นพบจากระบบ Sales** (อ้างอิงเพิ่มเติม)
                3️⃣ **ข้อความที่ผู้ใช้ถามล่าสุด**

                กรุณาตอบโดยเน้นให้ความสำคัญกับบทสนทนาเดิมก่อน

                🔹 **ประวัติการสนทนาเดิม**
                {}
                🔹 **ข้อมูลจากระบบ Sales**
                {}
                🔹 **ข้อความที่ผู้ใช้ถาม**
                {}
                """.format("\n".join(chat_history), "\n".join(sales_data), user_message)


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

# 🔹 **ฟังก์ชันลบบทสนทนาเก่าของผู้ใช้จาก Azure Cognitive Search**
def delete_chat_history(user_id):
    try:
        results = chat_history_client.search(search_text="*", filter=f"user_id eq '{user_id}'")
        document_ids = [result["id"] for result in results]

        if document_ids:
            chat_history_client.delete_documents(documents=[{"id": doc_id} for doc_id in document_ids])
            print(f"✅ ลบบทสนทนาทั้งหมดของ {user_id} สำเร็จ!")
        else:
            print(f"❌ ไม่พบข้อมูลสนทนาเก่าของ {user_id}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการลบประวัติการสนทนา: {e}")



# 🔹 ค้นหาประวัติการสนทนา

def search_chat_history(user_id, query, top=5):
    try:
        results = chat_history_client.search(
            search_text="*", filter=f"user_id eq '{user_id}'", top=top
        )
        chat_history = sorted(
            [{"message": result["message"], "timestamp": result.get("timestamp", "")} for result in results],
            key=lambda x: x["timestamp"], reverse=True
        )
        return [entry["message"] for entry in chat_history] if chat_history else []
    except Exception as e:
        print(f"❌ Error fetching chat history: {e}")
        return []
    

def get_combined_user_history(user_id, user_message, top=5):
    try:
        # ดึงประวัติข้อความที่ user เคยถาม
        user_history = search_chat_history_User(user_id, query="*", top=top)

        # รวมข้อความ user_message เข้าไปในประวัติ
        if user_history:
            combined_text = f"{user_history}\n{user_message}"
        else:
            combined_text = user_message  # ถ้าไม่มีประวัติ ให้ใช้ user_message อย่างเดียว

        return combined_text
    except Exception as e:
        print(f"❌ Error combining chat history: {e}")
        return user_message  # คืน user_message ถ้ามี error

    
def search_chat_history_User(user_id, query, top=5):
    try:
        results = chat_history_client.search(
            search_text="*", filter=f"user_id eq '{user_id}'", top=top
        )
        chat_history = sorted(
            [{"message": result["message"], "timestamp": result.get("timestamp", "")} for result in results],
            key=lambda x: x["timestamp"], reverse=True
        )
        
        # กรองเฉพาะข้อความที่เป็นของ User เท่านั้น
        user_questions = [entry["message"] for entry in chat_history if entry["message"].startswith("User:")]

        # รวมกันเป็นข้อความเดียว (แต่ละบรรทัดคั่นด้วย \n)
        return "\n".join(user_questions) if user_questions else None
    except Exception as e:
        print(f"❌ Error fetching chat history: {e}")
        return None


# 🔹 บันทึกข้อความลง Azure Cognitive Search

def save_chat(user_id, message):
    document = {
        "id": f"{user_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message
    }
    try:
        chat_history_client.upload_documents(documents=[document])
        print(f"✅ บันทึกข้อความสำเร็จ: {document}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการบันทึก: {e}")





# 🔹 ค้นหาข้อมูลการขายจาก RAG

# def search_sales_data(query, top=3):
#     results = sales_data_client.search(search_text=query, top=top)
#     return [result["chunk"] for result in results] if results else ["ไม่พบข้อมูลการขายที่เกี่ยวข้อง"]
def search_sales_data(query, top=3):
    try:
        results = sales_data_client.search(search_text=query, top=top)
        return [result["chunk"] for result in results] if results else ["ไม่พบข้อมูลการขายที่เกี่ยวข้อง"]
    except Exception as e:
        print(f"❌ Error fetching sales data: {e}")
        return ["เกิดข้อผิดพลาดในการค้นหาข้อมูล"]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
