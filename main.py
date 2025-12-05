#!/usr/bin/env python3
"""
SuperBot (improved)
- File converter (JPG <-> PNG)
- Cloud storage (simple JSON)
- Reminders with persistence, list and cancel
"""

import logging
import re
import json
import os
import tempfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import asyncio
import uuid

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from PIL import Image  # Pillow

# --------------- CONFIG ---------------
DEFAULT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # fallback if env var not set
TOKEN = os.getenv("BOT_TOKEN", DEFAULT_TOKEN)

STORAGE_FILE = Path("user_files.json")
REMINDERS_FILE = Path("reminders.json")

# limits
MAX_REMINDER_SECONDS = 60 * 60 * 24 * 365  # 1 year
MIN_REMINDER_SECONDS = 1  # at least 1 second

# logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --------------- UTIL: atomic json write ---------------
def atomic_write_json(path: Path, data):
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent))
    try:
        with open(tmp.name, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp.name, str(path))
    finally:
        if os.path.exists(tmp.name):
            try:
                os.remove(tmp.name)
            except Exception:
                pass


# --------------- STORAGE: files ---------------
def load_files():
    if not STORAGE_FILE.exists():
        return {}
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed reading storage file; returning empty")
        return {}


def save_file_record(user_id, file_name, file_id):
    data = load_files()
    user_key = str(user_id)
    if user_key not in data:
        data[user_key] = []
    data[user_key].append({"id": str(uuid.uuid4()), "name": file_name, "file_id": file_id, "date": datetime.utcnow().isoformat()})
    atomic_write_json(STORAGE_FILE, data)


# --------------- REMINDERS: persistence + management ---------------
def load_reminders():
    if not REMINDERS_FILE.exists():
        return {}
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed reading reminders file; returning empty")
        return {}


def save_reminders(data):
    atomic_write_json(REMINDERS_FILE, data)


async def alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    data = job.data or {}
    chat_id = job.chat_id
    text = data.get("task", "Reminder!")
    rid = data.get("id")
    try:
        await context.bot.send_message(chat_id, text=f"⏰ REMINDER: {text}\n(ID: {rid})")
    except Exception:
        logger.exception("Failed sending reminder message")

    # remove reminder from persisted storage after firing
    reminders = load_reminders()
    if str(rid) in reminders:
        del reminders[str(rid)]
        save_reminders(reminders)


def schedule_reminder_in_app(context, job_queue, reminder_obj):
    """
    reminder_obj keys:
      id, chat_id, user_id, task, when_ts (epoch seconds)
    """
    now_ts = int(datetime.utcnow().timestamp())
    delay = reminder_obj["when_ts"] - now_ts
    if delay <= 0:
        # If time already passed, schedule immediately (1 second)
        delay = 1
    if delay > MAX_REMINDER_SECONDS:
        logger.warning("Refusing to schedule reminder > max")
        return None
    job = job_queue.run_once(alarm, delay, chat_id=reminder_obj["chat_id"], name=str(reminder_obj["id"]), data={"task": reminder_obj["task"], "id": reminder_obj["id"]})
    return job


# --------------- REMINDER PARSING ---------------
TIME_RE = re.compile(
    r"\b(in\s*)?(?P<num>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\b",
    flags=re.IGNORECASE,
)


def parse_delay_from_text(text: str):
    """
    Returns (seconds, matched_text) or (None, None)
    Accepts formats like:
      - in 10 seconds
      - 10s / 5min / 2 hours
      - remind me to X in 5 minutes
    """
    text = text.strip().lower()
    m = TIME_RE.search(text)
    if not m:
        return None, None
    num = int(m.group("num"))
    unit = m.group("unit").lower()

    seconds = num
    if unit.startswith("s"):
        seconds = num
    elif unit.startswith("m"):
        seconds = num * 60
    elif unit.startswith("h"):
        seconds = num * 3600
    elif unit.startswith("d"):
        seconds = num * 86400

    # safety clamp
    if seconds > MAX_REMINDER_SECONDS:
        return None, None
    if seconds < MIN_REMINDER_SECONDS:
        return MIN_REMINDER_SECONDS, m.group(0)
    return seconds, m.group(0)


# --------------- HANDLERS ---------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to SuperBot!*\n\n"
        "I can do:\n"
        "• Send images to convert JPG ⇄ PNG and store them.\n"
        "• Schedule reminders.\n\n"
        "Usage examples:\n"
        "• `Remind me to drink water in 10 seconds` (natural)\n"
        "• `/remind 10m drink water` (command)\n"
        "Commands:\n"
        "• /remind <time> <task>  — create reminder (e.g. /remind 5m call mom)\n"
        "• /reminders — list your pending reminders\n"
        "• /cancel <id> — cancel a reminder by id\n"
        "• /myfiles — list your saved files\n",
        parse_mode="Markdown",
    )


