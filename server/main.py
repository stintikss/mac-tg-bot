"""Relay-сервер для Render: принимает сообщения из Telegram (webhook)
и передаёт команды агенту, запущенному на Mac."""
import asyncio
import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile

BOT_TOKEN = os.environ["BOT_TOKEN"]
# OWNER_ID может содержать несколько админов через запятую: 111,222
ADMINS = [int(x) for x in os.environ["OWNER_ID"].replace(" ", "").split(",") if x]
OWNER_ID = ADMINS[0]  # главный админ: у него хранится база пользователей
AGENT_KEY = os.environ["AGENT_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
queue: list[dict] = []
last_seen = 0.0

HELP_USER = """🖥 *Управление Mac*

*Информация*
/status — CPU, RAM, диск, аптайм
/top — что больше всего грузит систему
/battery — батарея
/ip — локальный и внешний IP
/online — на связи ли Mac
"""

HELP_FULL = HELP_USER + """
*Питание и экран*
/sleep — усыпить Mac
/screen\\_off — выключить экран
/screen\\_on — включить экран (или /wake)
/lock — заблокировать
/keepawake — не давать засыпать
/allowsleep — снова разрешить сон
/restart — перезагрузка
/shutdown — выключение

*Прочее*
/shot — скриншот
/volume 0-100 — громкость
/mute, /unmute — звук
/notify текст — уведомление на Mac
/say текст — Mac произнесёт текст
/kill PID — завершить процесс
"""

HELP_ADMIN = HELP_FULL + """
*Админ*
/users — список пользователей
/adduser ID \\[full|basic] — добавить
/deluser ID — удалить
/setrole ID full|basic — сменить роль
_basic — только просмотр, full — полное управление_
"""

SIMPLE = {"status", "top", "battery", "wake", "screen_on", "screen_off", "lock", "shot",
          "mute", "unmute", "keepawake", "allowsleep", "ip"}
WITH_ARGS = {"volume", "notify", "say"}
DANGER = {
    "sleep": "💤 Усыпить Mac?",
    "restart": "🔄 Перезагрузить Mac?",
    "shutdown": "⏻ Выключить Mac?",
    "kill": "☠️ Завершить процесс?",
}
ALL = SIMPLE | WITH_ARGS | set(DANGER)
BASIC = {"status", "top", "battery", "ip"}
ADMIN_ONLY = {"users", "adduser", "deluser", "setrole"}

MENU = [
    ("status", "CPU, RAM, диск"), ("top", "Топ процессов"), ("battery", "Батарея"),
    ("sleep", "Усыпить Mac"), ("screen_off", "Выключить экран"), ("screen_on", "Включить экран"),
    ("lock", "Заблокировать"), ("shot", "Скриншот"), ("volume", "Громкость"),
    ("keepawake", "Не засыпать"), ("allowsleep", "Разрешить сон"), ("online", "Mac на связи?"),
    ("help", "Все команды"),
]
MENU_BASIC = [m for m in MENU if m[0] in BASIC | {"online", "help"}]
MENU_ADMIN = MENU + [("users", "Список пользователей"), ("adduser", "Добавить пользователя"),
                     ("deluser", "Удалить пользователя"), ("setrole", "Сменить роль")]

# ---------- пользователи (хранятся в закреплённом сообщении у админа) ----------
MARK = "USERS_DB:"
users: dict[int, dict] = {}          # id -> {"role": "basic"|"full", "name": str}
store_msg_id = None
pending: dict[int, float] = {}       # id -> время последнего запроса доступа


def role_of(uid: int):
    if uid in ADMINS:
        return "admin"
    return users.get(uid, {}).get("role")


def allowed(role, cmd) -> bool:
    if cmd in ("start", "help", "online"):
        return role is not None
    if cmd in ADMIN_ONLY:
        return role == "admin"
    if role in ("admin", "full"):
        return cmd in ALL
    if role == "basic":
        return cmd in BASIC
    return False


def online() -> bool:
    return time.time() - last_seen < 40


async def tg(method: str, **payload):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/{method}", json=payload)
        return r.json()


async def send(chat_id, text, **kw):
    return await tg("sendMessage", chat_id=chat_id, text=text, parse_mode="Markdown", **kw)


async def send_plain(chat_id, text, **kw):
    return await tg("sendMessage", chat_id=chat_id, text=text, **kw)


async def load_users():
    global store_msg_id
    r = await tg("getChat", chat_id=OWNER_ID)
    pm = (r.get("result") or {}).get("pinned_message")
    if pm and (pm.get("text") or "").startswith(MARK):
        data = json.loads(pm["text"][len(MARK):])
        users.clear()
        users.update({int(k): v for k, v in data.items()})
        store_msg_id = pm["message_id"]


async def save_users():
    global store_msg_id
    text = MARK + json.dumps({str(k): v for k, v in users.items()}, ensure_ascii=False)
    if store_msg_id:
        r = await tg("editMessageText", chat_id=OWNER_ID, message_id=store_msg_id, text=text)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    r = await tg("sendMessage", chat_id=OWNER_ID, text=text, disable_notification=True)
    store_msg_id = r["result"]["message_id"]
    await tg("pinChatMessage", chat_id=OWNER_ID, message_id=store_msg_id, disable_notification=True)


@app.on_event("startup")
async def startup():
    if PUBLIC_URL:
        await tg("setWebhook", url=f"{PUBLIC_URL}/tg", secret_token=WEBHOOK_SECRET,
                 allowed_updates=["message", "callback_query"])
    try:
        await load_users()
    except Exception as e:
        print("load_users failed:", e)
    await tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU_BASIC],
             scope={"type": "default"})
    for a in ADMINS:
        await tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU_ADMIN],
                 scope={"type": "chat", "chat_id": a})
    for uid, u in users.items():
        cmds = MENU if u.get("role") == "full" else MENU_BASIC
        await tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds],
                 scope={"type": "chat", "chat_id": uid})


