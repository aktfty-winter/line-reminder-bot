import os
import random
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent, LeaveEvent
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import date, datetime, timedelta
from collections import defaultdict
import psycopg2
import pytz
import requests as req

CONGRATS_MSGS = [
    "🎉 任務完成！\n「{name}」已順利結案！\n謝謝大家的努力與配合，辛苦了 ✨\n（順帶一提，這幾天的提醒轟炸也是我的功勞，不客氣 😎）",
    "🌟 完工了！\n「{name}」完美收尾 🎊\n感謝各位的付出，你們最棒了！\n（我這個盡責的鬧鐘機器人也默默流下感動的眼淚）",
    "🎊 大功告成！\n「{name}」任務圓滿！\n謝謝每個人的辛苦，繼續加油 💪\n（謝謝我自己每天準時報時，沒有功勞也有苦勞 🤖）",
    "✅ 任務結案！\n「{name}」搞定！\n感謝大家的配合，辛苦了 🥳\n（也謝謝這位每天碎碎念提醒大家的機器人，它真的很棒 👏）",
    "🏁 終於結束了！\n「{name}」正式畢業！\n大家辛苦了，可以暫時不用看到我了（但我會想你們的）👋",
    "🥳 收工慶祝！\n「{name}」完成啦！\n感謝大家沒有把我設成靜音，讓提醒發揮了作用 📣",
]

TAIPEI = pytz.timezone("Asia/Taipei")

app = Flask(__name__)

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)


def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def today_taipei():
    return datetime.now(TAIPEI).date()


def parse_reminder_type(s):
    """
    支援以下格式：
    - '週五'           → 每週五提醒
    - '14 7 3 1'       → 截止前指定天數
    - '週五 7 1'       → 每週五 + 截止前指定天數（混合）
    """
    s = s.strip()
    tokens = s.replace(",", " ").split()

    has_weekly = "週五" in tokens
    day_tokens = [t for t in tokens if t != "週五"]

    if day_tokens:
        try:
            days = [int(d) for d in day_tokens]
            if not all(d > 0 for d in days):
                return None, "天數必須是正整數"
            days_str = ",".join(str(d) for d in sorted(set(days), reverse=True))
        except ValueError:
            return None, "提醒設定格式錯誤\n請用「週五」、天數如「14 7 3 1」，或混合如「週五 7 1」"
    else:
        days_str = ""

    if not has_weekly and not days_str:
        return None, "提醒設定不能為空"

    if has_weekly and days_str:
        return f"週五+{days_str}", None
    elif has_weekly:
        return "週五", None
    else:
        return days_str, None


def format_reminder_type(reminder_type):
    if reminder_type == "週五":
        return "每週五提醒"
    if reminder_type.startswith("週五+"):
        days = reminder_type[3:].replace(",", " ")
        return f"每週五 + 截止前 {days} 天提醒"
    return f"截止前 {reminder_type.replace(',', ' ')} 天提醒"


def extract_days(reminder_type):
    """從 reminder_type 取出特定天數列表"""
    if reminder_type.startswith("週五+"):
        days_part = reminder_type[3:]
    elif reminder_type == "週五":
        return []
    else:
        days_part = reminder_type
    try:
        return [int(d.strip()) for d in days_part.split(",") if d.strip()]
    except ValueError:
        return []


def has_weekly(reminder_type):
    return reminder_type == "週五" or reminder_type.startswith("週五+")


def format_mention_all(mention_all):
    return {"none": "不@all", "deadline": "截止當天@all", "all": "每次都@all"}.get(mention_all, "不@all")


