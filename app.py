import os
import json
import re
import base64
import ftplib
import io
import uuid
import threading
import time
import psycopg2
import psycopg2.extras
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, PostbackEvent,
    TextSendMessage, TemplateSendMessage, ButtonsTemplate,
    PostbackAction, DatetimePickerAction, ImageSendMessage)
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz

JST = pytz.timezone('Asia/Tokyo')

app = Flask(__name__)

# ===== 環境変数から設定を読み込む =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
XSERVER_FTP_PASSWORD = os.environ.get('XSERVER_FTP_PASSWORD')

# ===== Xserver FTP設定 =====
XSERVER_FTP_HOST = 'sv3112.xserver.jp'
XSERVER_FTP_USER = 'skateboard'
XSERVER_FTP_PATH = '/skateboard.xsrv.jp/public_html/InvitationClip/'
XSERVER_PUBLIC_URL = 'https://skateboard.xsrv.jp/InvitationClip/'

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ===== 画像をXserverにアップロード（バックグラウンドで実行） =====
def upload_image_to_xserver(image_data, callback):
    def _upload():
        try:
            filename = f"{uuid.uuid4().hex}.jpg"
            ftp = ftplib.FTP()
            ftp.connect(XSERVER_FTP_HOST, 21, timeout=15)
            ftp.login(XSERVER_FTP_USER, XSERVER_FTP_PASSWORD)
            ftp.set_pasv(True)

            # XserverのFTPルートはホームディレクトリ（/skateboard.xsrv.jp/）
            # なのでパスのうち先頭のドメイン部分を除いた相対パスで移動する
            # 例: /skateboard.xsrv.jp/public_html/InvitationClip/
            #   → ['skateboard.xsrv.jp', 'public_html', 'InvitationClip'] のうち
            #     '.' を含む先頭要素（ドメイン）をスキップして移動
            parts = [p for p in XSERVER_FTP_PATH.split('/') if p]
            # 先頭がドメイン名（.を含む）なら FTP ルート＝そこなのでスキップ
            if parts and '.' in parts[0]:
                parts = parts[1:]

            for part in parts:
                try:
                    ftp.cwd(part)
                except ftplib.error_perm:
                    try:
                        ftp.mkd(part)
                        ftp.cwd(part)
                    except Exception as e:
                        print(f"FTP mkdir error at '{part}': {e}")
                        callback(None)
                        ftp.quit()
                        return

            ftp.storbinary(f'STOR {filename}', io.BytesIO(image_data))
            ftp.quit()
            image_url = f"{XSERVER_PUBLIC_URL}{filename}"
            print(f"Image uploaded successfully: {image_url}")
            callback(image_url)
        except Exception as e:
            print(f"FTP upload error: {type(e).__name__}: {e}")
            callback(None)
    threading.Thread(target=_upload).start()


# ===== データベース接続 =====
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


