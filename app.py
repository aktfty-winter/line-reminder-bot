import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent, LeaveEvent
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import date, datetime, timedelta
import psycopg2
import pytz

TAIPEI = pytz.timezone("Asia/Taipei")

app = Flask(__name__)

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)


def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY
        )
    """)
    cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS name TEXT")

    # 若 projects 沒有 group_id 欄位（舊版），刪掉重建
    cur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'projects' AND column_name = 'group_id'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("DROP TABLE IF EXISTS projects")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            deadline DATE NOT NULL
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_group_id_by_name(cur, name):
    cur.execute("SELECT group_id FROM groups WHERE name = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


@app.route("/ping", methods=["GET"])
def ping():
    print(f"[Ping] {datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M:%S')}")
    return "pong", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(JoinEvent)
def handle_join(event):
    if event.source.type == "group":
        group_id = event.source.group_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO groups (group_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (group_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        line_bot_api.push_message(
            group_id,
            TextSendMessage(
                text="大家好！我是法務截止日提醒機器人。\n\n請先在此群組輸入：\n命名 群組名稱\n\n例：命名 法務一組"
            )
        )


@handler.add(LeaveEvent)
def handle_leave(event):
    if event.source.type == "group":
        group_id = event.source.group_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM groups WHERE group_id = %s", (group_id,))
        conn.commit()
        cur.close()
        conn.close()


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    # 群組內只接受「命名」指令
    if event.source.type == "group":
        if text.startswith("命名 "):
            group_id = event.source.group_id
            name = text[3:].strip()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT group_id FROM groups WHERE name = %s", (name,))
            existing = cur.fetchone()
            if existing and existing[0] != group_id:
                reply = f"「{name}」已被其他群組使用，請換一個名稱。"
            else:
                cur.execute(
                    "UPDATE groups SET name = %s WHERE group_id = %s",
                    (name, group_id)
                )
                conn.commit()
                reply = f"✅ 群組已命名為「{name}」！\n管理員可透過私訊機器人管理此群組的專案。"
            cur.close()
            conn.close()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if event.source.type != "user":
        return

    # ── 群組清單 ──
    if text == "群組清單":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT name FROM groups WHERE name IS NOT NULL ORDER BY name")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if rows:
            names = "\n".join([f"• {r[0]}" for r in rows])
            reply = f"📋 已命名的群組：\n\n{names}"
        else:
            reply = "目前還沒有已命名的群組。\n請先將機器人加入群組，並在群組中輸入「命名 群組名稱」。"

    # ── 新增 群組名 專案名 YYYY-MM-DD ──
    elif text.startswith("新增 "):
        parts = text.split(" ", 3)
        if len(parts) == 4:
            group_name, proj_name, deadline_str = parts[1], parts[2], parts[3]
            conn = get_db()
            cur = conn.cursor()
            group_id = get_group_id_by_name(cur, group_name)
            if not group_id:
                reply = f"找不到群組「{group_name}」。\n請先輸入「群組清單」確認群組名稱。"
            else:
                try:
                    deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                    cur.execute(
                        "INSERT INTO projects (group_id, name, deadline) VALUES (%s, %s, %s) RETURNING id",
                        (group_id, proj_name, deadline)
                    )
                    pid = cur.fetchone()[0]
                    conn.commit()
                    days_left = (deadline - date.today()).days
                    reply = (
                        f"✅ 專案已新增！\n"
                        f"群組：{group_name}\n"
                        f"編號：#{pid}\n"
                        f"專案：{proj_name}\n"
                        f"截止日：{deadline.month}/{deadline.day}\n"
                        f"距今還有 {days_left} 天"
                    )
                except ValueError:
                    reply = "日期格式錯誤，請用：\n新增 群組名 專案名 YYYY-MM-DD\n例：新增 法務一組 臨終問答一校 2024-05-31"
            cur.close()
            conn.close()
        else:
            reply = "格式：新增 群組名 專案名 YYYY-MM-DD\n例：新增 法務一組 臨終問答一校 2024-05-31"

    # ── 查看 / 查看 群組名 ──
    elif text == "查看" or text.startswith("查看 "):
        conn = get_db()
        cur = conn.cursor()
        today = date.today()

        if text == "查看":
            cur.execute("""
                SELECT g.name, p.id, p.name, p.deadline
                FROM projects p JOIN groups g ON p.group_id = g.group_id
                WHERE g.name IS NOT NULL
                ORDER BY g.name, p.deadline
            """)
            rows = cur.fetchall()
            if rows:
                result = {}
                for gname, pid, pname, deadline in rows:
                    days_left = (deadline - today).days
                    status = f"已過截止 {abs(days_left)} 天" if days_left < 0 else ("今天截止！" if days_left == 0 else f"還有 {days_left} 天")
                    result.setdefault(gname, []).append(f"  #{pid} {pname}（{deadline.month}/{deadline.day} {status}）")
                lines = []
                for gname, items in result.items():
                    lines.append(f"【{gname}】")
                    lines.extend(items)
                reply = "📋 所有專案：\n\n" + "\n".join(lines)
            else:
                reply = "目前沒有任何專案。"
        else:
            group_name = text[3:].strip()
            group_id = get_group_id_by_name(cur, group_name)
            if not group_id:
                reply = f"找不到群組「{group_name}」。"
            else:
                cur.execute(
                    "SELECT id, name, deadline FROM projects WHERE group_id = %s ORDER BY deadline",
                    (group_id,)
                )
                rows = cur.fetchall()
                if rows:
                    lines = []
                    for pid, name, deadline in rows:
                        days_left = (deadline - today).days
                        status = f"已過截止 {abs(days_left)} 天" if days_left < 0 else ("今天截止！" if days_left == 0 else f"還有 {days_left} 天")
                        lines.append(f"#{pid} {name}\n　{deadline.month}/{deadline.day}（{status}）")
                    reply = f"📋【{group_name}】專案清單：\n\n" + "\n\n".join(lines)
                else:
                    reply = f"「{group_name}」目前沒有任何專案。"

        cur.close()
        conn.close()

    # ── 刪除 群組名 編號 ──
    elif text.startswith("刪除 "):
        parts = text.split(" ", 2)
        if len(parts) == 3:
            group_name, pid_str = parts[1], parts[2]
            try:
                pid = int(pid_str)
                conn = get_db()
                cur = conn.cursor()
                group_id = get_group_id_by_name(cur, group_name)
                if not group_id:
                    reply = f"找不到群組「{group_name}」。"
                else:
                    cur.execute(
                        "DELETE FROM projects WHERE id = %s AND group_id = %s RETURNING name",
                        (pid, group_id)
                    )
                    row = cur.fetchone()
                    conn.commit()
                    reply = f"✅ 已刪除【{group_name}】專案 #{pid}「{row[0]}」" if row else f"在「{group_name}」找不到專案 #{pid}。"
                cur.close()
                conn.close()
            except ValueError:
                reply = "格式：刪除 群組名 編號\n例：刪除 法務一組 1"
        else:
            reply = "格式：刪除 群組名 編號\n例：刪除 法務一組 1"

    # ── 測試 / 測試 群組名 ──
    elif text == "測試" or text.startswith("測試 "):
        conn = get_db()
        cur = conn.cursor()

        if text == "測試":
            cur.execute("SELECT group_id, name FROM groups WHERE name IS NOT NULL")
            groups = cur.fetchall()
        else:
            group_name = text[3:].strip()
            group_id = get_group_id_by_name(cur, group_name)
            groups = [(group_id, group_name)] if group_id else []

        if not groups:
            reply = "找不到群組，請確認群組名稱。"
        else:
            today = date.today()
            total = 0
            for group_id, group_name in groups:
                cur.execute(
                    "SELECT name, deadline FROM projects WHERE group_id = %s ORDER BY deadline",
                    (group_id,)
                )
                for name, deadline in cur.fetchall():
                    days_left = (deadline - today).days
                    if days_left < 0:
                        msg = f"🔴 已過截止！\n{name}\n截止日：{deadline.month}/{deadline.day}"
                    elif days_left == 0:
                        msg = f"🔴 今天截止！\n{name}\n截止日：{deadline.month}/{deadline.day}"
                    else:
                        msg = f"⚠️ {name} 距離{deadline.month}/{deadline.day}截止日還有{days_left}天"
                    try:
                        line_bot_api.push_message(group_id, TextSendMessage(text=msg))
                        total += 1
                    except Exception as e:
                        print(f"Push error: {e}")
            reply = f"✅ 已推播 {total} 則提醒到群組。" if total > 0 else "沒有可推播的專案。"

        cur.close()
        conn.close()

    else:
        reply = (
            "📌 私訊指令：\n\n"
            "• 群組清單\n\n"
            "• 新增 群組名 專案名 YYYY-MM-DD\n"
            "  例：新增 法務一組 臨終問答一校 2024-05-31\n\n"
            "• 查看\n"
            "• 查看 群組名\n\n"
            "• 刪除 群組名 編號\n"
            "  例：刪除 法務一組 1\n\n"
            "• 測試\n"
            "• 測試 群組名\n\n"
            "📌 群組內指令：\n"
            "• 命名 群組名稱"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


def send_reminders():
    now = datetime.now(TAIPEI)
    today = now.date()
    now_hour = now.hour
    print(f"[排程觸發] {now.strftime('%Y-%m-%d %H:%M:%S')} hour={now_hour}")
    conn = get_db()
    cur = conn.cursor()

    if now_hour == 8:
        cur.execute("""
            SELECT g.group_id, p.name, p.deadline
            FROM projects p JOIN groups g ON p.group_id = g.group_id
            WHERE p.deadline = %s AND g.name IS NOT NULL
        """, (today,))
        for group_id, name, deadline in cur.fetchall():
            try:
                line_bot_api.push_message(
                    group_id,
                    TextSendMessage(text=f"🔴 今天截止！\n{name}\n截止日：{deadline.month}/{deadline.day}")
                )
            except Exception as e:
                print(f"Push error: {e}")

    elif now_hour == 9:
        for days in [3, 5, 7]:
            target = today + timedelta(days=days)
            cur.execute("""
                SELECT g.group_id, p.name, p.deadline
                FROM projects p JOIN groups g ON p.group_id = g.group_id
                WHERE p.deadline = %s AND g.name IS NOT NULL
            """, (target,))
            for group_id, name, deadline in cur.fetchall():
                try:
                    line_bot_api.push_message(
                        group_id,
                        TextSendMessage(text=f"⚠️ {name} 距離{deadline.month}/{deadline.day}截止日還有{days}天")
                    )
                except Exception as e:
                    print(f"Push error: {e}")

    cur.close()
    conn.close()


scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_reminders, "cron", hour=8, minute=0)
scheduler.add_job(send_reminders, "cron", hour=9, minute=0)
scheduler.start()

init_db()

if __name__ == "__main__":
    app.run(port=5000)
