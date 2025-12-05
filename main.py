from telegram.helpers import mention_html

async def alarm(context: ContextTypes.DEFAULT_TYPE):
    """Send the reminder message with user mention."""
    job = context.job
    user = job.data["user"]
    task = job.data["task"]

    # Mention user in HTML format
    mention = mention_html(user["id"], user["name"])

    await context.bot.send_message(
        job.chat_id,
        text=f"⏰ Reminder for {mention}:\n<b>{task}</b>",
        parse_mode="HTML"
    )


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse messages like: Remind me to drink water in 10 seconds"""
    text = update.message.text.lower()
    chat_id = update.effective_message.chat_id
    user = update.effective_user

    match = re.search(r"remind me to (.+) in (\d+) (second|seconds|minute|minutes|hour|hours)", text)
    if not match:
        await update.message.reply_text("❌ Couldn't understand.\nExample:\n➡ Remind me to drink water in 10 seconds")
        return

    task = match.group(1)
    amount = int(match.group(2))
    unit = match.group(3)

    delay = amount
    if "minute" in unit:
        delay *= 60
    elif "hour" in unit:
        delay *= 3600

    # Make a UNIQUE job name per user + timestamp
    job_name = f"{user.id}_{datetime.now().timestamp()}"

    # Schedule job
    context.job_queue.run_once(
        alarm,
        delay,
        chat_id=chat_id,
        name=job_name,
        data={
            "task": task,
            "user": {"id": user.id, "name": user.first_name}
        }
    )

    await update.message.reply_text(
        f"✅ Reminder set!\nI will remind you in {amount} {unit}."
    )
