import os
import json
import re
import base64
import io
import threading
import time
import psycopg2
import psycopg2.extras
import cloudinary
import cloudinary.uploader
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, PostbackEvent,
    TextSendMessage, TemplateSendMessage, ButtonsTemplate,
    PostbackAction, DatetimePickerAction, ImageSendMessage,
    QuickReply, QuickReplyButton)
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta, date as dateobj
from urllib.parse import quote
import pytz

JST = pytz.timezone('Asia/Tokyo')

app = Flask(__name__)

# ===== 環境変数から設定を読み込む =====
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

# ===== Cloudinary設定 =====
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', 'dzeex2b4y'),
    api_key=os.environ.get('CLOUDINARY_API_KEY', '831251356737948'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ===== Google Maps URL生成 =====
def make_maps_url(location):
    """場所名・住所からGoogle MapsのURLを生成する"""
    if not location or location in ('場所不明', ''):
        return None
    encoded = quote(location, safe='')
    return f"https://www.google.com/maps/search/{encoded}/"


# ===== 日付テキストを解析してYYYY-MM-DD形式で返す =====
def parse_date_input(text):
    """
    「今日」「3月13日」「3/13」などを解析してYYYY-MM-DDを返す。
    解析できない場合はNone。
    """
    today = datetime.now(JST).date()

    if text in ('今日', '本日'):
        return today.strftime('%Y-%m-%d')
    if text in ('明日', 'あした', '翌日'):
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')
    if text in ('明後日', 'あさって'):
        return (today + timedelta(days=2)).strftime('%Y-%m-%d')

    # X月X日 or X月X日
    m = re.match(r'^(\d{1,2})月(\d{1,2})日?$', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        for year in [today.year, today.year + 1]:
            try:
                return dateobj(year, month, day).strftime('%Y-%m-%d')
            except ValueError:
                pass
        return None

    # X/X or X-X（年なし）
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})$', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        for year in [today.year, today.year + 1]:
            try:
                return dateobj(year, month, day).strftime('%Y-%m-%d')
            except ValueError:
                pass
        return None

    # YYYY/MM/DD or YYYY-MM-DD
    m = re.match(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$', text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return dateobj(year, month, day).strftime('%Y-%m-%d')
        except ValueError:
            return None

    return None


# ===== 画像をCloudinaryにアップロード（バックグラウンドで実行） =====
def upload_image_to_cloudinary(image_data, callback):
    def _upload():
        try:
            result = cloudinary.uploader.upload(
                io.BytesIO(image_data),
                folder='InvitationClip',
                resource_type='image'
            )
            image_url = result['secure_url']
            print(f"Image uploaded successfully: {image_url}")
            callback(image_url)
        except Exception as e:
            print(f"Cloudinary upload error: {type(e).__name__}: {e}")
            callback(None)
    threading.Thread(target=_upload).start()


# ===== データベース接続 =====
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


# ===== データベースの初期化 =====
def init_db():
    conn = get_conn()
    c = conn.cursor()

    # remindersテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        event_name TEXT,
        remind_at TEXT,
        image_url TEXT,
        location TEXT,
        sent INTEGER DEFAULT 0,
        source_pending_id INTEGER
    )''')
    try:
        c.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS image_url TEXT")
        c.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS location TEXT")
        c.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS source_pending_id INTEGER")
    except Exception:
        pass

    # pendingテーブルの存在確認
    c.execute("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_name='pending' AND table_schema='public'
    """)
    pending_exists = c.fetchone()[0] > 0

    if not pending_exists:
        # 新規インストール: pendingテーブルを作成
        c.execute('''CREATE TABLE pending (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            event_name TEXT,
            remind_at TEXT,
            state TEXT DEFAULT 'confirm',
            image_url TEXT,
            location TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )''')
        print("Pending table created (fresh install).")
    else:
        # pendingテーブルが存在する場合: id列の有無を確認
        c.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='pending' AND column_name='id' AND table_schema='public'
        """)
        has_id = c.fetchone()[0] > 0

        if not has_id:
            # 旧構造(user_id PK)から新構造(id SERIAL PK)へ移行
            try:
                c.execute("ALTER TABLE pending RENAME TO pending_old")
                c.execute('''CREATE TABLE pending (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    event_name TEXT,
                    remind_at TEXT,
                    state TEXT DEFAULT 'confirm',
                    image_url TEXT,
                    location TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )''')
                c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url, location)
                             SELECT user_id, event_name, remind_at, state, image_url, location FROM pending_old""")
                c.execute("DROP TABLE pending_old")
                print("Pending table migrated to new structure.")
            except Exception as e:
                print(f"Pending migration error: {e}")
                conn.rollback()
                c.execute('''CREATE TABLE IF NOT EXISTS pending (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    event_name TEXT,
                    remind_at TEXT,
                    state TEXT DEFAULT 'confirm',
                    image_url TEXT,
                    location TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )''')
        else:
            # created_atカラムがなければ追加
            try:
                c.execute("ALTER TABLE pending ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()")
            except Exception:
                pass

    # 起動時に古い 'processing' レコードを削除（サーバー再起動後の取りこぼし防止）
    try:
        c.execute("""DELETE FROM pending WHERE state = 'processing'
                     AND created_at < NOW() - INTERVAL '10 minutes'""")
        deleted = c.rowcount
        if deleted > 0:
            print(f"Cleaned up {deleted} stale 'processing' records.")
    except Exception as e:
        print(f"Cleanup stale processing error: {e}")

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
    conn = None
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT id, user_id, event_name, image_url, location FROM reminders WHERE remind_at <= %s AND sent = 0", (now,))
        reminders = c.fetchall()
        for rid, user_id, event_name, image_url, location in reminders:
            try:
                loc = location if location else "場所不明"
                maps_url = make_maps_url(loc)
                map_line = f"\n🗺 {maps_url}" if maps_url else ""
                messages = []
                if image_url:
                    messages.append(ImageSendMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url
                    ))
                messages.append(TextSendMessage(
                    text=f"🔔 リマインダー！\n「{event_name}」の時間です！\n📍 {loc}{map_line}\n楽しんできてください😊"
                ))
                line_bot_api.push_message(user_id, messages)
                c.execute("UPDATE reminders SET sent = 1 WHERE id = %s", (rid,))
            except Exception as e:
                print(f"Error sending reminder id={rid}: {e}")
        conn.commit()
    except Exception as e:
        print(f"check_and_send_reminders error: {e}")
    finally:
        if conn:
            conn.close()


scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(check_and_send_reminders, 'interval', minutes=1)
scheduler.start()


# ===== postbackデータを解析（action&pid形式対応） =====
def parse_postback(data):
    parts = {}
    for part in data.split('&'):
        if '=' in part:
            k, v = part.split('=', 1)
            parts[k] = v
    action = parts.get('action', data)
    pid = int(parts.get('pid', 0))
    return action, pid


# ===== 確認メッセージを送る（QuickReply付き・5ボタン） =====
def send_confirm_message(user_id, event_name, remind_at, image_url=None, location="場所不明", pending_id=0):
    parts = remind_at.split(' ', 1)
    date_str = parts[0] if len(parts) > 0 and parts[0] else '2026-01-01'
    time_str = parts[1] if len(parts) > 1 and parts[1] else '09:00'
    loc = location if location else "場所不明"
    messages = []
    if image_url:
        messages.append(ImageSendMessage(
            original_content_url=image_url,
            preview_image_url=image_url
        ))
    maps_url = make_maps_url(loc)
    loc_line = f"📍 {loc}"
    if maps_url:
        loc_line += f"\n🗺 {maps_url}"
    messages.append(TextSendMessage(
        text=(
            f"📅 イベントを検出しました\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📌 {event_name}\n"
            f"⏰ {date_str} {time_str}\n"
            f"{loc_line}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"下のボタンで確認・修正してください👇"
        ),
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=PostbackAction(
                label='✅ このままOK', data=f'action=confirm&pid={pending_id}')),
            QuickReplyButton(action=PostbackAction(
                label='✏️ 名前を修正', data=f'action=edit_name&pid={pending_id}')),
            QuickReplyButton(action=DatetimePickerAction(
                label='📅 日時を修正',
                data=f'action=edit_datetime&pid={pending_id}',
                mode='datetime',
                initial=f'{date_str}T{time_str}',
                min='2026-01-01T00:00',
                max='2030-12-31T23:59'
            )),
            QuickReplyButton(action=PostbackAction(
                label='📍 場所を修正', data=f'action=edit_location&pid={pending_id}')),
            QuickReplyButton(action=PostbackAction(
                label='❌ キャンセル', data=f'action=cancel&pid={pending_id}')),
        ])
    ))
    line_bot_api.push_message(user_id, messages)


# ===== 次のpendingがあれば確認メッセージを出す =====
def show_next_pending(user_id, reply_token, done_message):
    next_p = None
    remaining_confirm = 0
    remaining_processing = 0
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""SELECT id, event_name, remind_at, image_url, location
                     FROM pending WHERE user_id = %s AND state = 'confirm'
                     ORDER BY created_at LIMIT 1""", (user_id,))
        next_p = c.fetchone()
        c.execute("SELECT COUNT(*) FROM pending WHERE user_id = %s AND state = 'confirm'", (user_id,))
        remaining_confirm = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM pending WHERE user_id = %s AND state = 'processing'", (user_id,))
        remaining_processing = c.fetchone()[0]
    except Exception as e:
        print(f"show_next_pending DB error: {e}")
    finally:
        conn.close()

    if next_p:
        next_id, next_name, next_at, next_img, next_loc = next_p
        # next_p自身を除いた追加待機件数（confirm + processing）
        additional_waiting = (remaining_confirm - 1) + remaining_processing
        if additional_waiting > 0:
            suffix = f"\n\nあと{additional_waiting}件が待機/分析中です。次を確認します👇"
        else:
            suffix = "\n\n次の画像を確認します👇"
        line_bot_api.reply_message(reply_token, TextSendMessage(
            text=f"{done_message}{suffix}"
        ))
        send_confirm_message(user_id, next_name, next_at, next_img, next_loc or "場所不明", next_id)
    elif remaining_processing > 0:
        # confirmはないがまだ分析中の画像がある
        line_bot_api.reply_message(reply_token, TextSendMessage(
            text=f"{done_message}\n\nあと{remaining_processing}枚の画像を分析中です... しばらくお待ちください⏳"
        ))
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=done_message))


# ===== ヘルスチェック用エンドポイント（UptimeRobot用） =====
@app.route("/", methods=['GET'])
def health_check():
    return 'OK', 200


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

    # ===== STEP 1: 「処理中」プレースホルダーをDBに挿入してキュー位置を原子的に確定 =====
    # 複数画像が同時に届いた場合でも、INSERTの順番（id/created_at）で
    # キュー内の位置が確定するため競合状態が発生しない
    placeholder_id = None
    is_first_in_queue = False
    pre_conn = None
    try:
        pre_conn = get_conn()
        pre_c = pre_conn.cursor()

        # 古い stale レコードを先にクリーンアップ
        # ・'processing': 5分以上前 → サーバー再起動等で取りこぼされたもの
        # ・'confirm': 2時間以上前 → 誰も操作しなかった放置レコード（ボタンが消えた等）
        pre_c.execute("""DELETE FROM pending
                         WHERE user_id = %s AND (
                           (state = 'processing' AND created_at < NOW() - INTERVAL '5 minutes')
                           OR (state = 'confirm'  AND created_at < NOW() - INTERVAL '2 hours')
                         )""", (user_id,))
        cleaned = pre_c.rowcount
        if cleaned > 0:
            print(f"Cleaned up {cleaned} stale records for user {user_id}")

        # state='processing' のプレースホルダーを挿入（後でOpenAI解析結果で上書き）
        pre_c.execute(
            """INSERT INTO pending (user_id, event_name, remind_at, state, image_url, location)
               VALUES (%s, %s, %s, 'processing', NULL, NULL) RETURNING id""",
            (user_id, '分析中...', '')
        )
        placeholder_id = pre_c.fetchone()[0]
        # 自分より前に挿入された confirm/processing レコードがあるか確認
        pre_c.execute(
            """SELECT COUNT(*) FROM pending
               WHERE user_id = %s AND state IN ('confirm', 'processing') AND id < %s""",
            (user_id, placeholder_id)
        )
        earlier_count = pre_c.fetchone()[0]
        is_first_in_queue = (earlier_count == 0)
        pre_conn.commit()
    except Exception as e:
        print(f"handle_image placeholder insert error: {e}")
        placeholder_id = None
        is_first_in_queue = False
    finally:
        if pre_conn:
            pre_conn.close()

    # 最初の画像だけ「分析中」リプライを送る
    # 2枚目以降はサイレント（reply_message を送るとQuickReplyボタンが消えるため）
    if is_first_in_queue:
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📸 画像を分析中です...\n少々お待ちください⏳")
            )
        except Exception as e:
            print(f"reply_message (分析中) error: {e}")

    # ===== STEP 2: 画像データ取得 =====
    message_content = line_bot_api.get_message_content(message_id)
    image_data = b''
    for chunk in message_content.iter_content():
        image_data += chunk

    image_base64 = base64.b64encode(image_data).decode('utf-8')

    try:
        # ===== STEP 3: OpenAI で画像を分析 =====
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
  "event_time": "HH:MM",
  "event_location": "場所名"
}
日付が見つからない場合: {"found": false}
・年が書いていない場合は2026年を使用
・時間が書いていない場合は"09:00"を使用
・場所が画像に明記されていない場合は必ず"場所不明"を使用（憶測で入れない）"""
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
            event_location = data.get("event_location") or "場所不明"
            remind_at = f"{event_date} {event_time}"

            conn = get_conn()
            try:
                c = conn.cursor()

                if placeholder_id:
                    # プレースホルダーを実データで上書きして 'confirm' に変更
                    c.execute("""UPDATE pending
                                 SET event_name=%s, remind_at=%s, state='confirm', location=%s
                                 WHERE id=%s AND user_id=%s""",
                              (event_name, remind_at, event_location, placeholder_id, user_id))
                    pending_id = placeholder_id
                else:
                    # フォールバック（プレースホルダーが失敗した場合）
                    c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url, location)
                                 VALUES (%s, %s, %s, 'confirm', NULL, %s) RETURNING id""",
                              (user_id, event_name, remind_at, event_location))
                    pending_id = c.fetchone()[0]

                # 自分より前に 'processing' レコード（まだ分析中の画像）があるか確認
                # → あれば自分は先頭ではない（それらが終わった後に表示される）
                c.execute("""SELECT COUNT(*) FROM pending
                             WHERE user_id = %s AND state = 'processing' AND id < %s""",
                          (user_id, pending_id))
                earlier_processing = c.fetchone()[0]

                # 自分が最古の 'confirm' レコードか確認
                c.execute("""SELECT MIN(id) FROM pending
                             WHERE user_id = %s AND state = 'confirm'""",
                          (user_id,))
                oldest_confirm_id = c.fetchone()[0]

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

            # 条件: 自分より前に処理中がなく、かつ自分が最古の confirm → 確認メッセージを表示
            # これにより複数画像が同時に届いても必ず1件だけ確認メッセージが表示される
            is_first = (earlier_processing == 0) and (oldest_confirm_id == pending_id)
            if is_first:
                send_confirm_message(user_id, event_name, remind_at, None, event_location, pending_id)

            # バックグラウンドでCloudinaryにアップロードしてDBを更新
            def on_upload_complete(image_url, _pid=pending_id):
                if image_url:
                    conn2 = None
                    try:
                        conn2 = get_conn()
                        c2 = conn2.cursor()
                        # pendingに更新（まだ確認待ちの場合）
                        c2.execute("UPDATE pending SET image_url=%s WHERE id=%s",
                                   (image_url, _pid))
                        pending_updated = c2.rowcount
                        # すでにOKが押されてremindersに移動済みの場合もremindersを更新
                        c2.execute("UPDATE reminders SET image_url=%s WHERE source_pending_id=%s AND image_url IS NULL",
                                   (image_url, _pid))
                        reminders_updated = c2.rowcount
                        conn2.commit()
                        print(f"Upload done pid={_pid}: pending_updated={pending_updated}, reminders_updated={reminders_updated}, url={image_url}")
                    except Exception as e:
                        print(f"DB update error: {e}")
                    finally:
                        if conn2:
                            conn2.close()

            upload_image_to_cloudinary(image_data, on_upload_complete)

        else:
            # イベントが見つからなかった → プレースホルダーを削除
            if placeholder_id:
                try:
                    del_conn = get_conn()
                    del_c = del_conn.cursor()
                    del_c.execute("DELETE FROM pending WHERE id=%s AND user_id=%s",
                                  (placeholder_id, user_id))
                    del_conn.commit()
                    del_conn.close()
                except Exception as del_e:
                    print(f"delete placeholder (not found) error: {del_e}")

            line_bot_api.push_message(
                user_id,
                TextSendMessage(text="⚠️ 画像から日付を見つけられませんでした。\n別の画像を試してみてください。")
            )

    except Exception as e:
        print(f"Error: {e}")
        # エラー時もプレースホルダーを削除（キューが詰まるのを防ぐ）
        if placeholder_id:
            try:
                del_conn = get_conn()
                del_c = del_conn.cursor()
                del_c.execute("DELETE FROM pending WHERE id=%s AND user_id=%s",
                              (placeholder_id, user_id))
                del_conn.commit()
                del_conn.close()
            except Exception as del_e:
                print(f"delete placeholder (error case) error: {del_e}")

        line_bot_api.push_message(
            user_id,
            TextSendMessage(text="❌ エラーが発生しました。もう一度試してください。")
        )


# ===== ボタンタップ（ポストバック）の処理 =====
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    raw_data = event.postback.data
    params = event.postback.params
    action, pid = parse_postback(raw_data)

    conn = get_conn()
    c = conn.cursor()

    try:
        # ✅ このままOK → リマインダー確定
        if action == 'confirm' and pid > 0:
            c.execute("SELECT event_name, remind_at, image_url, location FROM pending WHERE id = %s AND user_id = %s",
                      (pid, user_id))
            row = c.fetchone()
            if row:
                event_name, remind_at, image_url, location = row
                loc = location if location else "場所不明"
                c.execute("INSERT INTO reminders (user_id, event_name, remind_at, image_url, location, source_pending_id) VALUES (%s, %s, %s, %s, %s, %s)",
                          (user_id, event_name, remind_at, image_url, loc, pid))
                c.execute("DELETE FROM pending WHERE id = %s", (pid,))
                conn.commit()
                done_msg = f"✅ 登録しました！\n📌 {event_name}\n⏰ {remind_at}\n📍 {loc}"
                show_next_pending(user_id, event.reply_token, done_msg)
            else:
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text="✅ リマインダーを登録しました！"))

        # ❌ キャンセル
        elif action == 'cancel' and pid > 0:
            c.execute("DELETE FROM pending WHERE id = %s AND user_id = %s", (pid, user_id))
            conn.commit()
            show_next_pending(user_id, event.reply_token, "❌ キャンセルしました。")

        # ✏️ 名前を修正
        elif action == 'edit_name' and pid > 0:
            c.execute("UPDATE pending SET state = 'edit_name' WHERE id = %s AND user_id = %s", (pid, user_id))
            conn.commit()
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="✏️ 新しいイベント名を入力してください："))

        # 📅 日時を修正
        elif action == 'edit_datetime' and pid > 0:
            c.execute("SELECT event_name, image_url, location FROM pending WHERE id = %s AND user_id = %s",
                      (pid, user_id))
            row = c.fetchone()
            if row:
                event_name, image_url, location = row
                new_datetime = params.get('datetime', '')
                new_remind_at = new_datetime.replace('T', ' ')
                c.execute("UPDATE pending SET remind_at = %s, state = 'confirm' WHERE id = %s",
                          (new_remind_at, pid))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"📅 日時を {new_remind_at} に変更しました！\n内容を確認してください👇"))
                send_confirm_message(user_id, event_name, new_remind_at, image_url, location, pid)
            else:
                conn.commit()

        # 📍 場所を修正
        elif action == 'edit_location' and pid > 0:
            c.execute("UPDATE pending SET state = 'edit_location' WHERE id = %s AND user_id = %s", (pid, user_id))
            conn.commit()
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="📍 新しい場所を入力してください："))

        # ✏️ 既存リマインダーの名前を修正
        elif action.startswith('edit_existing_name_') or raw_data.startswith('action=edit_existing_name_'):
            rid = int(raw_data.split('_')[-1])
            # 既存のedit用pendingを削除してから挿入
            c.execute("DELETE FROM pending WHERE user_id = %s AND state LIKE 'edit_existing%%'", (user_id,))
            c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url, location)
                         VALUES (%s, '', '', %s, NULL, NULL)""",
                      (user_id, f'edit_existing_name_{rid}'))
            conn.commit()
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="✏️ 新しいイベント名を入力してください："))

        # 📅 既存リマインダーの日時を修正
        elif raw_data.startswith('action=edit_existing_datetime_'):
            rid = int(raw_data.split('_')[-1])
            new_datetime = params.get('datetime', '') if params else ''
            new_remind_at = new_datetime.replace('T', ' ').strip()
            if not new_remind_at:
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text="⚠️ 日時の取得に失敗しました。もう一度お試しください。"))
            else:
                c.execute("UPDATE reminders SET remind_at = %s WHERE id = %s AND user_id = %s",
                          (new_remind_at, rid, user_id))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"✅ 日時を {new_remind_at} に変更しました！"))

        # 📍 既存リマインダーの場所を修正
        elif raw_data.startswith('action=edit_existing_location_'):
            rid = int(raw_data.split('_')[-1])
            c.execute("DELETE FROM pending WHERE user_id = %s AND state LIKE 'edit_existing%%'", (user_id,))
            c.execute("""INSERT INTO pending (user_id, event_name, remind_at, state, image_url, location)
                         VALUES (%s, '', '', %s, NULL, NULL)""",
                      (user_id, f'edit_existing_location_{rid}'))
            conn.commit()
            line_bot_api.reply_message(event.reply_token,
                                       TextSendMessage(text="📍 新しい場所を入力してください："))

        else:
            pass  # 未知のアクション
    except Exception as e:
        print(f"handle_postback error: {e}")
    finally:
        conn.close()


# ===== テキストメッセージを受け取った時の処理 =====
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    conn = get_conn()
    c = conn.cursor()

    try:
        # 編集待ち状態のpendingを確認（confirm/processing以外のstate）
        c.execute("""SELECT id, event_name, remind_at, state, image_url, location
                     FROM pending WHERE user_id = %s AND state NOT IN ('confirm', 'processing')
                     ORDER BY created_at LIMIT 1""", (user_id,))
        editing = c.fetchone()

        # 📖 説明書
        if text == '説明書':
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="📖 使い方ガイド\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "📸 【リマインダーを設定する】\n"
                         "イベントのチラシや予定表の画像を送ってください。\n"
                         "複数枚まとめて送ると1件ずつ順番に確認できます。\n\n"
                         "　✅ このままOK → そのまま登録\n"
                         "　✏️ 名前を修正 → テキストで入力\n"
                         "　📅 日時を修正 → カレンダーで選択\n"
                         "　📍 場所を修正 → テキストで入力\n"
                         "　❌ キャンセル → 登録しない\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "📋 【一覧を見る】\n"
                         "「一覧」と送ると登録済みリマインダーが表示されます。\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "🔍 【日付で検索】\n"
                         "「今日」「明日」「3月13日」「3/13」などで\nその日のリマインダーを確認できます。\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "🗑️ 【削除する】\n"
                         "「削除 1」のように番号を指定して送ってください。\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "✏️ 【修正する】\n"
                         "「修正 1」のように番号を指定して送ってください。\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "🔔 【リマインダー通知】\n"
                         "設定した日時になると画像と一緒にお知らせが届きます。\n\n"
                         "━━━━━━━━━━━━━━━\n\n"
                         "🔄 【画像が反応しない場合】\n"
                         "「クリア」と送ると確認待ちをリセットできます。\n\n"
                         "━━━━━━━━━━━━━━━\n"
                         "📖「説明書」→ この画面を表示"
                )
            )
            return

        # 🗑️ クリア（詰まった場合のリセット）
        if text == 'クリア':
            c.execute("DELETE FROM pending WHERE user_id = %s AND state IN ('confirm', 'processing')", (user_id,))
            deleted = c.rowcount
            conn.commit()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"🗑️ 確認待ち・分析中の画像を{deleted}件クリアしました。\nもう一度画像を送ってください📸")
            )
            return

        # 📋 一覧表示
        if text == '一覧':
            c.execute("SELECT id, event_name, remind_at, location FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at", (user_id,))
            reminders = c.fetchall()
            if reminders:
                msg = "📋 設定中のリマインダー\n\n"
                for i, (rid, name, remind_at, location) in enumerate(reminders, 1):
                    loc = location if location else "場所不明"
                    maps_url = make_maps_url(loc)
                    map_line = f"\n   🗺 {maps_url}" if maps_url else ""
                    msg += f"{i}. {name}\n   ⏰ {remind_at}\n   📍 {loc}{map_line}\n\n"
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
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 「{name}」を削除しました。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="その番号のリマインダーが見つかりません。\n「一覧」で確認してください。"))
            return

        # ✏️ 修正コマンド
        edit_match = re.match(r'^修正\s*(\d+)$', text)
        if edit_match:
            index = int(edit_match.group(1))
            c.execute("SELECT id, event_name, remind_at, location FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at", (user_id,))
            reminders = c.fetchall()
            if 1 <= index <= len(reminders):
                rid, name, remind_at, location = reminders[index - 1]
                loc = location if location else "場所不明"
                parts = remind_at.split(' ', 1)
                date_str = parts[0] if len(parts) > 0 and parts[0] else '2026-01-01'
                time_str = parts[1] if len(parts) > 1 and parts[1] else '09:00'
                # '✏️ ' プレフィックス3文字分を引いた37文字以内に切り詰める（合計40文字制限）
                title = (name[:35] + '..') if len(name) > 37 else name
                line_bot_api.reply_message(
                    event.reply_token,
                    TemplateSendMessage(
                        alt_text=f'修正：{name}',
                        template=ButtonsTemplate(
                            title=f'✏️ {title}',
                            text=f'{remind_at}\n📍 {loc[:20]}',
                            actions=[
                                PostbackAction(label='✏️ 名前を修正', data=f'action=edit_existing_name_{rid}'),
                                DatetimePickerAction(
                                    label='📅 日時を修正',
                                    data=f'action=edit_existing_datetime_{rid}',
                                    mode='datetime',
                                    initial=f'{date_str}T{time_str}',
                                    min='2026-01-01T00:00',
                                    max='2030-12-31T23:59'
                                ),
                                PostbackAction(label='📍 場所を修正', data=f'action=edit_existing_location_{rid}'),
                            ]
                        )
                    )
                )
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="その番号のリマインダーが見つかりません。\n「一覧」で確認してください。"))
            return

        # ✏️📍 編集待ち状態の処理
        # ※ 日付検索より先にチェックする。
        #   edit_name/edit_location 中に「3/13」「今日」などを入力した場合、
        #   日付検索ではなくイベント名・場所として登録する必要があるため。
        if editing:
            edit_id, event_name, remind_at, state, image_url, location = editing

            if state == 'edit_name':
                c.execute("UPDATE pending SET event_name = %s, state = 'confirm' WHERE id = %s", (text, edit_id))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"✏️ イベント名を「{text}」に変更しました！\n内容を確認してください👇"))
                send_confirm_message(user_id, text, remind_at, image_url, location or "場所不明", edit_id)
                return

            elif state == 'edit_location':
                c.execute("UPDATE pending SET location = %s, state = 'confirm' WHERE id = %s", (text, edit_id))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"📍 場所を「{text}」に変更しました！\n内容を確認してください👇"))
                send_confirm_message(user_id, event_name, remind_at, image_url, text, edit_id)
                return

            elif state.startswith('edit_existing_name_'):
                rid = int(state.split('_')[-1])
                c.execute("UPDATE reminders SET event_name = %s WHERE id = %s AND user_id = %s", (text, rid, user_id))
                c.execute("DELETE FROM pending WHERE id = %s", (edit_id,))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"✅ イベント名を「{text}」に変更しました！"))
                return

            elif state.startswith('edit_existing_location_'):
                rid = int(state.split('_')[-1])
                c.execute("UPDATE reminders SET location = %s WHERE id = %s AND user_id = %s", (text, rid, user_id))
                c.execute("DELETE FROM pending WHERE id = %s", (edit_id,))
                conn.commit()
                line_bot_api.reply_message(event.reply_token,
                                           TextSendMessage(text=f"✅ 場所を「{text}」に変更しました！"))
                return

        # 📅 日付検索（「今日」「3月13日」「3/13」など）
        # ※ 編集状態チェックの後に置くことで、編集中に日付らしい文字を打っても
        #   誤って日付検索にならないようにしている
        date_str_parsed = parse_date_input(text)
        if date_str_parsed:
            today_str = datetime.now(JST).strftime('%Y-%m-%d')
            if date_str_parsed < today_str:
                # 過去の日付
                d = dateobj.fromisoformat(date_str_parsed)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"📅 {d.month}月{d.day}日は過去の日付です。\n過去のリマインダーは消去されています。"
                ))
            else:
                # 当日または未来
                c.execute("""SELECT event_name, remind_at, location FROM reminders
                             WHERE user_id = %s AND remind_at LIKE %s AND sent = 0
                             ORDER BY remind_at""",
                          (user_id, f"{date_str_parsed}%"))
                day_reminders = c.fetchall()
                d = dateobj.fromisoformat(date_str_parsed)
                label = f"{d.month}月{d.day}日"
                if date_str_parsed == today_str:
                    label = f"今日（{d.month}月{d.day}日）"
                if day_reminders:
                    msg = f"📅 {label}のリマインダー\n\n"
                    for name, remind_at, location in day_reminders:
                        loc = location if location else "場所不明"
                        maps_url = make_maps_url(loc)
                        map_line = f"\n   🗺 {maps_url}" if maps_url else ""
                        msg += f"📌 {name}\n   ⏰ {remind_at}\n   📍 {loc}{map_line}\n\n"
                else:
                    msg = f"📅 {label}のリマインダーはありません。"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg.rstrip()))
            return

        # デフォルトメッセージ
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="こんにちは！📅\n\nチラシや予定表の画像を送ると\n日付を読み取ってリマインダーを設定します！\n複数枚まとめて送ってもOK📸\n\n─────────────\n📖「説明書」→ 使い方を見る\n📋「一覧」→ リマインダー一覧\n🔍「今日」「3/13」→ 日付検索\n🗑️「削除 1」→ 1番目を削除\n✏️「修正 1」→ 1番目を修正\n🔄「クリア」→ 詰まった時のリセット"
            )
        )

    except Exception as e:
        print(f"handle_text error: {e}")
    finally:
        conn.close()


# ===== アプリ起動 =====
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
