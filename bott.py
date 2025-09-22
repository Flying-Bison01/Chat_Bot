# file: numinfo_bot.py
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any

import phonenumbers
from phonenumbers import geocoder, carrier, timezone as tzlib

from telegram import Update, Bot, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === Config ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
BACKUP_DIR = "./backups"
LOG_FILE = "bot.log"

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === in-memory store of lookups (simple) ===
LOOKUP_HISTORY = []  # list of dicts

# === Helpers ===
def save_lookup_record(record: Dict[str, Any]):
    LOOKUP_HISTORY.append(record)
    # keep size limited
    if len(LOOKUP_HISTORY) > 2000:
        LOOKUP_HISTORY.pop(0)

def format_lookup_result(number_str: str, parsed, info: Dict[str, Any]) -> str:
    lines = [
        f"🔎 Lookup result for: `{number_str}`",
        f"• E.164: `{phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)}`",
        f"• International: `{phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}`",
        f"• Country/Region: {info.get('region', 'Unknown')}",
        f"• Valid: {'Yes' if info.get('valid') else 'No'}",
    ]
    carrier_name = info.get("carrier")
    if carrier_name:
        lines.append(f"• Carrier: {carrier_name}")
    tzs = info.get("timezones")
    if tzs:
        lines.append(f"• Timezone(s): {', '.join(tzs)}")
    possible = info.get("possible")
    if possible is not None:
        lines.append(f"• Possible: {'Yes' if possible else 'No'}")
    return "\n".join(lines)

def lookup_number(number_str: str, default_region: str = None) -> Dict[str, Any]:
    """
    Parse and get metadata for the phone number using python-phonenumbers.
    default_region: ISO 3166-1 alpha-2 (e.g., 'IN') used for ambiguous numbers.
    """
    result: Dict[str, Any] = {"input": number_str}
    try:
        parsed = phonenumbers.parse(number_str, default_region)
    except phonenumbers.NumberParseException as e:
        result.update({
            "error": str(e),
            "valid": False
        })
        return result

    is_valid = phonenumbers.is_valid_number(parsed)
    is_possible = phonenumbers.is_possible_number(parsed)

    # geocoder (region name)
    try:
        region = geocoder.description_for_number(parsed, "en")
    except Exception:
        region = None

    # carrier
    try:
        carrier_name = carrier.name_for_number(parsed, "en")
    except Exception:
        carrier_name = None

    # timezone(s)
    try:
        tzs = tzlib.time_zones_for_number(parsed)
        tzs_list = list(tzs)
    except Exception:
        tzs_list = []

    result.update({
        "valid": is_valid,
        "possible": is_possible,
        "region": region,
        "carrier": carrier_name,
        "timezones": tzs_list,
        "e164": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164) if is_possible else None,
        "national_number": phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL),
    })
    return result

# === Bot Handlers ===
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi — I can look up phone numbers for you.\n\n"
        "Use /lookup <phone_number> (example: /lookup +919876543210 or /lookup 9876543210)\n"
        "Or just send me a phone number and I'll try to parse it.\n\n"
        "Commands:\n"
        "/lookup <number> - lookup number\n"
        "/backup - get a JSON backup of recent lookups\n"
        "/help - show this message"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Please provide a phone number. Example: `/lookup +919876543210`", parse_mode="Markdown")
        return
    number_str = " ".join(args).strip()
    # optional default region; if user groups from a country you can set default region
    default_region = None
    # try to infer default region from chat if you want (left None by default)
    info = lookup_number(number_str, default_region)
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "query": number_str,
        "result": info
    }
    save_lookup_record(record)

    if "error" in info:
        await update.message.reply_text(f"Error parsing number: {info['error']}")
        return
    parsed = info
    # re-parse to pass a parsed object for nice formatting
    try:
        parsed_obj = phonenumbers.parse(number_str, default_region)
    except Exception:
        parsed_obj = None

    text = format_lookup_result(number_str, parsed_obj, info)
    await update.message.reply_markdown(text)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If the user sends a plain message, try to detect a phone number
    text = (update.message.text or "").strip()
    if not text:
        return
    # crude: try lookup directly
    info = lookup_number(text, None)
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "query": text,
        "result": info
    }
    save_lookup_record(record)
    if "error" in info:
        # not a number — ignore or guide user
        # keep this gently helpful
        await update.message.reply_text("I couldn't parse that as a phone number. Try `/lookup +1234567890`.")
        return
    parsed_obj = None
    try:
        parsed_obj = phonenumbers.parse(text, None)
    except Exception:
        pass
    text_out = format_lookup_result(text, parsed_obj, info)
    await update.message.reply_markdown(text_out)

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Create backup dir
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = os.path.join(BACKUP_DIR, f"lookup_backup_{ts}.json")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(LOOKUP_HISTORY, f, ensure_ascii=False, indent=2)
        # send file back
        await update.message.reply_document(document=InputFile(filename), filename=os.path.basename(filename))
    except Exception as e:
        logger.exception("Failed to create/send backup")
        await update.message.reply_text(f"Failed to create backup: {e}")

# === Main ===
def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN env var not set. Exiting.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lookup", lookup_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))

    # handle all text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Starting bot...")
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])

if __name__ == "__main__":
    main()