def push_message(group_id, text, use_all_mention=False):
    """推播訊息，可選擇是否 @all"""
    if use_all_mention:
        full_text = "@all\n" + text
        resp = req.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_TOKEN}"
            },
            json={
                "to": group_id,
                "messages": [{
                    "type": "text",
                    "text": full_text,
                    "mention": {
                        "mentionees": [{"index": 0, "length": 4, "type": "all"}]
                    }
                }]
            }
        )
        if not resp.ok:
            raise Exception(f"@all push error {resp.status_code}: {resp.text}")
    else:
        line_bot_api.push_message(group_id, TextSendMessage(text=text))


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS groups (group_id TEXT PRIMARY KEY)")
    cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS name TEXT")

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
            deadline DATE NOT NULL,
            reminder_type TEXT NOT NULL DEFAULT '7,5,3',
            mention_all TEXT NOT NULL DEFAULT 'none'
        )
    """)
    cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS reminder_type TEXT DEFAULT '7,5,3'")
    cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mention_all TEXT DEFAULT 'none'")

    conn.commit()
    cur.close()
    conn.close()


def get_group_id_by_name(cur, name):
    cur.execute("SELECT group_id FROM groups WHERE name = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


@app.route("/ping", methods=["GET"])
def ping():
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
        cur.execute("INSERT INTO groups (group_id) VALUES (%s) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()
        cur.close()
        conn.close()
        line_bot_api.push_message(
            group_id,
            TextSendMessage(text="大家好！我是法務截止日提醒機器人。\n\n請先在此群組輸入：\n命名 群組名稱\n\n例：命名 法務一組")
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

    # ── 群組內只接受「命名」指令 ──
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
                cur.execute("UPDATE groups SET name = %s WHERE group_id = %s", (name, group_id))
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
        reply = "📋 已命名的群組：\n\n" + "\n".join([f"• {r[0]}" for r in rows]) if rows else "目前還沒有已命名的群組。"

    # ── 新增 群組名 專案名 YYYY-MM-DD [提醒設定] ──
    elif text.startswith("新增 "):
        parts = text.split(" ", 4)
        if len(parts) >= 4:
            group_name, proj_name, deadline_str = parts[1], parts[2], parts[3]
            reminder_raw = parts[4] if len(parts) == 5 else "7,5,3"
            reminder_type, err = parse_reminder_type(reminder_raw)
            if err:
                reply = err
            else:
                conn = get_db()
                cur = conn.cursor()
                group_id = get_group_id_by_name(cur, group_name)
                if not group_id:
                    reply = f"找不到群組「{group_name}」。\n請先輸入「群組清單」確認群組名稱。"
                else:
                    try:
                        deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                        cur.execute(
                            "INSERT INTO projects (group_id, name, deadline, reminder_type, mention_all) VALUES (%s, %s, %s, %s, 'none') RETURNING id",
                            (group_id, proj_name, deadline, reminder_type)
                        )
                        pid = cur.fetchone()[0]
                        conn.commit()
                        days_left = (deadline - today_taipei()).days
                        reply = (
                            f"✅ 專案已新增！\n"
                            f"群組：{group_name}\n"
                            f"編號：#{pid}\n"
                            f"專案：{proj_name}\n"
                            f"截止日：{deadline.month}/{deadline.day}（還有 {days_left} 天）\n"
                            f"提醒：{format_reminder_type(reminder_type)}\n"
                            f"@all：不@all"
                        )
                    except ValueError:
                        reply = "日期格式錯誤，請用 YYYY-MM-DD\n例：2026-05-31"
                cur.close()
                conn.close()
        else:
            reply = (
                "格式：新增 群組名 專案名 YYYY-MM-DD [提醒設定]\n\n"
                "提醒設定（可選）：\n"
                "• 週五 → 每週五提醒\n"
                "• 14 7 3 1 → 截止前指定天數\n"
                "• 不填 → 預設 7 5 3 天前\n\n"
                "例：新增 法務一組 臨終問答一校 2026-05-31 週五"
            )

    # ── 查看 / 查看 群組名 ──
    elif text == "查看" or text.startswith("查看 "):
        conn = get_db()
        cur = conn.cursor()
        today = today_taipei()

        if text == "查看":
            cur.execute("""
                SELECT g.name, p.id, p.name, p.deadline, p.reminder_type, p.mention_all
                FROM projects p JOIN groups g ON p.group_id = g.group_id
                WHERE g.name IS NOT NULL ORDER BY g.name, p.deadline
            """)
            rows = cur.fetchall()
            if rows:
                result = defaultdict(list)
                for gname, pid, pname, deadline, rtype, mention_all in rows:
                    days_left = (deadline - today).days
                    status = f"已過截止 {abs(days_left)} 天" if days_left < 0 else ("今天截止！" if days_left == 0 else f"還有 {days_left} 天")
                    result[gname].append(
                        f"  #{pid} {pname}\n"
                        f"    截止：{deadline.month}/{deadline.day}（{status}）\n"
                        f"    提醒：{format_reminder_type(rtype)}｜{format_mention_all(mention_all)}"
                    )
                lines = []
                for gname, items in result.items():
                    lines.append(f"【{gname}】")
                    lines.extend(items)
                reply = "📋 所有專案：\n\n" + "\n\n".join(lines)
            else:
                reply = "目前沒有任何專案。"
        else:
            group_name = text[3:].strip()
            group_id = get_group_id_by_name(cur, group_name)
            if not group_id:
                reply = f"找不到群組「{group_name}」。"
            else:
                cur.execute(
                    "SELECT id, name, deadline, reminder_type, mention_all FROM projects WHERE group_id = %s ORDER BY deadline",
                    (group_id,)
                )
                rows = cur.fetchall()
                if rows:
                    lines = []
                    for pid, name, deadline, rtype, mention_all in rows:
                        days_left = (deadline - today).days
                        status = f"已過截止 {abs(days_left)} 天" if days_left < 0 else ("今天截止！" if days_left == 0 else f"還有 {days_left} 天")
                        lines.append(
                            f"#{pid} {name}\n"
                            f"　截止：{deadline.month}/{deadline.day}（{status}）\n"
                            f"　提醒：{format_reminder_type(rtype)}｜{format_mention_all(mention_all)}"
                        )
                    reply = f"📋【{group_name}】專案清單：\n\n" + "\n\n".join(lines)
                else:
                    reply = f"「{group_name}」目前沒有任何專案。"

        cur.close()
        conn.close()

    # ── 改名 群組名 編號 新名稱 ──
    elif text.startswith("改名 "):
        parts = text.split(" ", 3)
        if len(parts) == 4:
            group_name, pid_str, new_name = parts[1], parts[2], parts[3]
            try:
                pid = int(pid_str)
                conn = get_db()
                cur = conn.cursor()
                group_id = get_group_id_by_name(cur, group_name)
                if not group_id:
                    reply = f"找不到群組「{group_name}」。"
                else:
                    cur.execute(
                        "UPDATE projects SET name = %s WHERE id = %s AND group_id = %s RETURNING id",
                        (new_name, pid, group_id)
                    )
                    row = cur.fetchone()
                    conn.commit()
                    reply = f"✅ 專案 #{pid} 已改名為「{new_name}」" if row else f"在「{group_name}」找不到專案 #{pid}。"
                cur.close()
                conn.close()
            except ValueError:
                reply = "格式：改名 群組名 編號 新名稱\n例：改名 法務一組 1 臨終問答一校（修訂版）"
        else:
            reply = "格式：改名 群組名 編號 新名稱\n例：改名 法務一組 1 臨終問答一校（修訂版）"

    # ── 設定提醒 群組名 編號 提醒設定 ──
    elif text.startswith("設定提醒 "):
        parts = text.split(" ", 3)
        if len(parts) == 4:
            group_name, pid_str, reminder_raw = parts[1], parts[2], parts[3]
            try:
                pid = int(pid_str)
                reminder_type, err = parse_reminder_type(reminder_raw)
                if err:
                    reply = err
                else:
                    conn = get_db()
                    cur = conn.cursor()
                    group_id = get_group_id_by_name(cur, group_name)
                    if not group_id:
                        reply = f"找不到群組「{group_name}」。"
                    else:
                        cur.execute(
                            "UPDATE projects SET reminder_type = %s WHERE id = %s AND group_id = %s RETURNING name",
                            (reminder_type, pid, group_id)
                        )
                        row = cur.fetchone()
                        conn.commit()
                        reply = f"✅ 已更新「{row[0]}」\n提醒 → {format_reminder_type(reminder_type)}" if row else f"在「{group_name}」找不到專案 #{pid}。"
                    cur.close()
                    conn.close()
            except ValueError:
                reply = "格式：設定提醒 群組名 編號 提醒設定\n例：設定提醒 法務一組 1 14 7 3 1"
        else:
            reply = (
                "格式：設定提醒 群組名 編號 提醒設定\n\n"
                "提醒設定：\n"
                "• 週五 → 每週五提醒\n"
                "• 14 7 3 1 → 截止前指定天數\n\n"
                "例：設定提醒 法務一組 1 14 7 3 1"
            )

    # ── 設定@all 群組名 編號 截止當天/全部/關閉 ──
    elif text.startswith("設定@all "):
        parts = text.split(" ", 3)
        if len(parts) == 4:
            group_name, pid_str, setting = parts[1], parts[2], parts[3].strip()
            mapping = {"截止當天": "deadline", "全部": "all", "關閉": "none"}
            if setting not in mapping:
                reply = "設定值請填：截止當天 / 全部 / 關閉"
            else:
                try:
                    pid = int(pid_str)
                    mention_all = mapping[setting]
                    conn = get_db()
                    cur = conn.cursor()
                    group_id = get_group_id_by_name(cur, group_name)
                    if not group_id:
                        reply = f"找不到群組「{group_name}」。"
                    else:
                        cur.execute(
                            "UPDATE projects SET mention_all = %s WHERE id = %s AND group_id = %s RETURNING name",
                            (mention_all, pid, group_id)
                        )
                        row = cur.fetchone()
                        conn.commit()
                        reply = f"✅ 已更新「{row[0]}」\n@all → {format_mention_all(mention_all)}" if row else f"在「{group_name}」找不到專案 #{pid}。"
                    cur.close()
                    conn.close()
                except ValueError:
                    reply = "格式：設定@all 群組名 編號 截止當天/全部/關閉"
        else:
            reply = "格式：設定@all 群組名 編號 截止當天/全部/關閉\n例：設定@all 法務一組 1 截止當天"

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

    # ── 結案 群組名 專案名稱 ──
    elif text.startswith("結案 "):
        parts = text.split(" ", 2)
        if len(parts) == 3:
            group_name, proj_name = parts[1], parts[2]
            conn = get_db()
            cur = conn.cursor()
            group_id = get_group_id_by_name(cur, group_name)
            cur.close()
            conn.close()
            if not group_id:
                reply = f"找不到群組「{group_name}」。"
            else:
                congrats_msg = random.choice(CONGRATS_MSGS).format(name=proj_name)
                try:
                    push_message(group_id, congrats_msg)
                    reply = f"✅ 已補發慶功訊息到【{group_name}】"
                except Exception as e:
                    reply = f"推播失敗：{e}"
        else:
            reply = "格式：結案 群組名 專案名稱\n例：結案 法務一組 臨終問答一校"

    # ── 測試 / 測試 群組名 ──
    elif text == "測試" or text.startswith("測試 "):
        conn = get_db()
        cur = conn.cursor()
        today = today_taipei()

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
            total = 0
            for group_id, _ in groups:
                cur.execute(
                    "SELECT name, deadline, mention_all FROM projects WHERE group_id = %s ORDER BY deadline",
                    (group_id,)
                )
                for name, deadline, mention_all in cur.fetchall():
                    days_left = (deadline - today).days
                    if days_left < 0:
                        msg = f"🔴 已過截止！\n{name}\n截止日：{deadline.month}/{deadline.day}"
                    elif days_left == 0:
                        msg = f"🔴 今天截止\n{name}\n截止日：{deadline.month}/{deadline.day}"
                    else:
                        msg = f"⚠️ {name} 距離{deadline.month}/{deadline.day}截止日還有{days_left}天"
                    use_mention = mention_all == "all" or (mention_all == "deadline" and days_left == 0)
                    try:
                        push_message(group_id, msg, use_all_mention=use_mention)
                        total += 1
                    except Exception as e:
                        print(f"Push error for group {group_id}: {e}")
                        reply = f"⚠️ 推播失敗：{e}"
            reply = f"✅ 已推播 {total} 則提醒到群組。" if total > 0 else "沒有可推播的專案。"

        cur.close()
        conn.close()

    else:
        reply = (
            "📌 私訊指令：\n\n"
            "• 群組清單\n\n"
            "• 新增 群組名 專案名 YYYY-MM-DD [提醒設定]\n"
            "  提醒設定：週五 / 14 7 3 1（不填預設 7 5 3）\n\n"
            "• 查看\n"
            "• 查看 群組名\n\n"
            "• 改名 群組名 編號 新名稱\n\n"
            "• 設定提醒 群組名 編號 提醒設定\n\n"
            "• 設定@all 群組名 編號 截止當天/全部/關閉\n\n"
            "• 刪除 群組名 編號\n\n"
            "• 結案 群組名 專案名稱（補發慶功訊息）\n\n"
            "• 測試 / 測試 群組名\n\n"
            "📌 群組內指令：\n"
            "• 命名 群組名稱"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


def send_deadline_reminders():
    """每天 8:00：截止當天提醒"""
    today = datetime.now(TAIPEI).date()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT g.group_id, p.name, p.deadline, p.mention_all
        FROM projects p JOIN groups g ON p.group_id = g.group_id
        WHERE p.deadline = %s AND g.name IS NOT NULL
    """, (today,))
    for group_id, name, deadline, mention_all in cur.fetchall():
        msg = f"🔴 今天截止\n{name}\n截止日：{deadline.month}/{deadline.day}"
        try:
            push_message(group_id, msg, use_all_mention=mention_all in ("all", "deadline"))
        except Exception as e:
            print(f"Push error: {e}")
    cur.close()
    conn.close()


