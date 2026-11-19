import os
import requests
from flask import Flask, request, abort

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15
    )


def get_btc_price():
    url = "https://apiv2.nobitex.ir/market/stats"
    params = {
        "srcCurrency": "btc",
        "dstCurrency": "usdt"
    }

    response = requests.get(url, params=params, timeout=15)
    data = response.json()

    stats = data.get("stats", {})
    market = stats.get("btc-usdt", {})

    price = market.get("latest") or market.get("bestBuy")

    if price:
        return price

    return "قیمت در حال حاضر در دسترس نیست."


@app.route("/health")
def health():
    return "Soheil AI Trader is running!"


@app.route("/telegram", methods=["POST"])
def telegram():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    update = request.get_json(silent=True) or {}
    message = update.get("message", {})

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id:
        return "OK"

    if text == "/start":
        send_message(
            chat_id,
            "سلام! به Soheil AI Trader خوش آمدی.\n"
            "برای مشاهده قیمت بیت‌کوین دستور /price را بفرست."
        )

    elif text == "/price":
        try:
            price = get_btc_price()
            send_message(chat_id, f"قیمت BTC/USDT:\n{price}")
        except Exception:
            send_message(chat_id, "خطا در دریافت قیمت از نوبیتکس.")

    elif text == "/status":
        send_message(chat_id, "ربات فعال است. حالت فعلی: آزمایشی.")

    elif text == "/help":
        send_message(
            chat_id,
            "دستورات ربات:\n"
            "/start - شروع\n"
            "/price - قیمت بیت‌کوین\n"
            "/status - وضعیت ربات\n"
            "/help - راهنما"
        )

    return "OK"


if BOT_TOKEN and WEBHOOK_URL and WEBHOOK_SECRET:
    try:
        requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": f"{WEBHOOK_URL.rstrip('/')}/telegram",
                "secret_token": WEBHOOK_SECRET
            },
            timeout=15
        )
    except Exception:
        pass