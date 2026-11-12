#!/usr/bin/env python3
"""
Telegram-бот: следит за поисками на krisha.kz и присылает новые объявления.

Запуск:
    python bot.py           — работать постоянно (проверка каждые N секунд)
    python bot.py chatid    — показать chat_id (сначала напиши боту любое сообщение)
    python bot.py once      — одна проверка и выход (для cron / планировщика)
    python bot.py ci        — работать RUN_MINUTES минут и выйти (для GitHub Actions)

Команды в Telegram:
    /add Название | https://krisha.kz/...   — добавить поиск
    /list                                    — список поисков
    /del Название                            — удалить поиск
    /check                                   — проверить прямо сейчас

Нужен только Python 3.8+, сторонние библиотеки не нужны.
"""
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
SEARCHES_FILE = os.path.join(DATA_DIR, "searches.json")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

BASE = "https://krisha.kz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}


def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


# ---------- файлы ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_FILE, {}) or {}
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg["telegram_token"] = os.environ["TELEGRAM_TOKEN"].strip()
    if os.environ.get("CHAT_ID"):
        cfg["chat_id"] = os.environ["CHAT_ID"].strip()
    if not cfg.get("telegram_token") or "ВСТАВЬ" in cfg["telegram_token"]:
        sys.exit("Нет токена: задай TELEGRAM_TOKEN или telegram_token в config.json")
    cfg.setdefault("interval_seconds", 180)
    cfg["interval_seconds"] = max(60, int(cfg["interval_seconds"]))
    return cfg


# ---------- krisha.kz ----------

def normalize_url(url):
    """Убираем sort_by: по умолчанию krisha показывает новые сверху — это и нужно."""
    u = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
         if k not in ("sort_by", "page")]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


class CardParser(HTMLParser):
    """Достаёт карточки объявлений (div.a-card[data-id]) со страницы поиска."""
    FIELDS = {
        "a-card__title": "title",
        "a-card__price": "price",
        "a-card__subtitle": "address",
        "a-card__text-preview": "text",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.card_depth = 0
        self.field = None
        self.field_depth = 0
        self.depth = 0
        self.hot_depth = None  # внутри section.highlighted-section (платный «горячий» блок)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if self.hot_depth is None and "highlighted-section" in cls and tag not in VOID:
            self.hot_depth = self.depth
        if self.card is None:
            if "a-card" in cls and a.get("data-id"):
                self.card = {"id": a["data-id"], "title": "", "price": "",
                             "address": "", "text": "", "url": "",
                             "hot": self.hot_depth is not None}
                self.card_depth = self.depth
        else:
            href = a.get("href") or ""
            if tag == "a" and "/a/show/" in href:
                if "hot_advert" in href or "hot_block" in href:
                    self.card["hot"] = True  # платный «горячий» блок, крутится случайно
                if not self.card["url"]:
                    self.card["url"] = urllib.parse.urljoin(BASE, href.split("?")[0])
            if self.field is None:
                for c in cls:
                    if c in self.FIELDS:
                        self.field = self.FIELDS[c]
                        self.field_depth = self.depth
                        break
        if tag not in VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        self.depth -= 1
        if self.hot_depth is not None and self.depth == self.hot_depth:
            self.hot_depth = None
        if self.field is not None and self.depth == self.field_depth:
            self.field = None
        if self.card is not None and self.depth == self.card_depth:
            self.cards.append(self.card)
            self.card = None
            self.field = None

    def handle_data(self, data):
        if self.card is not None and self.field:
            self.card[self.field] += data


def parse_listings(page):
    p = CardParser()
    try:
        p.feed(page)
    except Exception as e:
        log("Ошибка разбора HTML:", e)
    cards, seen_ids = [], set()
    for c in p.cards:
        if c["hot"] or c["id"] in seen_ids:
            continue
        seen_ids.add(c["id"])
        for k in ("title", "price", "address", "text"):
            c[k] = " ".join(c[k].split())
        if not c["url"]:
            c["url"] = f"{BASE}/a/show/{c['id']}"
        cards.append(c)
    if not cards and not p.cards:
        # запасной вариант, если вёрстка поменялась: просто ссылки на объявления
        for id_ in dict.fromkeys(re.findall(r'/a/show/(\d+)', page)):
            cards.append({"id": id_, "title": "", "price": "", "address": "",
                          "text": "", "url": f"{BASE}/a/show/{id_}"})
    return cards


# ---------- Telegram ----------

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, http_timeout=30, **params):
        data = urllib.parse.urlencode(params).encode()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(self.base + method, data=data, timeout=http_timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = json.loads(e.read().decode() or "{}")
                wait = body.get("parameters", {}).get("retry_after")
                if e.code == 429 and wait:
                    time.sleep(wait + 1)
                    continue
                raise RuntimeError(f"Telegram {method}: {e.code} {body.get('description')}")
        raise RuntimeError(f"Telegram {method}: слишком много попыток")

    def send(self, chat_id, text, preview=True):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview="false" if preview else "true")


