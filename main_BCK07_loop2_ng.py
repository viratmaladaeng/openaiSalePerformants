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
    user_message = event.message.text.strip()

    # 🔹 หากผู้ใช้พิมพ์ "เริ่มการสนทนาใหม่" หรือ "reset"
    if user_message.lower() in ["เริ่มการสนทนาใหม่", "reset"]:
        delete_chat_history(user_id)
        reply_message = "สนทนาใหม่เริ่มต้นแล้วค่ะ กรุณาพิมพ์คำถามของคุณ!"

    else:
        # 🔹 ตรวจสอบว่าผู้ใช้ต้องการค้นหารายการขายหรือไม่
        if user_message.isdigit():  
            sales_number = user_message  # ผู้ใช้ป้อนหมายเลขออเดอร์
            sales_data = search_sales_data(sales_number, top=5)  # ค้นหารายการทั้งหมด
            store_last_sales_query(user_id, sales_number, sales_data)  # เก็บข้อมูลใน session/cache
            reply_message = format_sales_summary(sales_number, sales_data)

        elif user_message.lower().startswith("ดูรายการ"):
            parts = user_message.split()
            if len(parts) == 2 and parts[1].isdigit():
                item_number = parts[1]
                last_query = get_last_sales_query(user_id)  # ดึงข้อมูลออเดอร์ที่ค้นหาก่อนหน้า

                if not last_query:
                    reply_message = "❌ ไม่พบหมายเลขออเดอร์ กรุณาระบุหมายเลขก่อน"
                else:
                    sales_number = last_query["sales_number"]
                    sales_summary = last_query["sales_data"]

                    # 🔹 หา Product Name หรือข้อมูลรายการที่เกี่ยวข้อง
                    product_info = next((item for item in sales_summary if str(item["item"]) == item_number), None)
                    if not product_info:
                        reply_message = f"❌ ไม่พบข้อมูลของรายการที่ {item_number} ในออเดอร์ {sales_number}"
                    else:
                        # 🔹 สร้าง Query ใหม่เพื่อค้นหารายละเอียดเพิ่มเติม
                        search_query = f"{sales_number} {product_info['product_name']} {product_info['quantity']}"
                        sales_details = search_sales_details(sales_number, item_number, search_query)
                        reply_message = format_sales_details(sales_details)
            else:
                reply_message = "❌ กรุณาระบุหมายเลขรายการให้ถูกต้อง เช่น 'ดูรายการ 3'"

        else:
            # 🔹 **ค้นหาประวัติการสนทนา**
            chat_history = search_chat_history(user_id, user_message, top=5)
            sales_data = search_sales_data(user_message, top=3)  # ค้นหาข้อมูลจาก Sales

            # 🔹 **สร้าง Context สำหรับ AI**
            prompt = f"""
            คุณคือ AI ที่ช่วยเหลือในการค้นหาข้อมูล ให้ความสำคัญกับบริบทเดิมของการสนทนามากกว่าข้อมูลใหม่
            1️⃣ **บทสนทนาเดิมที่เคยคุยกับผู้ใช้** (สำคัญที่สุด)
            2️⃣ **ข้อมูลที่ค้นพบจากระบบ Sales** (อ้างอิงเพิ่มเติม)
            3️⃣ **ข้อความที่ผู้ใช้ถามล่าสุด**

            กรุณาตอบโดยเน้นให้ความสำคัญกับบทสนทนาเดิมก่อน

            🔹 **ประวัติการสนทนาเดิม**
            {chat_history}

            🔹 **ข้อมูลจากระบบ Sales**
            {sales_data}

            🔹 **ข้อความที่ผู้ใช้ถาม**
            {user_message}
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
                "stream": False
            }

            try:
                response = requests.post(AZURE_OPENAI_ENDPOINT, headers=headers, json=payload, timeout=10)
                response.raise_for_status()
                openai_response = response.json()
                reply_message = openai_response["choices"][0]["message"]["content"]
            except requests.RequestException as e:
                print(f"❌ OpenAI API Error: {e}")
                reply_message = "ขออภัย ระบบมีปัญหาในการเชื่อมต่อกับ AI"

        # 🔹 **บันทึกการสนทนาเข้า Azure Cognitive Search**
        save_chat(user_id, user_message)
        save_chat(user_id, reply_message)

    # 🔹 **ส่งข้อความตอบกลับไปยัง Line**
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_message)
    )


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
def search_sales_data(query, sales_number=None, top=3):
    """ ค้นหาข้อมูลการขายจาก Azure Cognitive Search โดยใช้หมายเลขออเดอร์เป็นเงื่อนไข (ถ้ามี) """
    try:
        if sales_number:
            # 🔹 ค้นหาเฉพาะรายการที่อยู่ในหมายเลขออเดอร์ที่ระบุ
            filter_query = f"sales_doc eq '{sales_number}'"
        else:
            filter_query = None  # ค้นหาทั่วไปถ้าไม่มี sales_number

        results = sales_data_client.search(
            search_text=query, 
            filter=filter_query,  
            top=top
        )
        
        return [result["chunk"] for result in results] if results else ["ไม่พบข้อมูลการขายที่เกี่ยวข้อง"]

    except Exception as e:
        print(f"❌ Error fetching sales data: {e}")
        return ["เกิดข้อผิดพลาดในการค้นหาข้อมูล"]



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