# Command: /remind 10m do something
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /remind <time> <task>  e.g. /remind 10s drink water")
        return

    # try first token as time
    first = " ".join(args[:2]) if len(args) >= 2 else args[0]
    # try flexible parse: either parse first arg or combined first two (like '10 min')
    seconds, matched = parse_delay_from_text(first)
    if seconds is None:
        # fallback: try full text search for time like "in 10 seconds"
        full = update.message.text
        seconds, matched = parse_delay_from_text(full)
    if seconds is None:
        await update.message.reply_text("Couldn't parse time. Use formats like '10s', '5min', '2 hours', or 'in 10 seconds'.")
        return

    # build task text: what's left after matched time
    full_text = update.message.text
    # remove the command part if present
    content_after = full_text.partition(" ")[2] if " " in full_text else ""
    # remove the matched time snippet
    task_text = re.sub(re.escape(matched), "", content_after, flags=re.IGNORECASE).strip()
    if not task_text:
        task_text = "Reminder"

    # compute when (epoch seconds)
    when_ts = int(datetime.utcnow().timestamp()) + int(seconds)

    # persist reminder
    rid = str(uuid.uuid4())
    rem = {
        "id": rid,
        "chat_id": chat_id,
        "user_id": str(user.id),
        "task": task_text,
        "when_ts": when_ts,
        "created_at": datetime.utcnow().isoformat(),
    }
    reminders = load_reminders()
    reminders[rid] = rem
    save_reminders(reminders)

    # schedule
    schedule_reminder_in_app(context, context.job_queue, rem)

    # human-friendly display
    when_dt = datetime.utcfromtimestamp(when_ts).isoformat() + "Z"
    await update.message.reply_text(f"✅ Reminder set (ID: `{rid}`)\nTask: {task_text}\nWhen (UTC): {when_dt}\nDelay: {seconds} seconds", parse_mode="Markdown")


# Text handler: natural language reminders like "remind me to X in 10 seconds"
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    chat_id = update.effective_chat.id

    # try to match pattern "remind me to (task) in 10 seconds"
    m = re.search(r"remind me to (.+) in (\d+\s*(?:s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours|d|day|days))", message, flags=re.IGNORECASE)
    if m:
        task = m.group(1).strip()
        time_part = m.group(2)
        seconds, _ = parse_delay_from_text(time_part)
        if seconds is None:
            await update.message.reply_text("Couldn't parse the time in your message. Try: 'Remind me to drink water in 10 seconds'.")
            return

        when_ts = int(datetime.utcnow().timestamp()) + int(seconds)
        rid = str(uuid.uuid4())
        rem = {"id": rid, "chat_id": chat_id, "user_id": str(update.effective_user.id), "task": task, "when_ts": when_ts, "created_at": datetime.utcnow().isoformat()}
        reminders = load_reminders()
        reminders[rid] = rem
        save_reminders(reminders)

        schedule_reminder_in_app(context, context.job_queue, rem)
        await update.message.reply_text(f"✅ I will remind you to '{task}' in {seconds} seconds. (ID: `{rid}`)", parse_mode="Markdown")
        return

    # fallback: maybe they asked with other phrasing, try to extract any time mention
    seconds, match = parse_delay_from_text(message)
    if seconds:
        # the task will be the original message with time snippet removed
        task_text = re.sub(re.escape(match), "", message, flags=re.IGNORECASE).strip()
        if not task_text:
            task_text = "Reminder"
        when_ts = int(datetime.utcnow().timestamp()) + int(seconds)
        rid = str(uuid.uuid4())
        rem = {"id": rid, "chat_id": chat_id, "user_id": str(update.effective_user.id), "task": task_text, "when_ts": when_ts, "created_at": datetime.utcnow().isoformat()}
        reminders = load_reminders()
        reminders[rid] = rem
        save_reminders(reminders)

        schedule_reminder_in_app(context, context.job_queue, rem)
        await update.message.reply_text(f"✅ Reminder set: '{task_text}' in {seconds} seconds. (ID: `{rid}`)", parse_mode="Markdown")
        return

    # Not a reminder — pass to other functionality suggestion
    await update.message.reply_text("Unknown command. Try:\n• Send an image to convert.\n• 'Remind me to drink water in 5 seconds'\n• /myfiles to see stored files.")