@app.get("/")
async def root():
    return {"ok": True, "mac_online": online()}


def parse_id_role(args: str, default_role=None):
    parts = args.split()
    if not parts:
        return None, None
    try:
        uid = int(parts[0])
    except ValueError:
        return None, None
    role = parts[1].lower() if len(parts) > 1 else default_role
    return uid, role


async def apply_role(uid: int, role: str, name: str = ""):
    if not name:
        r = await tg("getChat", chat_id=uid)
        res = r.get("result") or {}
        name = res.get("first_name") or res.get("username") or "—"
    users[uid] = {"role": role, "name": name}
    await save_users()
    cmds = MENU if role == "full" else MENU_BASIC
    await tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds],
             scope={"type": "chat", "chat_id": uid})
    label = "полное управление" if role == "full" else "только просмотр"
    await send_plain(uid, f"✅ Тебе выдан доступ к боту ({label}). Напиши /help")


async def admin_command(chat_id, cmd, args):
    if cmd == "users":
        if not users:
            return await send_plain(chat_id, "Пользователей пока нет. Добавить: /adduser ID")
        lines = [f"{i}. {u.get('name', '—')} — {uid} — {u['role']}"
                 for i, (uid, u) in enumerate(users.items(), 1)]
        return await send_plain(chat_id, "👥 Пользователи:\n" + "\n".join(lines))
    if cmd == "adduser":
        uid, role = parse_id_role(args, "basic")
        if uid is None or role not in ("basic", "full"):
            return await send_plain(chat_id, "Использование: /adduser ID [full|basic]\nID человека можно узнать у @userinfobot")
        if uid in ADMINS:
            return await send_plain(chat_id, "Этот человек уже админ 🙂")
        await apply_role(uid, role)
        return await send_plain(chat_id, f"✅ {uid} добавлен ({role})")
    if cmd == "setrole":
        uid, role = parse_id_role(args)
        if uid not in users or role not in ("basic", "full"):
            return await send_plain(chat_id, "Использование: /setrole ID full|basic (ID должен быть в /users)")
        await apply_role(uid, role, users[uid].get("name", ""))
        return await send_plain(chat_id, f"✅ Роль {uid}: {role}")
    if cmd == "deluser":
        uid, _ = parse_id_role(args)
        if uid not in users:
            return await send_plain(chat_id, "Использование: /deluser ID (ID должен быть в /users)")
        users.pop(uid)
        await save_users()
        await tg("deleteMyCommands", scope={"type": "chat", "chat_id": uid})
        await send_plain(uid, "⛔ Твой доступ к боту отозван.")
        return await send_plain(chat_id, f"🗑 {uid} удалён")


async def run_command(chat_id, cmd, args, role, confirmed=False):
    if not allowed(role, cmd):
        return await send_plain(chat_id, "⛔ Нет прав на эту команду. /help")
    if cmd in ("start", "help"):
        return await send(chat_id, HELP_ADMIN if role == "admin" else HELP_FULL if role == "full" else HELP_USER)
    if cmd in ADMIN_ONLY:
        return await admin_command(chat_id, cmd, args)
    if cmd == "online":
        return await send(chat_id, "🟢 Mac на связи" if online() else "🔴 Mac офлайн (спит / нет сети / агент не запущен)")
    if cmd not in ALL:
        return await send(chat_id, "Неизвестная команда. /help")
    if not online():
        return await send(chat_id, "🔴 Mac офлайн: он спит, выключен или нет интернета.\n"
                                   "Разбудить спящий Mac из облака нельзя — нажми клавишу/крышку на самом Mac.")
    if cmd in DANGER and not confirmed:
        kb = {"inline_keyboard": [[
            {"text": "✅ Да", "callback_data": f"ok|{cmd}|{args}"[:64]},
            {"text": "❌ Отмена", "callback_data": "no||"},
        ]]}
        return await send(chat_id, DANGER[cmd] + (f" PID {args}" if args else ""), reply_markup=kb)
    queue.append({"id": uuid.uuid4().hex, "cmd": cmd, "args": args, "chat_id": chat_id, "ts": time.time()})


