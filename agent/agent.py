"""Агент для Mac: забирает команды у сервера на Render и выполняет их."""
import os
import re
import socket
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import psutil
import requests

HERE = Path(__file__).parent
env_file = HERE / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SERVER = os.environ["SERVER_URL"].rstrip("/")
HEADERS = {"X-Agent-Key": os.environ["AGENT_KEY"]}
caffeinate_proc = None


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()


def osa(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30).stdout.strip()


def fmt_bytes(n):
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def reply(chat_id, text):
    requests.post(f"{SERVER}/agent/result", headers=HEADERS,
                  json={"chat_id": chat_id, "text": text}, timeout=30)


def do_browser_open(value):
    """Open a unu.im task URL, accepting plain URLs or Markdown links."""
    value = str(value).strip()
    match = re.search(r'\]\((https?://[^)]+)\)', value)
    url = match.group(1) if match else value
    url = url.replace(r"\&", "&").strip()

    if not url.startswith("https://unu.im/"):
        return "⛔ Разрешены только ссылки unu.im"

    subprocess.Popen(["open", "-a", "Google Chrome", url])
    return f"🌐 Открываю страницу в Google Chrome: {url}"

def do_browser_close(_):
    osa('tell application "Google Chrome" to activate')
    osa('tell application "System Events" to key code 50 using {shift down}')
    return "⌨️ Отправлено Shift + клавиша Ё в Chrome"

def do_browser_back(_):
    osa('tell application "Google Chrome" to activate')
    osa('tell application "System Events" to keystroke "[" using {command down}')
    return "⬅️ Назад в Google Chrome"

def do_status(_):
    cpu = psutil.cpu_percent(interval=1)
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    up = time.time() - psutil.boot_time()
    load = os.getloadavg()
    return (f"📊 *Статус*\nCPU: {cpu}% (load {load[0]:.2f})\n"
            f"RAM: {vm.percent}% ({fmt_bytes(vm.used)} / {fmt_bytes(vm.total)})\n"
            f"Диск: {disk.percent}% (свободно {fmt_bytes(disk.free)})\n"
            f"Аптайм: {int(up // 3600)} ч {int(up % 3600 // 60)} мин")


def do_top(_):
    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(1.5)
    cpu, mem = defaultdict(float), defaultdict(int)
    pid_of = {}
    for p in procs:
        try:
            name = p.info["name"] or "?"
            cpu[name] += p.cpu_percent(None)
            mem[name] += p.memory_info().rss
            pid_of.setdefault(name, p.pid)
        except Exception:
            pass
    top_cpu = sorted(cpu.items(), key=lambda x: -x[1])[:7]
    top_mem = sorted(mem.items(), key=lambda x: -x[1])[:7]
    out = "🔥 *Топ по CPU*\n" + "\n".join(f"{n}: {v:.0f}% (PID {pid_of[n]})" for n, v in top_cpu)
    out += "\n\n🧠 *Топ по RAM*\n" + "\n".join(f"{n}: {fmt_bytes(v)} (PID {pid_of[n]})" for n, v in top_mem)
    return out


def do_battery(_):
    b = psutil.sensors_battery()
    if not b:
        return "🔌 Батареи нет (десктоп)"
    state = "заряжается" if b.power_plugged else "от батареи"
    left = f", осталось ~{int(b.secsleft // 60)} мин" if b.secsleft and b.secsleft > 0 else ""
    return f"🔋 {b.percent:.0f}% — {state}{left}"


def do_ip(_):
    local = sh("ipconfig getifaddr en0") or sh("ipconfig getifaddr en1") or socket.gethostname()
    try:
        pub = requests.get("https://api.ipify.org", timeout=10).text
    except Exception:
        pub = "?"
    return f"🌐 Локальный: {local}\nВнешний: {pub}"


def do_sleep(_):
    subprocess.Popen("sleep 3; pmset sleepnow", shell=True)
    return "💤 Усыпляю Mac через 3 сек. После этого он будет офлайн."


