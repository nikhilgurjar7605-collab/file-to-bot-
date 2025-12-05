import logging
import re
import json
import os
from datetime import datetime
from io import BytesIO

# --- NEW IMPORTS FOR GITHUB SECURITY ---
from dotenv import load_dotenv  
load_dotenv()  # This loads your .env file locally

# Telegram Libraries
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from PIL import Image

# --- CONFIGURATION ---
# The bot will look for a variable named "BOT_TOKEN"
TOKEN = os.getenv("BOT_TOKEN") 

if not TOKEN:
    print("Error: BOT_TOKEN not found! Make sure you have a .env file or set vars.")
    exit()

STORAGE_FILE = 'user_files.json'

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- 1. STORAGE SYSTEM ---
def load_files():
    if not os.path.exists(STORAGE_FILE):
        return {}
    with open(STORAGE_FILE, 'r') as f:
        return json.load(f)

def save_file_record(user_id, file_name, file_id):
    data = load_files()
    user_id = str(user_id)
    if user_id not in data:
        data[user_id] = []
    data[user_id].append({'name': file_name, 'file_id': file_id, 'date': str(datetime.now())})
    with open(STORAGE_FILE, 'w') as f:
        json.dump(data, f)

# --- 2. REMINDER LOGIC ---
async def alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"⏰ REMINDER: {job.data}")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.lower()
    chat_id = update.effective_message.chat_id
    match = re.search(r'remind me to (.+) in (\d+) (second|minute|hour)', message)

    if match:
        task = match.group(1)
        amount = int(match.group(2))
        unit = match.group(3)
        delay = amount
        if 'minute' in unit: delay *= 60
        elif 'hour' in unit: delay *= 3600

        context.job_queue.run_once(alarm, delay, chat_id=chat_id, name=str(chat_id), data=task)
        await update.message.reply_text(f"✅ I will remind you to '{task}' in {amount} {unit}(s)!")
    else:
        await update.message.reply_text("Unknown command. Try: 'Remind me to [task] in [time] seconds'")

# --- 3. FILE HANDLING ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name
        file_id = file_obj.file_id
        is_image = file_obj.mime_type and file_obj.mime_type.startswith('image')
    elif update.message.photo:
        file_obj = update.message.photo[-1]
        file_name = f"photo_{int(datetime.now().timestamp())}.jpg"
        file_id = file_obj.file_id
        is_image = True
    else:
        return

    save_file_record(user_id, file_name, file_id)
    await update.message.reply_text(f"💾 File '{file_name}' saved to cloud!")

    if is_image:
        status_msg = await update.message.reply_text("🔄 Converting format...")
        new_file = await context.bot.get_file(file_id)
        f = BytesIO()
        await new_file.download_to_memory(out=f)
        f.seek(0)

        try:
            image = Image.open(f)
            output_stream = BytesIO()
            if image.format == 'PNG':
                image = image.convert('RGB')
                image.save(output_stream, format='JPEG')
                new_filename = "converted.jpg"
            else:
                image.save(output_stream, format='PNG')
                new_filename = "converted.png"
            
            output_stream.seek(0)
            await update.message.reply_document(document=output_stream, filename=new_filename)
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=status_msg.message_id)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    data = load_files()
    if user_id in data and data[user_id]:
        msg = "📂 **Your Files:**\n" + "\n".join([f"- {f['name']}" for f in data[user_id]])
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("No files saved.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 I am SuperBot! Send me a file or set a reminder.")

if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myfiles", list_files))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), set_reminder))
    
    print("Bot is running...")
    application.run_polling()