def send_advance_reminders():
    """每天 9:00：提前提醒"""
    today = datetime.now(TAIPEI).date()
    is_friday = datetime.now(TAIPEI).weekday() == 4
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT g.group_id, p.name, p.deadline, p.reminder_type, p.mention_all
        FROM projects p JOIN groups g ON p.group_id = g.group_id
        WHERE g.name IS NOT NULL
    """)
    all_projects = cur.fetchall()

    # 週五型：彙整成一則訊息推播
    if is_friday:
        weekly_groups = defaultdict(list)
        for group_id, pname, deadline, reminder_type, mention_all in all_projects:
            if has_weekly(reminder_type) and deadline >= today:
                days_left = (deadline - today).days
                weekly_groups[group_id].append((pname, deadline, days_left, mention_all))
        for group_id, projects in weekly_groups.items():
            lines = [f"📅 每週截止日提醒（{today.month}/{today.day}）\n"]
            for pname, deadline, days_left, _ in projects:
                lines.append(f"⚠️ {pname} 距離{deadline.month}/{deadline.day}截止日還有{days_left}天")
            use_mention = any(m == "all" for _, _, _, m in projects)
            try:
                push_message(group_id, "\n".join(lines), use_all_mention=use_mention)
            except Exception as e:
                print(f"Push error: {e}")

    # 特定天數型（包含混合模式的天數部分）
    for group_id, name, deadline, reminder_type, mention_all in all_projects:
        days_left = (deadline - today).days
        remind_days = extract_days(reminder_type)
        if remind_days and days_left in remind_days:
            msg = f"⚠️ {name} 距離{deadline.month}/{deadline.day}截止日還有{days_left}天"
            try:
                push_message(group_id, msg, use_all_mention=mention_all == "all")
            except Exception as e:
                print(f"Push error: {e}")

    cur.close()
    conn.close()


scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_deadline_reminders, "cron", hour=8, minute=0)
scheduler.add_job(send_advance_reminders, "cron", hour=9, minute=0)
scheduler.start()

init_db()

if __name__ == "__main__":
    app.run(port=5000)
