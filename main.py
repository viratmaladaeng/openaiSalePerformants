#main_BCK15
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
from linebot.models import VideoSendMessage


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
    password=REDIS_PASSWORD
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

        # 🔹 1. ส่ง GIF "กำลังพิมพ์..." ก่อน (Typing Indicator)


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
        sales_data = search_sales_data(search_query, top=10)

        print(f"✅ ค้นหาข้อมูลการขายจาก: {search_query}")  # Debugging
        print(f"✅ ผลค้นหาข้อมูลการขายจาก RAG: {sales_data}")  # Debugging


        # เพิ่ม system_message โดยไม่ทำให้กลายเป็น list ซ้อนกัน
        chat_history.append({"role": "system", "content": system_message})

        # สร้าง prompt
        prompt = f"\n\nsearch_sales_data:\n"
        prompt += "\n".join([str(item) for item in sales_data])  # ต้องมีข้อมูลนี้
        prompt += "\n\nchat_history:\n"
        prompt += "\n".join([json.dumps(item, ensure_ascii=False) for item in chat_history])  
        prompt += f"\n\nUser: {user_message}\n AI:"

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

   
def search_sales_data(query, top=10):  
    try:
        search_results = sales_data_client.search(search_text=query, top=top)

        results = []
        for result in search_results:
            filtered_result = {
                # 🔹 Sales Information
                "Sales_document_No": result.get("Sales_document", ""),
                "Sales_document_item_No": result.get("Sales_document_item", ""),
                "Billing_document_No": result.get("Billing_document", ""),
                "Billing_item_No": result.get("Billing_item", ""),
                "Sales_Order_Created_Date": result.get("Sales_Order_Created_Date", ""),
                "Delivery_document_No": result.get("Delivery", ""),
                "Billing_Date": result.get("Billing_Date", ""),
                "CalMonth": result.get("CalMonth", ""),
                "Sales_Organization": result.get("Sales_Organization", ""),
                "Distribution_Channel_Key": result.get("Distribution_Channel_Key", ""),
                "Distribution_Channel_Text": result.get("Distribution_Channel_Text", ""),
                "Sales_Code": result.get("Sales_Code", ""),
                "Selling_Unit": result.get("Selling_Unit", ""),
                
                # 🔹 Buyer Information
                "Buyer_Name": result.get("Buyer_Name", ""),
                "Buyer_Address": f"{result.get('Buyer_Address1', '')} {result.get('Buyer_Address2', '')} {result.get('Buyer_Address3', '')}".strip(),
                "Buyer_Zip_Code": result.get("Buyer_Zip_Code", ""),
                "Buyer_Phone": result.get("Buyer_Phone", ""),
                "Buyer_Mobile": result.get("Buyer_Mobile", ""),
                "Tax_No": result.get("Tax_No", ""),
                "Tax_No2": result.get("Tax_No2", ""),

                # 🔹 Recipient Information
                "Recipient": result.get("Recipient", ""),
                "Recipient_Address": f"{result.get('Recipient_Address1', '')} {result.get('Recipient_Address2', '')} {result.get('Recipient_Address3', '')}".strip(),
                "Recipient_Zip_Code": result.get("Recipient_Zip_Code", ""),
                "Recipient_Phone": result.get("Recipient_Phone", ""),
                "Recipient_Mobile": result.get("Recipient_Mobile", ""),

                # 🔹 Product Information
                "Material_Key": result.get("Material_Key", ""),
                "Sales_doc_type": result.get("Sales_doc_type", ""),
                "Quotation_Number_Sales_Rep_OSR_Text": result.get("Quotation_Number_Sales_Rep_OSR_Text", ""),
                "Project_Class": result.get("Project_Class", ""),
                "Ship_To_Party_Key": result.get("Ship_To_Party_Key", ""),
                "Sold_To_Sales_Sales_Office_ISR_1_Text": result.get("Sold_To_Sales_Sales_Office_ISR_1_Text", ""),
                
                "product_hierarchy_level_1": result.get("product_hierarchy_level_1", ""),
                "product_hierarchy_level_2": result.get("product_hierarchy_level_2", ""),
                "brand": result.get("brand", ""),
                "product_family": result.get("product_family", ""),
                "product_sub_family": result.get("product_sub_family", ""),

                # 🔹 Quotation & Pricing
                "Quotation_Number_Project_Owner_Key": result.get("Quotation_Number_Project_Owner_Key", ""),
                "Quotation_Number_Project_Owner_Text": result.get("Quotation_Number_Project_Owner_Text", ""),
                "Quantity_Purchased": result.get("Quantity_Purchased", ""),
                "Purchase_Value": result.get("Purchase_Value", ""),
                "Net_price": result.get("net_price", ""),
                "Total_price": result.get("list_price", "")
                
                # # 🔹 Combine Fields (ใช้ชื่อใหม่)
                # "combine_fields": f"{result.get('Sales_document', '')} | {result.get('Buyer_Name', '')} | "
                #                 f"{result.get('Buyer_Address1', '')} | {result.get('Buyer_Address2', '')} | "
                #                 f"{result.get('Buyer_Address3', '')} | {result.get('Sales_Organization', '')} | "
                #                 f"{result.get('brand', '')} | {result.get('product_family', '')} | "
                #                 f"{result.get('Quantity_Purchased', '')} | {result.get('net_price', '')}".strip()
            }
            
            results.append(filtered_result)  

        return results if results else ["ไม่พบข้อมูลการขายที่เกี่ยวข้อง"]
    
    except Exception as e:
        print(f"❌ Error fetching sales data: {e}")
        return ["เกิดข้อผิดพลาดในการค้นหาข้อมูล"]        

    
def save_chatRedis(user_id, message):
    """บันทึกข้อความสนทนาลง Redis"""
    chat_key = f"chat_history:{user_id}"
    
    # ดึงประวัติแชทเก่าจาก Redis
    chat_history = redis_client.get(chat_key)
    chat_history = json.loads(chat_history) if chat_history else []

    # เพิ่มข้อความใหม่ โดยใช้ str() แทน JSON ถ้าจำเป็น
    chat_history.append(str(message))

    # บันทึกกลับเข้า Redis พร้อมกำหนด TTL (เช่น 24 ชั่วโมง)
    redis_client.setex(chat_key, timedelta(hours=24), json.dumps(chat_history, ensure_ascii=False))
    
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

from linebot.models import VideoSendMessage




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
