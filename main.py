import logging
import re
import json
import os
import sys
from datetime import datetime
from io import BytesIO

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
# 1. Get Token from Environment (GitHub Secrets)
TOKEN = os.getenv("BOT_TOKEN")

# 2. Safety Check: If token is missing, stop immediately.
if not TOKEN:
    print("CRITICAL ERROR: 'BOT_TOKEN' not found in environment variables.")
    print("If running on GitHub: Go to Settings > Secrets > Actions and add 'BOT_TOKEN'.")
    sys.exit(1)

STORAGE_FILE = 'user_files.json'

# 3. Enable detailed logging (Helps debugging)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- 1. CLOUD STORAGE LOGIC ---
def load_files():
    if not os.path.exists(STORAGE_FILE):
        return {}
    try:
        with open(STORAGE_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def save_file_record(user_id, file_name, file_id):
    data = load_files()
    user_id = str(user_id)
    if user_id not in data:
        data[user_id] = []
    
    # Add file to user's list
    data[user_id].append({
        'name': file_name, 
        'file_id': file_id, 
        'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    
    with open(STORAGE_FILE, 'w') as f:
        json.dump(data, f)

# --- 2. REMINDER LOGIC (Fixed) ---
async def alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the alarm message."""
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"⏰ **REMINDER:** {job.data}")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parses messages like 'Remind me to [action] in [number] seconds/minutes'"""
    # Print to console for debugging
    print(f"Received text: {update.message.text}")
    
    message = update.message.text.lower()
    chat_id = update.effective_message.chat_id

    # Regex: Matches "remind me to [task] in [number] [unit]"
    # Example: "Remind me to sleep in 5 seconds"
    match = re.search(r'remind me to (.+) in (\d+) (second|minute|hour)', message)

    if match:
        task = match.group(1)
        amount = int(match.group(2))
        unit = match.group(3)

        # Calculate delay
        delay = amount
        if 'minute' in unit:
            delay *= 60
        elif 'hour' in unit:
            delay *= 3600

        if context.job_queue:
            context.job_queue.run_once(alarm, delay, chat_id=chat_id, name=str(chat_id), data=task)
            await update.message.reply_text(f"✅ Timer set! I will remind you to '{task}' in {amount} {unit}(s).")
        else:
            await update.message.reply_text("❌ Error: JobQueue not active. Check requirements.txt")
    else:
        # If it's not a reminder, treat it as an unknown command
        await update.message.reply_text(
            "❓ **I didn't understand that.**\n\n"
            "**Try these:**\n"
            "1️⃣ Send me a **Photo** to convert it.\n"
            "2️⃣ Send me a **File** to save it.\n"
            "3️⃣ Type: `Remind me to drink water in 10 seconds`"
        )

# --- 3. FILE CONVERTER LOGIC ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    print("Received a file/photo...")

    # Identify File Type
    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name
        file_id = file_obj.file_id
        is_image = file_obj.mime_type and file_obj.mime_type.startswith('image')
    elif update.message.photo:
        file_obj = update.message.photo[-1] # Largest size
        file_name = f"photo_{int(datetime.now().timestamp())}.jpg"
        file_id = file_obj.file_id
        is_image = True
    else:
        return

    # Save Metadata
    save_file_record(user_id, file_name, file_id)
    
    # If it's an image, convert it
    if is_image:
        status_msg = await update.message.reply_text("🔄 Image detected. Converting...")
        
        try:
            # Download
            new_file = await context.bot.get_file(file_id)
            f = BytesIO()
            await new_file.download_to_memory(out=f)
            f.seek(0)
            
            image = Image.open(f)
            output_stream = BytesIO()
            
            # Convert Logic
            if image.format == 'PNG':
                image = image.convert('RGB')
                image.save(output_stream, format='JPEG')
                new_filename = "converted.jpg"
                caption = "Here is your JPG!"
            else:
                image.save(output_stream, format='PNG')
                new_filename = "converted.png"
                caption = "Here is your PNG!"
            
            output_stream.seek(0)
            await update.message.reply_document(document=output_stream, filename=new_filename, caption=caption)
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=status_msg.message_id)
        
        except Exception as e:
            print(f"Conversion Error: {e}")
            await update.message.reply_text("❌ Failed to convert image.")
    else:
        await update.message.reply_text(f"💾 File '{file_name}' saved to cloud! (Use /myfiles to view)")

# --- 4. LIST FILES COMMAND ---
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    data = load_files()
    
    if user_id in data and data[user_id]:
        msg = "📂 **Your Saved Files:**\n\n"
        for idx, file in enumerate(data[user_id]):
            msg += f"{idx+1}. {file['name']} \n   📅 {file['date']}\n"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("📭 You have no saved files.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Start command received")
    await update.message.reply_text(
        "👋 **Hello! I am SuperBot.**\n\n"
        "**I can do 3 things:**\n"
        "1. **Convert Images:** Send me a JPG or PNG.\n"
        "2. **Save Files:** Send any file to store it.\n"
        "3. **Reminders:** Type 'Remind me to [task] in [time] seconds'."
    )

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    print("🚀 Starting Bot...")
    
    # Initialize Application
    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myfiles", list_files))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_document))
    application.add_handler(MessageHandler(filters.Document.ALL & (~filters.Document.IMAGE), handle_document)) # Handle non-image files
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), set_reminder))

    print("✅ Bot is polling...")
    application.run_polling()
