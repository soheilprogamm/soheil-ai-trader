import os
import json
import time
import urllib.request
import urllib.parse

TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

API = f"https://api.telegram.org/bot{TOKEN}/"

def telegram(method, data=None):
    url = API + method
    payload = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(url, data=payload)

    with urllib.request.urlopen(req, timeout=40) as response:
        return json.loads(response.read())

def send(chat_id, text):
    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text
    })

def get_btc_price():
    url = (
        "https://apiv2.nobitex.ir/market/stats"
        "?srcCurrency=btc&dstCurrency=usdt"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SoheilAITrader/1.0"}
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read())

    return float(data["stats"]["btc-usdt"]["latest"])

def handle(chat_id, command):

    if command == "/start":
        send(
            chat_id,
            "🤖 به Soheil AI Trader خوش آمدی!\n\n"
            "ربات آماده است.\n"
            "حالت فعلی: آزمایشی\n"
            "معاملات واقعی: غیرفعال\n\n"
            "/price - قیمت بیت کوین\n"
            "/status - وضعیت ربات\n"
            "/help - راهنما"
        )

    elif command == "/price":
        try:
            price = get_btc_price()
            send(
                chat_id,
                f"📊 قیمت لحظه‌ای BTC/USDT:\n"
                f"{price:,.2f} USDT"
            )
        except Exception as error:
            send(chat_id, f"خطا در دریافت قیمت: {error}")

    elif command == "/status":
        send(
            chat_id,
            "🟢 Soheil AI Trader فعال است.\n"
            "حالت: Paper Trading\n"
            "معاملات واقعی: خاموش"
        )

    else:
        send(
            chat_id,
            "دستورات:\n/start\n/price\n/status\n/help"
        )

def main():
    print("Soheil AI Trader started")
    offset = None

    while True:
        try:
            params = {"timeout": 30}

            if offset is not None:
                params["offset"] = offset

            result = telegram("getUpdates", params)

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                text = message.get("text", "")

                if chat_id and text.startswith("/"):
                    handle(chat_id, text.split()[0])

        except Exception as error:
            print("Error:", error)
            time.sleep(5)

if __name__ == "__main__":
    main()
