"""Relay-сервер для Render: принимает сообщения из Telegram (webhook)
и передаёт команды агенту, запущенному на Mac."""
import asyncio
import os
import time
import uuid

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
AGENT_KEY = os.environ["AGENT_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
queue: list[dict] = []
last_seen = 0.0

HELP = """🖥 *Управление Mac*

*Информация*
/status — CPU, RAM, диск, аптайм
/top — что больше всего грузит систему
/battery — батарея
/ip — локальный и внешний IP
/online — на связи ли Mac

*Питание и экран*
/sleep — усыпить Mac
/screen\\_off — выключить экран
/wake — разбудить экран
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

SIMPLE = {"status", "top", "battery", "wake", "screen_off", "lock", "shot",
          "mute", "unmute", "keepawake", "allowsleep", "ip"}
WITH_ARGS = {"volume", "notify", "say"}
DANGER = {
    "sleep": "💤 Усыпить Mac?",
    "restart": "🔄 Перезагрузить Mac?",
    "shutdown": "⏻ Выключить Mac?",
    "kill": "☠️ Завершить процесс?",
}
ALL = SIMPLE | WITH_ARGS | set(DANGER)

MENU = [
    ("status", "CPU, RAM, диск"), ("top", "Топ процессов"), ("battery", "Батарея"),
    ("sleep", "Усыпить Mac"), ("screen_off", "Выключить экран"), ("wake", "Разбудить экран"),
    ("lock", "Заблокировать"), ("shot", "Скриншот"), ("volume", "Громкость"),
    ("keepawake", "Не засыпать"), ("allowsleep", "Разрешить сон"), ("online", "Mac на связи?"),
    ("help", "Все команды"),
]


def online() -> bool:
    return time.time() - last_seen < 40


async def tg(method: str, **payload):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/{method}", json=payload)
        return r.json()


async def send(chat_id, text, **kw):
    return await tg("sendMessage", chat_id=chat_id, text=text, parse_mode="Markdown", **kw)


@app.on_event("startup")
async def startup():
    if PUBLIC_URL:
        await tg("setWebhook", url=f"{PUBLIC_URL}/tg", secret_token=WEBHOOK_SECRET,
                 allowed_updates=["message", "callback_query"])
    await tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU])


@app.get("/")
async def root():
    return {"ok": True, "mac_online": online()}


async def run_command(chat_id, cmd, args="", confirmed=False):
    if cmd in ("start", "help"):
        return await send(chat_id, HELP)
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


@app.post("/tg")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)
    upd = await request.json()

    if "callback_query" in upd:
        cq = upd["callback_query"]
        if cq["from"]["id"] != OWNER_ID:
            return {"ok": True}
        chat_id = cq["message"]["chat"]["id"]
        await tg("answerCallbackQuery", callback_query_id=cq["id"])
        await tg("editMessageReplyMarkup", chat_id=chat_id,
                 message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
        action, cmd, args = (cq["data"].split("|", 2) + ["", ""])[:3]
        if action == "ok":
            await run_command(chat_id, cmd, args, confirmed=True)
        else:
            await send(chat_id, "Отменено")
        return {"ok": True}

    msg = upd.get("message")
    if not msg or msg.get("from", {}).get("id") != OWNER_ID:
        return {"ok": True}  # чужим не отвечаем
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return {"ok": True}
    head, _, args = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    await run_command(msg["chat"]["id"], cmd, args.strip())
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
