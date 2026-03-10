import os
import json
import re
import base64
import sqlite3
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

app = Flask(__name__)

# ===== 環境変数から設定を読み込む =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ===== データベースの初期化 =====
def init_db():
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    # リマインダーテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        event_name TEXT,
        remind_at TEXT,
        sent INTEGER DEFAULT 0
    )''')
    # 確認待ちテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS pending (
        user_id TEXT PRIMARY KEY,
        event_name TEXT,
        remind_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()


# ===== スケジューラー（毎分チェックしてリマインダーを送る） =====
def check_and_send_reminders():
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute("SELECT id, user_id, event_name FROM reminders WHERE remind_at <= ? AND sent = 0", (now,))
    reminders = c.fetchall()
    for rid, user_id, event_name in reminders:
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"🔔 リマインダー！\n「{event_name}」の時間です！\n楽しんできてください😊")
            )
            c.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (rid,))
            print(f"Reminder sent: {event_name} to {user_id}")
        except Exception as e:
            print(f"Error sending reminder: {e}")
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(check_and_send_reminders, 'interval', minutes=1)
scheduler.start()


# ===== LINEのWebhookエンドポイント =====
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


# ===== 画像メッセージを受け取った時の処理 =====
@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    message_id = event.message.id

    # まず「分析中」と返信
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="📸 画像を分析中です...\n少々お待ちください⏳")
    )

    # 画像をダウンロード
    message_content = line_bot_api.get_message_content(message_id)
    image_data = b''
    for chunk in message_content.iter_content():
        image_data += chunk
    image_base64 = base64.b64encode(image_data).decode('utf-8')

    # OpenAI GPT-4oで画像を解析
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """この画像からイベント情報を抽出してください。
以下のJSON形式だけで返してください（説明文は不要）：
{
  "found": true,
  "event_name": "イベント名",
  "event_date": "YYYY-MM-DD",
  "event_time": "HH:MM"
}
日付が見つからない場合: {"found": false}
・年が書いていない場合は2026年を使用
・時間が書いていない場合は"09:00"を使用"""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )

        result_text = response.choices[0].message.content.strip()

        # JSONを取り出す
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = {"found": False}

        if data.get("found"):
            event_name = data.get("event_name", "イベント")
            event_date = data.get("event_date", "")
            event_time = data.get("event_time", "09:00")
            remind_at = f"{event_date} {event_time}"

            # 確認待ちに保存
            conn = sqlite3.connect('reminders.db')
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO pending VALUES (?, ?, ?)",
                      (user_id, event_name, remind_at))
            conn.commit()
            conn.close()

            # ユーザーに確認メッセージを送る
            confirm_msg = (
                f"📅 イベントを検出しました！\n\n"
                f"イベント名：{event_name}\n"
                f"日時：{event_date} {event_time}\n\n"
                f"このリマインダーを設定しますか？\n"
                f"👉「はい」または「いいえ」で答えてください"
            )
            line_bot_api.push_message(user_id, TextSendMessage(text=confirm_msg))

        else:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="⚠️ 画像から日付を見つけられませんでした。\n別の画像を試してみてください。")
            )

    except Exception as e:
        print(f"Error analyzing image: {e}")
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text="❌ エラーが発生しました。もう一度試してください。")
        )


# ===== テキストメッセージを受け取った時の処理 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute("SELECT event_name, remind_at FROM pending WHERE user_id = ?", (user_id,))
    pending = c.fetchone()

    YES_WORDS = ["はい", "yes", "YES", "はい！", "OK", "ok", "オーケー", "する", "お願い"]
    NO_WORDS = ["いいえ", "no", "NO", "キャンセル", "やめる", "しない"]

    if pending and text in YES_WORDS:
        event_name, remind_at = pending
        c.execute("INSERT INTO reminders (user_id, event_name, remind_at) VALUES (?, ?, ?)",
                  (user_id, event_name, remind_at))
        c.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"✅ リマインダーを設定しました！\n\n"
                     f"📌 {event_name}\n"
                     f"⏰ {remind_at}\n\n"
                     f"時間になったらお知らせします🔔"
            )
        )

    elif pending and text in NO_WORDS:
        c.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="キャンセルしました。\n別の画像があればいつでも送ってください！")
        )

    else:
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="こんにちは！📅\n\nチラシや予定表の画像を送ってください。\n日付を読み取ってリマインダーを設定します！"
            )
        )


# ===== アプリ起動 =====
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