# ===== データベースの初期化 =====
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        event_name TEXT,
        remind_at TEXT,
        image_url TEXT,
        sent INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending (
        user_id TEXT PRIMARY KEY,
        event_name TEXT,
        remind_at TEXT,
        state TEXT DEFAULT 'confirm',
        image_url TEXT
    )''')
    # 既存テーブルにimage_urlカラムがない場合は追加
    try:
        c.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS image_url TEXT")
        c.execute("ALTER TABLE pending ADD COLUMN IF NOT EXISTS image_url TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

for _i in range(5):
    try:
        init_db()
        print("DB initialized successfully.")
        break
    except Exception as _e:
        print(f"DB init error (attempt {_i + 1}/5): {_e}")
        time.sleep(3)


# ===== スケジューラー（毎分チェックしてリマインダーを送る） =====
def check_and_send_reminders():
    now = datetime.now(JST).strftime('%Y-%m-%d %H:%M')
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, user_id, event_name, image_url FROM reminders WHERE remind_at <= %s AND sent = 0", (now,))
    reminders = c.fetchall()
    for rid, user_id, event_name, image_url in reminders:
        try:
            messages = []
            # 画像があれば一緒に送る
            if image_url:
                messages.append(ImageSendMessage(
                    original_content_url=image_url,
                    preview_image_url=image_url
                ))
            messages.append(TextSendMessage(
                text=f"🔔 リマインダー！\n「{event_name}」の時間です！\n楽しんできてください😊"
            ))
            line_bot_api.push_message(user_id, messages)
            c.execute("UPDATE reminders SET sent = 1 WHERE id = %s", (rid,))
        except Exception as e:
            print(f"Error sending reminder: {e}")
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(check_and_send_reminders, 'interval', minutes=1)
scheduler.start()


# ===== 確認ボタンメッセージを送る（カレンダーUI付き） =====
def send_confirm_message(user_id, event_name, remind_at, image_url=None):
    date_str, time_str = remind_at.split(' ')
    short_name = event_name[:18] + '..' if len(event_name) > 20 else event_name
    messages = []
    # 画像があれば確認時にも表示
    if image_url:
        messages.append(ImageSendMessage(
            original_content_url=image_url,
            preview_image_url=image_url
        ))
    messages.append(TemplateSendMessage(
        alt_text=f'イベント確認：{event_name}',
        template=ButtonsTemplate(
            title='📅 イベントを検出しました',
            text=f'{short_name}\n{date_str} {time_str}',
            actions=[
                PostbackAction(label='✅ このままOK', data='action=confirm'),
                PostbackAction(label='✏️ 名前を修正', data='action=edit_name'),
                DatetimePickerAction(
                    label='📅 日時を修正',
                    data='action=edit_datetime',
                    mode='datetime',
                    initial=f'{date_str}T{time_str}',
                    min='2026-01-01T00:00',
                    max='2030-12-31T23:59'
                )
            ]
        )
    ))
    line_bot_api.push_message(user_id, messages)


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

    try:
        # OpenAIで画像を分析
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
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
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }],
            max_tokens=300
        )

        result_text = response.choices[0].message.content.strip()
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        data = json.loads(json_match.group()) if json_match else {"found": False}

        if data.get("found"):
            event_name = data.get("event_name", "イベント")
            event_date = data.get("event_date", "")
            event_time = data.get("event_time", "09:00")
            remind_at = f"{event_date} {event_time}"

            # まず画像なしで確認メッセージを送る
            conn = get_conn()
            c = conn.cursor()
            c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url)
                         VALUES (%s, %s, %s, 'confirm', NULL)
                         ON CONFLICT (user_id) DO UPDATE
                         SET event_name=%s, remind_at=%s, state='confirm', image_url=NULL""",
                      (user_id, event_name, remind_at, event_name, remind_at))
            conn.commit()
            conn.close()

            send_confirm_message(user_id, event_name, remind_at, None)

            # バックグラウンドでFTPアップロードしてDBを更新
            def on_upload_complete(image_url):
                if image_url:
                    try:
                        conn2 = get_conn()
                        c2 = conn2.cursor()
                        c2.execute("UPDATE pending SET image_url=%s WHERE user_id=%s",
                                   (image_url, user_id))
                        conn2.commit()
                        conn2.close()
                        print(f"Image URL saved to pending: {image_url}")
                    except Exception as e:
                        print(f"DB update error: {e}")

            upload_image_to_xserver(image_data, on_upload_complete)

        else:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="⚠️ 画像から日付を見つけられませんでした。\n別の画像を試してみてください。")
            )

    except Exception as e:
        print(f"Error: {e}")
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text="❌ エラーが発生しました。もう一度試してください。")
        )


# ===== ボタンタップ（ポストバック）の処理 =====
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    params = event.postback.params

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT event_name, remind_at, state, image_url FROM pending WHERE user_id = %s", (user_id,))
    pending = c.fetchone()

    # ✅ このままOK → リマインダー確定
    if data == 'action=confirm' and pending:
        event_name, remind_at, _, image_url = pending
        c.execute("INSERT INTO reminders (user_id, event_name, remind_at, image_url) VALUES (%s, %s, %s, %s)",
                  (user_id, event_name, remind_at, image_url))
        c.execute("DELETE FROM pending WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"✅ リマインダーを設定しました！\n\n📌 {event_name}\n⏰ {remind_at}\n\n時間になったらお知らせします🔔"
            )
        )

    # ✏️ 名前を修正（新規）
    elif data == 'action=edit_name' and pending:
        c.execute("UPDATE pending SET state = 'edit_name' WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="✏️ 新しいイベント名を入力してください：")
        )

    # 📅 日時を修正（新規）
    elif data == 'action=edit_datetime' and pending:
        event_name, _, _, image_url = pending
        new_datetime = params.get('datetime', '')
        new_remind_at = new_datetime.replace('T', ' ')
        c.execute("UPDATE pending SET remind_at = %s, state = 'confirm' WHERE user_id = %s",
                  (new_remind_at, user_id))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"📅 日時を {new_remind_at} に変更しました！\n内容を確認してください👇")
        )
        send_confirm_message(user_id, event_name, new_remind_at, image_url)

    # ✏️ 既存リマインダーの名前を修正
    elif data.startswith('action=edit_existing_name_'):
        rid = int(data.split('_')[-1])
        c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url)
                     VALUES (%s, '', '', %s, NULL)
                     ON CONFLICT (user_id) DO UPDATE
                     SET event_name='', remind_at='', state=%s, image_url=NULL""",
                  (user_id, f'edit_existing_name_{rid}', f'edit_existing_name_{rid}'))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="✏️ 新しいイベント名を入力してください：")
        )

    # 📅 既存リマインダーの日時を修正
    elif data.startswith('action=edit_existing_datetime_'):
        rid = int(data.split('_')[-1])
        new_datetime = params.get('datetime', '')
        new_remind_at = new_datetime.replace('T', ' ')
        c.execute("UPDATE reminders SET remind_at = %s WHERE id = %s AND user_id = %s",
                  (new_remind_at, rid, user_id))
        c.execute("DELETE FROM pending WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"✅ 日時を {new_remind_at} に変更しました！")
        )

    else:
        conn.close()


# ===== テキストメッセージを受け取った時の処理 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT event_name, remind_at, state, image_url FROM pending WHERE user_id = %s", (user_id,))
    pending = c.fetchone()

    # 📖 説明書
    if text == '説明書':
        conn.close()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="📖 使い方ガイド\n"
                     "━━━━━━━━━━━━━━━\n\n"
                     "📸 【リマインダーを設定する】\n"
                     "イベントのチラシや予定表の画像を送ってください。\n"
                     "日付を自動で読み取り、ボタンが表示されます。\n\n"
                     "　✅ このままOK → そのまま登録\n"
                     "　✏️ 名前を修正 → テキストで入力\n"
                     "　📅 日時を修正 → カレンダーで選択\n\n"
                     "━━━━━━━━━━━━━━━\n\n"
                     "📋 【一覧を見る】\n"
                     "「一覧」と送ると登録済みリマインダーが表示されます。\n\n"
                     "━━━━━━━━━━━━━━━\n\n"
                     "🗑️ 【削除する】\n"
                     "「削除 1」のように番号を指定して送ってください。\n\n"
                     "━━━━━━━━━━━━━━━\n\n"
                     "✏️ 【修正する】\n"
                     "「修正 1」のように番号を指定して送ってください。\n"
                     "名前またはカレンダーで日時を変更できます。\n\n"
                     "━━━━━━━━━━━━━━━\n\n"
                     "🔔 【リマインダー通知】\n"
                     "設定した日時になると画像と一緒にお知らせが届きます。\n\n"
                     "━━━━━━━━━━━━━━━\n"
                     "📖「説明書」→ この画面を表示"
            )
        )
        return

    # 📋 一覧表示
    if text == '一覧':
        c.execute("SELECT id, event_name, remind_at FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at", (user_id,))
        reminders = c.fetchall()
        conn.close()
        if reminders:
            msg = "📋 設定中のリマインダー\n\n"
            for i, (rid, name, remind_at) in enumerate(reminders, 1):
                msg += f"{i}. {name}\n   ⏰ {remind_at}\n\n"
            msg += "─────────────\n🗑️ 削除する →「削除 番号」\n✏️ 修正する →「修正 番号」"
        else:
            msg = "設定中のリマインダーはありません。\nチラシの画像を送ってください！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 🗑️ 削除コマンド
    delete_match = re.match(r'^削除\s*(\d+)$', text)
    if delete_match:
        index = int(delete_match.group(1))
        c.execute("SELECT id, event_name FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at", (user_id,))
        reminders = c.fetchall()
        if 1 <= index <= len(reminders):
            rid, name = reminders[index - 1]
            c.execute("DELETE FROM reminders WHERE id = %s", (rid,))
            conn.commit()
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 「{name}」を削除しました。"))
        else:
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="その番号のリマインダーが見つかりません。\n「一覧」で確認してください。"))
        return

    # ✏️ 修正コマンド
    edit_match = re.match(r'^修正\s*(\d+)$', text)
    if edit_match:
        index = int(edit_match.group(1))
        c.execute("SELECT id, event_name, remind_at FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at", (user_id,))
        reminders = c.fetchall()
        if 1 <= index <= len(reminders):
            rid, name, remind_at = reminders[index - 1]
            c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url)
                         VALUES (%s, %s, %s, %s, NULL)
                         ON CONFLICT (user_id) DO UPDATE
                         SET event_name=%s, remind_at=%s, state=%s, image_url=NULL""",
                      (user_id, name, remind_at, f'edit_existing_{rid}',
                       name, remind_at, f'edit_existing_{rid}'))
            conn.commit()
            conn.close()
            date_str, time_str = remind_at.split(' ')
            title = name[:38] + '..' if len(name) > 40 else name
            line_bot_api.reply_message(
                event.reply_token,
                TemplateSendMessage(
                    alt_text=f'修正：{name}',
                    template=ButtonsTemplate(
                        title=f'✏️ {title}',
                        text=f'現在の日時：{remind_at}',
                        actions=[
                            PostbackAction(label='✏️ 名前を修正', data=f'action=edit_existing_name_{rid}'),
                            DatetimePickerAction(
                                label='📅 日時を修正',
                                data=f'action=edit_existing_datetime_{rid}',
                                mode='datetime',
                                initial=f'{date_str}T{time_str}',
                                min='2026-01-01T00:00',
                                max='2030-12-31T23:59'
                            )
                        ]
                    )
                )
            )
        else:
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="その番号のリマインダーが見つかりません。\n「一覧」で確認してください。"))
        return

    # ✏️ 名前の入力待ち
    if pending:
        event_name, remind_at, state, image_url = pending

        if state == 'edit_name':
            c.execute("UPDATE pending SET event_name = %s, state = 'confirm' WHERE user_id = %s", (text, user_id))
            conn.commit()
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✏️ イベント名を「{text}」に変更しました！\n内容を確認してください👇"))
            send_confirm_message(user_id, text, remind_at, image_url)
            return

        elif state.startswith('edit_existing_name_'):
            rid = int(state.split('_')[-1])
            c.execute("UPDATE reminders SET event_name = %s WHERE id = %s AND user_id = %s", (text, rid, user_id))
            c.execute("DELETE FROM pending WHERE user_id = %s", (user_id,))
            conn.commit()
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ イベント名を「{text}」に変更しました！"))
            return

    conn.close()

    # デフォルトメッセージ
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text="こんにちは！📅\n\nチラシや予定表の画像を送ると\n日付を読み取ってリマインダーを設定します！\n\n─────────────\n📖「説明書」→ 使い方を見る\n📋「一覧」→ リマインダー一覧\n🗑️「削除 1」→ 1番目を削除\n✏️「修正 1」→ 1番目を修正"
        )
    )


# ===== アプリ起動 =====
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