def format_card(c, search_name):
    e = html.escape
    lines = []
    lines.append(f"🏠 <b>{e(c['title'])}</b>" if c["title"] else "🏠 <b>Новое объявление</b>")
    if c["price"]:
        lines.append(f"💰 {e(c['price'])}")
    if c["address"]:
        lines.append(f"📍 {e(c['address'])}")
    if c["text"]:
        t = c["text"]
        lines.append(f"\n{e(t[:300] + ('…' if len(t) > 300 else ''))}")
    lines.append(f"\n🔗 {c['url']}")
    lines.append(f"🔎 <i>{e(search_name)}</i>")
    return "\n".join(lines)


# ---------- логика ----------

class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg["telegram_token"])
        self.state = load_json(STATE_FILE, {})
        # chat_id: из настроек, иначе — чат того, кто первым написал боту
        self.chat_id = str(cfg.get("chat_id") or self.state.get("chat_id") or "")
        self.searches = load_json(SEARCHES_FILE, cfg.get("searches", []))
        self.seen = load_json(SEEN_FILE, {})
        self.offset = self.state.get("offset")

    def notify(self, text, preview=False):
        if not self.chat_id:
            log("chat_id не задан, сообщение не отправлено:", text[:80])
            return
        try:
            self.tg.send(self.chat_id, text, preview)
        except Exception as e:
            log("Не удалось отправить в Telegram:", e)

    def check_all(self):
        for s in list(self.searches):
            self.check_one(s["name"], s["url"])
            time.sleep(random.uniform(2, 5))

    def check_one(self, name, url):
        try:
            page = http_get(normalize_url(url))
        except Exception as e:
            log(f"[{name}] не удалось открыть страницу:", e)
            return
        cards = parse_listings(page)
        if not cards:
            log(f"[{name}] объявлений не найдено (пустой поиск или сайт отдал капчу)")
            return
        ids = [c["id"] for c in cards]
        known = self.seen.get(name)
        if known is None:
            # первый запуск: запоминаем текущие, чтобы не заспамить
            self.seen[name] = ids
            save_json(SEEN_FILE, self.seen)
            log(f"[{name}] начал следить, сейчас {len(ids)} объявлений")
            self.notify(f"👀 Слежу за «{html.escape(name)}». Сейчас на странице {len(ids)} "
                        f"объявлений — буду присылать только новые.")
            return
        known_set = set(known)
        new = [c for c in cards if c["id"] not in known_set]
        if new:
            log(f"[{name}] новых: {len(new)}")
        for c in reversed(new):  # от старых к новым
            try:
                self.tg.send(self.chat_id, format_card(c, name))
            except Exception as e:
                log(f"[{name}] не отправилось {c['id']}:", e)
                continue
            known.insert(0, c["id"])
            time.sleep(1)
        self.seen[name] = known[:3000]
        save_json(SEEN_FILE, self.seen)

    # --- команды из Telegram ---

    def poll_commands(self, seconds):
        """Слушает команды до следующей проверки (long polling)."""
        deadline = time.time() + seconds
        first = True
        while True:
            left = int(deadline - time.time())
            if left <= 0 and not first:
                return
            first = False
            try:
                params = {"timeout": max(0, min(left, 50)), "allowed_updates": '["message"]'}
                if self.offset:
                    params["offset"] = self.offset
                res = self.tg.call("getUpdates", http_timeout=60, **params)
            except Exception as e:
                log("getUpdates:", e)
                time.sleep(min(10, max(left, 1)))
                continue
            for u in res.get("result", []):
                self.offset = u["update_id"] + 1
                self.state["offset"] = self.offset
                msg = u.get("message") or {}
                chat = str(msg.get("chat", {}).get("id", ""))
                if not self.chat_id and chat and msg.get("chat", {}).get("type") == "private":
                    self.chat_id = chat
                    self.state["chat_id"] = chat
                    log("Владелец бота:", chat)
                save_json(STATE_FILE, self.state)
                if chat == self.chat_id and msg.get("text"):
                    self.handle_command(msg["text"].strip())

    def handle_command(self, text):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/add":
            name, sep, url = arg.partition("|")
            name, url = name.strip(), url.strip()
            if not sep or not url.startswith("https://krisha.kz/"):
                self.notify("Формат: /add Название | https://krisha.kz/...")
                return
            self.searches = [s for s in self.searches if s["name"] != name]
            self.searches.append({"name": name, "url": url})
            save_json(SEARCHES_FILE, self.searches)
            self.seen.pop(name, None)
            self.check_one(name, url)
        elif cmd == "/del":
            before = len(self.searches)
            self.searches = [s for s in self.searches if s["name"] != arg]
            save_json(SEARCHES_FILE, self.searches)
            self.seen.pop(arg, None)
            save_json(SEEN_FILE, self.seen)
            self.notify("Удалил." if len(self.searches) < before else f"Нет поиска «{html.escape(arg)}». /list")
        elif cmd == "/list":
            if not self.searches:
                self.notify("Поисков пока нет. Добавь: /add Название | https://krisha.kz/...")
            else:
                self.notify("\n\n".join(f"• <b>{html.escape(s['name'])}</b>\n{html.escape(s['url'])}"
                                        for s in self.searches))
        elif cmd == "/check":
            self.notify("Проверяю…")
            self.check_all()
            self.notify("Готово.")
        elif cmd in ("/start", "/help"):
            self.notify("Команды:\n/add Название | ссылка с krisha.kz\n/list — мои поиски\n"
                        "/del Название — удалить\n/check — проверить сейчас")

    def run_forever(self, minutes=None):
        """Работать бесконечно или заданное число минут (для GitHub Actions)."""
        end = time.time() + minutes * 60 if minutes else None
        log(f"Бот запущен. Поисков: {len(self.searches)}, интервал {self.cfg['interval_seconds']} с")
        self.poll_commands(1)  # сначала прочитать накопившиеся команды
        while True:
            self.check_all()
            wait = self.cfg["interval_seconds"] + random.randint(-15, 15)
            if end is not None:
                wait = min(wait, end - time.time())
                if wait <= 5:
                    return
            self.poll_commands(wait)


def show_chat_id(cfg):
    res = Telegram(cfg["telegram_token"]).call("getUpdates")
    chats = {}
    for u in res.get("result", []):
        ch = (u.get("message") or {}).get("chat")
        if ch:
            chats[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name")
    if not chats:
        print("Пусто. Напиши своему боту любое сообщение в Telegram и запусти команду ещё раз.")
    for cid, who in chats.items():
        print(f"chat_id: {cid}   ({who})")


def main():
    cfg = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "chatid":
        show_chat_id(cfg)
        return
    bot = Bot(cfg)
    if mode == "once":
        bot.poll_commands(1)
        bot.check_all()
    elif mode == "ci":
        bot.run_forever(minutes=float(os.environ.get("RUN_MINUTES", "9")))
    else:
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            log("Остановлен.")


if __name__ == "__main__":
    main()