def do_screen_off(_):
    subprocess.Popen("pmset displaysleepnow", shell=True)
    return "🖥 Экран выключен"


def do_wake(_):
    # -u имитирует активность пользователя и включает экран, -d держит его включённым
    subprocess.Popen(["caffeinate", "-u", "-t", "10"])
    subprocess.Popen(["caffeinate", "-d", "-t", "10"])
    return "☀️ Экран включён"


def do_lock(_):
    osa('tell application "System Events" to keystroke "q" using {control down, command down}')
    return "🔒 Заблокировано (нужен доступ Accessibility, если не сработало)"


def do_keepawake(_):
    global caffeinate_proc
    if caffeinate_proc and caffeinate_proc.poll() is None:
        return "☕ Уже включено"
    caffeinate_proc = subprocess.Popen(["caffeinate", "-dims"])
    return "☕ Mac не будет засыпать"


def do_allowsleep(_):
    global caffeinate_proc
    if caffeinate_proc and caffeinate_proc.poll() is None:
        caffeinate_proc.terminate()
    return "😴 Сон снова разрешён"


def do_volume(a):
    try:
        v = max(0, min(100, int(a)))
    except ValueError:
        return "Использование: /volume 0-100"
    osa(f"set volume output volume {v}")
    return f"🔊 Громкость {v}%"


def do_mute(_):
    osa("set volume output muted true")
    return "🔇 Звук выключен"


def do_unmute(_):
    osa("set volume output muted false")
    return "🔊 Звук включён"


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def do_notify(a):
    if not a:
        return "Использование: /notify текст"
    osa(f'display notification "{esc(a)}" with title "Telegram"')
    return "🔔 Отправлено"


def do_say(a):
    if not a:
        return "Использование: /say текст"
    subprocess.Popen(["say", a])
    return "🗣 Говорю"


def do_restart(_):
    subprocess.Popen("sleep 3; osascript -e 'tell application \"System Events\" to restart'", shell=True)
    return "🔄 Перезагрузка через 3 сек"


def do_shutdown(_):
    subprocess.Popen("sleep 3; osascript -e 'tell application \"System Events\" to shut down'", shell=True)
    return "⏻ Выключение через 3 сек"


def do_kill(a):
    try:
        p = psutil.Process(int(a))
        name = p.name()
        p.terminate()
        return f"☠️ {name} (PID {a}) завершён"
    except Exception as e:
        return f"Не вышло: {e}"


HANDLERS = {
    "status": do_status, "top": do_top, "battery": do_battery, "ip": do_ip,
    "sleep": do_sleep, "screen_off": do_screen_off, "wake": do_wake, "screen_on": do_wake, "lock": do_lock,
    "keepawake": do_keepawake, "allowsleep": do_allowsleep, "volume": do_volume,
    "mute": do_mute, "unmute": do_unmute, "notify": do_notify, "say": do_say,
    "restart": do_restart, "shutdown": do_shutdown, "kill": do_kill,
    "browser_open": do_browser_open, "browser_close": do_browser_close,
    "browser_back": do_browser_back,
}


def run(job):
    cmd, args, chat_id = job["cmd"], job["args"], job["chat_id"]
    try:
        if cmd == "shot":
            path = "/tmp/tg_shot.png"
            subprocess.run(["screencapture", "-x", path], timeout=20)
            with open(path, "rb") as f:
                requests.post(f"{SERVER}/agent/photo", headers=HEADERS,
                              data={"chat_id": chat_id, "caption": "📸 Скриншот"},
                              files={"file": f}, timeout=60)
            os.remove(path)
            return
        reply(chat_id, HANDLERS[cmd](args))
    except Exception as e:
        reply(chat_id, f"⚠️ Ошибка: {e}")


def main():
    print("Agent started →", SERVER)
    while True:
        try:
            r = requests.get(f"{SERVER}/agent/poll", headers=HEADERS, timeout=40)
            if r.status_code == 403:
                print("Неверный AGENT_KEY")
                time.sleep(30)
                continue
            job = r.json().get("job")
            if job:
                run(job)
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