# List reminders for user
async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    reminders = load_reminders()
    user_rems = [r for r in reminders.values() if r.get("user_id") == user_id]
    if not user_rems:
        await update.message.reply_text("You have no pending reminders.")
        return
    msg_lines = ["⏳ *Your Pending Reminders:*"]
    now_ts = int(datetime.utcnow().timestamp())
    for r in sorted(user_rems, key=lambda x: x["when_ts"]):
        remain = r["when_ts"] - now_ts
        when = datetime.utcfromtimestamp(r["when_ts"]).isoformat() + "Z"
        msg_lines.append(f"- ID: `{r['id']}` | in {remain}s | at {when} | {r['task']}")
    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")


# Cancel reminder by id
async def cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <reminder_id>")
        return
    rid = context.args[0]
    reminders = load_reminders()
    if rid not in reminders:
        await update.message.reply_text("No reminder found with that ID.")
        return
    # try to remove job from job queue
    try:
        jobs = context.job_queue.get_jobs_by_name(rid)
        for j in jobs:
            j.schedule_removal()
    except Exception:
        logger.warning("Failed to remove job by name (maybe already executed)")

    # remove from persisted store
    del reminders[rid]
    save_reminders(reminders)
    await update.message.reply_text(f"✅ Cancelled reminder `{rid}`", parse_mode="Markdown")


# --------------- FILE HANDLING (images & docs) ---------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # Determine if it's a document or a photo
    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name or f"doc_{int(datetime.utcnow().timestamp())}"
        file_id = file_obj.file_id
        is_image = file_obj.mime_type and file_obj.mime_type.startswith("image")
    elif update.message.photo:
        file_obj = update.message.photo[-1]  # best quality
        file_name = f"photo_{int(datetime.utcnow().timestamp())}.jpg"
        file_id = file_obj.file_id
        is_image = True
    else:
        return

    # Save record
    save_file_record(user_id, file_name, file_id)
    await update.message.reply_text(f"💾 File '{file_name}' saved to your cloud storage!")

    # If image: convert
    if is_image:
        status_msg = await update.message.reply_text("🔄 Detect image... Converting format...")
        try:
            new_file = await context.bot.get_file(file_id)
            f = BytesIO()
            await new_file.download_to_memory(out=f)
            f.seek(0)
            image = Image.open(f)
            output_stream = BytesIO()
            # Convert
            if image.format and image.format.upper() == "PNG":
                image = image.convert("RGB")
                image.save(output_stream, format="JPEG")
                new_filename = file_name.rsplit(".", 1)[0] + "_converted.jpg"
            else:
                image.save(output_stream, format="PNG")
                new_filename = file_name.rsplit(".", 1)[0] + "_converted.png"
            output_stream.seek(0)
            await update.message.reply_document(document=output_stream, filename=new_filename, caption="Here is your converted file!")
            # delete status message
            try:
                await context.bot.delete_message(chat_id=update.message.chat_id, message_id=status_msg.message_id)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Image conversion failed")
            await update.message.reply_text(f"❌ Could not convert image: {e}")


# List stored files for user
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    data = load_files()
    if user_id in data and data[user_id]:
        msg = "📂 *Your Saved Files:*\n"
        for idx, file in enumerate(data[user_id]):
            msg += f"{idx+1}. {file['name']} (Saved: {file['date']})\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("You have no files saved yet. Send me a file to start!")


# --------------- STARTUP: reschedule persisted reminders ---------------
async def reschedule_persisted_reminders(app):
    reminders = load_reminders()
    if not reminders:
        logger.info("No persisted reminders to reschedule.")
        return
    logger.info("Rescheduling persisted reminders...")
    jobs_added = 0
    for rid, r in list(reminders.items()):
        try:
            # if time already passed by long, we still schedule immediate run
            schedule_reminder_in_app(app, app.job_queue, r)
            jobs_added += 1
        except Exception:
            logger.exception("Failed to reschedule reminder %s", rid)
    logger.info("Rescheduled %d reminders", jobs_added)


# --------------- MAIN ---------------
def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE" or not TOKEN:
        logger.error("Please set the BOT_TOKEN environment variable or put token in DEFAULT_TOKEN")
        return

    application = ApplicationBuilder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remind", cmd_remind))
    application.add_handler(CommandHandler("reminders", list_reminders))
    application.add_handler(CommandHandler("cancel", cancel_reminder))
    application.add_handler(CommandHandler("myfiles", list_files))

    # File & image handler
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_document))

    # Text handler (natural language reminders & fallback)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), set_reminder))

    # startup job to reschedule persisted reminders
    async def _on_startup(app):
        await reschedule_persisted_reminders(app)

    application.post_init = _on_startup

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