async def handle_stranger(msg):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    name = msg["from"].get("first_name", "") or "—"
    username = msg["from"].get("username")
    now = time.time()
    if now - pending.get(uid, 0) < 3600:
        return await send_plain(chat_id, "⏳ Запрос уже отправлен администратору, ожидай.")
    pending[uid] = now
    await send_plain(chat_id, "⛔ У тебя нет доступа. Запрос отправлен администратору.")
    kb = {"inline_keyboard": [[
        {"text": "👁 Basic", "callback_data": f"adm|b|{uid}"},
        {"text": "🛠 Full", "callback_data": f"adm|f|{uid}"},
        {"text": "❌ Отклонить", "callback_data": f"adm|n|{uid}"},
    ]]}
    who = f"{name}" + (f" (@{username})" if username else "") + f"\nID: {uid}"
    for a in ADMINS:
        await send_plain(a, f"🔔 Запрос доступа к боту:\n{who}", reply_markup=kb)


@app.post("/tg")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)
    upd = await request.json()

    if "callback_query" in upd:
        cq = upd["callback_query"]
        uid = cq["from"]["id"]
        role = role_of(uid)
        if role is None:
            return {"ok": True}
        chat_id = cq["message"]["chat"]["id"]
        await tg("answerCallbackQuery", callback_query_id=cq["id"])
        await tg("editMessageReplyMarkup", chat_id=chat_id,
                 message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
        action, a, b = (cq["data"].split("|", 2) + ["", ""])[:3]
        if action == "adm":
            if role != "admin":
                return {"ok": True}
            target = int(b)
            if a == "n":
                await send_plain(chat_id, f"❌ Запрос {target} отклонён")
                await send_plain(target, "⛔ Администратор отклонил запрос доступа.")
            else:
                name = (cq["message"].get("text", "").split("\n")[1:2] or ["—"])[0]
                await apply_role(target, "full" if a == "f" else "basic", name)
                await send_plain(chat_id, f"✅ {target} добавлен ({'full' if a == 'f' else 'basic'})")
        elif action == "ok":
            if not allowed(role, a):
                return {"ok": True}
            if uid not in ADMINS:
                for adm in ADMINS:
                    await send_plain(adm, f"⚠️ {users.get(uid, {}).get('name', uid)} ({uid}) выполнил /{a} {b}")
            await run_command(chat_id, a, b, role, confirmed=True)
        else:
            await send(chat_id, "Отменено")
        return {"ok": True}

    msg = upd.get("message")
    if not msg or "from" not in msg or msg["chat"].get("type") != "private":
        return {"ok": True}
    uid = msg["from"]["id"]
    role = role_of(uid)
    if role is None:
        await handle_stranger(msg)
        return {"ok": True}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return {"ok": True}
    head, _, args = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    await run_command(msg["chat"]["id"], cmd, args.strip(), role)
    return {"ok": True}


def check_agent(key):
    if key != AGENT_KEY:
        raise HTTPException(403)


@app.get("/agent/poll")
async def agent_poll(x_agent_key: str = Header(None)):
    global last_seen
    check_agent(x_agent_key)
    end = time.time() + 25
    while time.time() < end:
        last_seen = time.time()
        while queue and time.time() - queue[0]["ts"] > 60:
            queue.pop(0)  # протухшие команды не выполняем
        if queue:
            return {"job": queue.pop(0)}
        await asyncio.sleep(0.5)
    return {"job": None}


@app.post("/agent/result")
async def agent_result(request: Request, x_agent_key: str = Header(None)):
    check_agent(x_agent_key)
    d = await request.json()
    await send(d["chat_id"], d["text"][:4000])
    return {"ok": True}


@app.post("/agent/photo")
async def agent_photo(chat_id: int = Form(...), caption: str = Form(""),
                      file: UploadFile = File(...), x_agent_key: str = Header(None)):
    check_agent(x_agent_key)
    data = await file.read()
    async with httpx.AsyncClient(timeout=60) as c:
        await c.post(f"{API}/sendDocument", data={"chat_id": chat_id, "caption": caption},
                     files={"document": ("screenshot.png", data, "image/png")})
    return {"ok": True}
