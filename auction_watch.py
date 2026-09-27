#!/usr/bin/env python3
"""
Моніторинг аукціонів Prozorro.Продажі та оголошень про землю на OLX і Доброземі
для Городищенської громади та кількох сусідніх сіл (Черкаська обл.).

Що робить за один запуск:
  1. Читає зміни з API Prozorro.Продажі (byDateModified) від останнього курсора.
  2. Відбирає лоти з Черкаської області, де в адресі/назві є один з населених пунктів.
  3. Повідомляє в Telegram про НОВІ лоти (яких ще не бачив).
  4. Один раз нагадує про лоти, де до кінця прийому заявок <= 10 днів.
  5. Шукає на OLX продаж землі від 1 га в цих населених пунктах і повідомляє про нові оголошення.
  5a. Те саме на Доброземі (dobrozem.com.ua, фільтр «Черкаська область»).
  6. Зберігає стан у state.json (workflow комітить його назад у репозиторій).

Перевірка йде щодня, знахідки накопичуються в state["pending"], а звіт у Telegram
надсилається раз на SEND_EVERY_DAYS днів (перший запуск — одразу).

Змінні середовища:
  TELEGRAM_BOT_TOKEN — токен бота; звіт отримують усі, хто написав боту /start
                   (і канали/групи, куди бота додали адміністратором)
  TELEGRAM_CHAT_ID — необов'язково: постійні адресати через кому (@канал, -100…)
  BACKFILL_DAYS    — на скільки днів назад читати при першому запуску (типово 30)
  REMIND_DAYS      — за скільки днів до кінця заявок нагадувати (типово 10)
  MIN_HECTARES     — мінімальна площа для OLX і Доброзему (типово 1.0)
  SEND_EVERY_DAYS  — як часто надсилати звіт, у днях (типово 3; 1 — щодня)
  FORCE_SEND=1     — надіслати накопичений звіт зараз
  SUBSCRIBERS_ONLY=1 — лише забрати нові /start і /stop (запускається кожні 6 год)
  DRY_RUN=1        — нічого не надсилати і не зберігати стан, лише надрукувати
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- налаштування

# Територія: Городищенська громада + В'язівок, Карашин, Корсунь-Шевченківський, Воронівка.
# Населений пункт визначається з адреси лота (locality, назва КОАТУУ, вулиця),
# а якщо там нічого не знайдено — з назви лота.
#   • UNIQUE_PLACES — назви, що в області не повторюються: досить самої назви.
#   • COMMON_PLACES — поширені назви (Калинівка тощо): лот береться, лише якщо код
#     КОАТУУ з 71203 (колишній Городищенський р-н) або в тексті згадано громаду.
#   • Саме Городище: адреса «Городище», код 7120310100 або «межі Городищенської
#     міської ради». Згадка лише «Городищенський район» (старий район: Товста,
#     Мліїв, Вільшана…) лот НЕ відбирає.
UNIQUE_PLACES = {
    "Хлистунівка": r"хлистунів", "Дирдин": r"дирдин", "Ксаверове": r"ксаверов",
    "Цвіткове": r"цвітков", "Валява": r"валяв", "Орловець": r"орлов[еця]",
    "Набоків": r"набоків",
    "В'язівок": r"в['’ʼ`]?язів", "Карашин": r"карашин",
    "Корсунь-Шевченківський": r"корсун",
}
COMMON_PLACES = {
    "Калинівка": r"калинів", "Петропавлівка": r"петропавлів", "Воронівка": r"воронів",
}
KOATUU_PREFIXES = ("71203",)          # 71 — обл., 2 — район, 03 — Городищенський
HOROD_KOATUU = ("7120310100",)        # м. Городище
HROMADA_RE = re.compile(r"городищенськ\w*\s+(міськ|територіальн|громад)|вільшанськ", re.I)
HOROD_RE = re.compile(r"(^|[\s.,/])(м\.?\s*)?городище\b|городищенськ\w*\s+(міськ|територіальн|громад)", re.I)
REGION_RE = re.compile(r"черкас", re.I)
UNIQUE_RES = {n: re.compile(rx, re.I) for n, rx in UNIQUE_PLACES.items()}
COMMON_RES = {n: re.compile(rx, re.I) for n, rx in COMMON_PLACES.items()}

# OLX: пошук за текстом, далі фільтр за регіоном і назвою міста/села оголошення.
OLX_QUERIES = ["городище", "хлистунівка", "дирдин", "ксаверове", "цвіткове", "валява",
               "орловець", "набоків", "калинівка", "петропавлівка", "вязівок", "в'язівок",
               "карашин", "корсунь", "воронівка"]
OLX_CITY_RE = re.compile("|".join([*UNIQUE_PLACES.values(), *COMMON_PLACES.values(),
                                   r"^городище$"]), re.I)
OLX_LAND_SALE_CATEGORY = 1608  # «Продаж землі»
# OLX відхиляє запити з нетиповими заголовками — представляємося звичайним браузером.
OLX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Referer": "https://www.olx.ua/uk/nedvizhimost/zemlya/prodazha-zemli/chk/",
}

API = "https://procedure.prozorro.sale/api/search/byDateModified/{date}?limit=100"
AUCTION_URL = "https://prozorro.sale/auction/{id}"
OLX_API = "https://www.olx.ua/api/v1/offers/"
DOBROZEM_SEARCH = "https://dobrozem.com.ua/offer/search"
DOBROZEM_URL = "https://dobrozem.com.ua/offer/view/{id}"
DOBROZEM_REGION_ID = 16          # Черкаська область у фільтрі Доброзему
DOBROZEM_MAX_PAGES = 60          # по 10 оголошень на сторінці

STATE_FILE = Path(__file__).with_name("state.json")
BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "30"))
REMIND_DAYS = int(os.getenv("REMIND_DAYS", "10"))
MIN_HECTARES = float(os.getenv("MIN_HECTARES", "1.0"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
SEND_EVERY_DAYS = int(os.getenv("SEND_EVERY_DAYS", "3"))   # перевірка щодня, звіт раз на N днів
FORCE_SEND = os.getenv("FORCE_SEND") == "1"               # надіслати звіт зараз, не чекаючи
SUBSCRIBERS_ONLY = os.getenv("SUBSCRIBERS_ONLY") == "1"   # лише зібрати нові підписки (/start, /stop)

HEADERS = {"User-Agent": "Mozilla/5.0 (auction-watch; personal monitoring)",
           "Accept": "application/json"}

ACTIVE_STATUSES = {"active_rectification", "active_tendering", "active_auction"}

SALE_TYPES = {
    "landSell": "продаж землі", "landArrested": "продаж арештованої землі",
    "landRental": "оренда землі", "smallPrivatization": "мала приватизація",
    "legitimatePropertyLease": "оренда майна", "regulationsPropertyLease": "оренда майна",
    "commercialSell": "продаж майна", "basicSell": "продаж майна",
    "bankRuptcy": "банкрутство", "commercialPropertyLease": "оренда майна",
}

# ---------------------------------------------------------------- утиліти


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def uk(v) -> str:
    """Дістає український текст з полів виду {"uk_UA": "..."}."""
    if isinstance(v, dict):
        return v.get("uk_UA") or v.get("en_US") or ""
    return v or ""


def get(url: str, params=None, tries: int = 4, headers: dict | None = None):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or HEADERS, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 404):
                snippet = " ".join(r.text[:300].split())
                print(f"  HTTP {r.status_code} for {r.url}\n  server={r.headers.get('server')} "
                      f"body: {snippet}", file=sys.stderr)
                return None
        except requests.RequestException as e:
            print(f"  error {e}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cursor": None, "lots": {}, "olx_seen": [], "dobrozem_seen": [], "initialized": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
                          encoding="utf-8")


# ---------------------------------------------------------------- Telegram і підписники
# Підписатися: написати боту /start (або додати бота адміністратором у канал/групу).
# Відписатися: /stop. Список підписників лежить у state.json зашифрованим
# (репозиторій публічний), ключ виводиться з TELEGRAM_BOT_TOKEN.


def tg(method: str, **params):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=30)
        return r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Telegram {method}: {e}", file=sys.stderr)
        return None


def _fernet():
    from cryptography.fernet import Fernet
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"auction-watch:" + token.encode()).digest())
    return Fernet(key)


def load_subscribers(state: dict) -> set[str]:
    blob = state.get("subscribers_enc")
    if not blob or not os.getenv("TELEGRAM_BOT_TOKEN"):
        return set()
    try:
        return set(json.loads(_fernet().decrypt(blob.encode())))
    except Exception:  # інший токен або пошкоджені дані
        print("Не вдалося розшифрувати список підписників (змінився токен?)", file=sys.stderr)
        return set()


def save_subscribers(state: dict, subs: set[str]) -> None:
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        state["subscribers_enc"] = _fernet().encrypt(json.dumps(sorted(subs)).encode()).decode()


def chunk_text(text: str) -> list[str]:
    # Telegram обмежує повідомлення 4096 символами — ріжемо по рядках.
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_to(chat_id: str, text: str) -> bool:
    """Надсилає текст одному адресату. False — якщо адресат недоступний (заблокував бота тощо)."""
    for c in chunk_text(text):
        r = tg("sendMessage", chat_id=chat_id, text=c, parse_mode="HTML",
               disable_web_page_preview="true")
        if not r or not r.get("ok"):
            print(f"Telegram error ({chat_id}): {r}", file=sys.stderr)
            return not (r and r.get("error_code") in (400, 403))
    return True


PERIOD = "щодня" if SEND_EVERY_DAYS == 1 else f"раз на {SEND_EVERY_DAYS} дні"
WELCOME = ("✅ Ви підписані на звіти про аукціони Prozorro, OLX і Доброзем по Городищенській "
           f"громаді та сусідніх селах. Звіт приходить {PERIOD}. Відписатися: /stop")
BYE = "Ви відписані. Щоб знову отримувати звіти, надішліть /start."


def collect_subscribers(state: dict) -> set[str]:
    """Забирає нові звернення до бота (Telegram зберігає їх 24 год) і оновлює підписників."""
    subs = load_subscribers(state)
    offset = int(state.get("tg_offset", 0))
    r = tg("getUpdates", offset=offset, timeout=0,
           allowed_updates=json.dumps(["message", "channel_post", "my_chat_member"]))
    if not r or not r.get("ok"):
        print(f"getUpdates: {r}", file=sys.stderr)
        return subs
    welcome = []
    for u in r["result"]:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or u.get("channel_post")
        if msg:
            chat = str(msg["chat"]["id"])
            cmd = (msg.get("text") or "").split("@")[0].split()[0:1]
            if cmd == ["/start"]:
                if chat not in subs:
                    welcome.append(chat)
                subs.add(chat)
            elif cmd == ["/stop"]:
                subs.discard(chat)
                send_to(chat, BYE)
        mcm = u.get("my_chat_member")
        if mcm:  # бота додали в канал/групу або прибрали звідти
            chat = str(mcm["chat"]["id"])
            status = mcm["new_chat_member"]["status"]
            if status in ("administrator", "member") and mcm["chat"]["type"] != "private":
                if chat not in subs:
                    welcome.append(chat)
                subs.add(chat)
            elif status in ("left", "kicked"):
                subs.discard(chat)
    state["tg_offset"] = offset
    if r["result"]:
        tg("getUpdates", offset=offset, timeout=0)  # підтверджуємо, що звернення оброблено
    # Вітаємо лише тих, хто досі підписаний (/start і /stop могли прийти за одну добу).
    for chat in dict.fromkeys(c for c in welcome if c in subs):
        text = WELCOME
        if state.get("last_report"):
            text += f"\n\nОстанній звіт ({state.get('last_report_date', '')}):\n\n" + state["last_report"]
        if not send_to(chat, text):
            subs.discard(chat)
    print(f"Підписників: {len(subs)} (нових: {len(welcome)})")
    save_subscribers(state, subs)
    return subs


def send(text: str, state: dict | None = None) -> None:
    """Однаковий звіт усім: підписникам і адресам із TELEGRAM_CHAT_ID (через кому)."""
    fixed = [x for x in re.split(r"[,;\s]+", os.getenv("TELEGRAM_CHAT_ID", "")) if x]
    subs = load_subscribers(state) if state is not None else set()
    recipients = list(dict.fromkeys([*fixed, *sorted(subs)]))
    if DRY_RUN or not os.getenv("TELEGRAM_BOT_TOKEN") or not recipients:
        print(f"----- MESSAGE (адресатів: {len(recipients)}) -----\n" + text + "\n-------------------")
        return
    gone = {c for c in recipients if not send_to(c, text) and c in subs}
    if gone and state is not None:  # заблокували бота — прибираємо з підписників
        save_subscribers(state, subs - gone)
    print(f"Звіт надіслано: {len(recipients) - len(gone)} адресатам")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ---------------------------------------------------------------- Prozorro


# Згадки районів («Корсунь-Шевченківський район», «Городищенський район») не
# вказують на конкретний населений пункт — прибираємо їх перед пошуком назв.
RAION_RE = re.compile(r"[\w'’ʼ-]+ськ\w*\s+(р-н|район)\w*", re.I)


def match_place(proc: dict) -> str | None:
    """Повертає назву населеного пункту, якщо лот на нашій території, інакше None."""
    items = proc.get("items") or []
    region_parts, locality, street, koatuu = [], [], [], []
    for it in items:
        a = it.get("address") or {}
        region_parts.append(uk(a.get("region")))
        aid = a.get("addressID") or {}
        locality += [uk(a.get("locality")), uk(aid.get("name"))]
        street.append(uk(a.get("streetAddress")))
        if aid.get("scheme") == "koatuu" and aid.get("id"):
            koatuu.append(str(aid["id"]))
    if not items:  # запасний варіант — адреса продавця
        a = (proc.get("sellingEntity") or {}).get("address") or {}
        region_parts.append(uk(a.get("region")))
        locality.append(uk(a.get("locality")))
    title = uk(proc.get("title"))
    all_txt = " ".join([title, *locality, *street])
    if not REGION_RE.search(" ".join(region_parts) + " " + all_txt):
        return None
    in_old_raion = any(k.startswith(KOATUU_PREFIXES) for k in koatuu) or HROMADA_RE.search(all_txt)

    # Пріоритет: поле населеного пункту → вулиця/опис адреси → назва лота.
    # «Городище» — лише якщо конкретного села не знайдено ніде (код КОАТУУ міста
    # громада часто ставить і для своїх сіл, а село пише у вулиці).
    fields = [RAION_RE.sub(" ", t) for t in (" ".join(locality), " ".join(street), title)]
    for txt in fields:
        for name, rx in COMMON_RES.items():
            if rx.search(txt) and in_old_raion:
                return name
        for name, rx in UNIQUE_RES.items():
            if rx.search(txt):
                return name
    if any(k.startswith(HOROD_KOATUU) for k in koatuu) or any(HOROD_RE.search(t) for t in fields):
        return "Городище"
    return None


def summarize(proc: dict, place: str) -> dict:
    items = proc.get("items") or [{}]
    it = items[0]
    a = it.get("address") or {}
    props = it.get("itemProps") or {}
    area = props.get("landArea")
    if area is None and (it.get("unit") or {}).get("code") == "HAR":
        area = it.get("quantity")
    value = proc.get("value") or {}
    sm = proc.get("sellingMethod") or ""
    kind = (SALE_TYPES.get(proc.get("saleType") or "") or SALE_TYPES.get(sm.split("-")[0])
            or sm)
    if "priority" in sm:
        kind += " (з переважним правом)"
    return {
        "id": proc.get("auctionId"),
        "place": place,
        "title": " ".join(uk(proc.get("title")).split())[:160],
        "kind": kind,
        "address": ", ".join(x for x in [uk(a.get("locality")), uk(a.get("streetAddress"))] if x),
        "area_ha": area,
        "price": value.get("amount"),
        "vat": value.get("valueAddedTaxIncluded"),
        "status": proc.get("status"),
        "deadline": (proc.get("tenderPeriod") or {}).get("endDate")
                    or (proc.get("enquiryPeriod") or {}).get("endDate"),
        "auction_date": (proc.get("auctionPeriod") or {}).get("startDate"),
        "published": proc.get("datePublished"),
    }


def fetch_changes(since: datetime) -> tuple[list[dict], str]:
    """Читає всі зміни починаючи з `since`, повертає (відібрані лоти, новий курсор)."""
    cursor = since.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    found: dict[str, dict] = {}
    pages = 0
    while True:
        data = get(API.format(date=cursor))
        if data is None:
            raise RuntimeError("Prozorro API недоступний")
        pages += 1
        for proc in data:
            place = match_place(proc)
            if place and proc.get("auctionId"):
                found[proc["auctionId"]] = summarize(proc, place)
        if len(data) < 100:
            if data:
                cursor = data[-1]["dateModified"]
            break
        last = data[-1]["dateModified"]
        if last == cursor:  # захист від зациклення на однаковій даті
            break
        cursor = last
        if pages % 20 == 0:
            print(f"  ...{pages} сторінок, дійшли до {cursor}")
    print(f"Prozorro: прочитано {pages} сторінок, відібрано {len(found)} лотів")
    return list(found.values()), cursor


def fmt_money(x) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", " ").replace(".00", "") + " грн"


def fmt_date(s: str | None) -> str:
    d = parse_dt(s)
    if not d:
        return "—"
    return d.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")


def fmt_day(s: str | None) -> str | None:
    """Дата без часу (DD.MM.YYYY) за київським часом; None, якщо дати немає."""
    d = parse_dt(s)
    return d.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y") if d else None


def lot_line(l: dict) -> str:
    parts = [f"<b>{esc(l['place'])}</b> · {esc(l['kind'])}"]
    if l.get("area_ha"):
        parts.append(f"{l['area_ha']} га")
    parts.append(f"старт {fmt_money(l.get('price'))}")
    head = " · ".join(parts)
    pub = f"Опубліковано {fmt_day(l.get('published'))} · " if l.get("published") else ""
    return (f"• {head}\n  {esc(l['title'])}\n"
            f"  {pub}Заявки до {fmt_date(l.get('deadline'))}, торги {fmt_date(l.get('auction_date'))}\n"
            f"  {AUCTION_URL.format(id=l['id'])}")


def is_minor(l: dict) -> bool:
    """Дрібниця: погодинна оренда залів, щебінь, металобрухт тощо."""
    t = (l.get("title") or "").lower()
    return bool(re.search(r"погодин|щебін|щебен|брухт|вагон|рейк|шпал", t))

# ---------------------------------------------------------------- OLX


def olx_area_ha(offer: dict) -> float | None:
    area = None
    for p in offer.get("params") or []:
        if p.get("key") == "land_area":
            v = p.get("value") or {}
            raw = str(v.get("key") or v.get("label") or "")
            m = re.search(r"[\d.,]+", raw.replace(" ", ""))
            if m:
                area = float(m.group().replace(",", ".")) / 100  # сотки -> га
    # Продавці часто помиляються з одиницями — перевіряємо «X га» у назві.
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*га\b", offer.get("title") or "", re.I)
    if m:
        title_ha = float(m.group(1).replace(",", "."))
        if area is None or area > title_ha * 5:
            area = title_ha
    return area


def olx_search() -> list[dict] | None:
    results: dict[int, dict] = {}
    for q in OLX_QUERIES:
        for offset in range(0, 200, 50):
            data = get(OLX_API, params={"offset": offset, "limit": 50, "query": q,
                                        "category_id": OLX_LAND_SALE_CATEGORY},
                       headers=OLX_HEADERS, tries=2)
            if data is None:
                return None  # OLX заблокував запит — пропускаємо цей раз
            offers = data.get("data") or []
            for o in offers:
                loc = o.get("location") or {}
                region = (loc.get("region") or {}).get("name", "")
                city = (loc.get("city") or {}).get("name", "")
                if not REGION_RE.search(region) or not OLX_CITY_RE.search(city):
                    continue
                area = olx_area_ha(o)
                if area is None or area < MIN_HECTARES:
                    continue
                price = next((p.get("value", {}).get("label") for p in o.get("params", [])
                              if p.get("key") == "price"), None)
                results[o["id"]] = {"id": o["id"], "title": o.get("title", ""), "city": city,
                                    "area_ha": round(area, 4), "price": price, "url": o.get("url"),
                                    "created": o.get("created_time")}
            if len(offers) < 50:
                break
    print(f"OLX: знайдено {len(results)} оголошень від {MIN_HECTARES} га")
    return list(results.values())

# ---------------------------------------------------------------- Доброзем
# Сайт віддає список оголошень як звичайний HTML (Next.js SSR), по 10 на сторінку,
# найновіші першими. Адреси там за старими районами: «Городищенський район, с. Дирдин».


def text_place(addr: str) -> str | None:
    """Та сама логіка території, що й для Prozorro, але для рядка адреси."""
    near = bool(HROMADA_RE.search(addr) or re.search(r"городищенськ\w*\s+р", addr, re.I))
    txt = RAION_RE.sub(" ", addr)
    for name, rx in COMMON_RES.items():
        if rx.search(txt) and near:
            return name
    for name, rx in UNIQUE_RES.items():
        if rx.search(txt):
            return name
    if HOROD_RE.search(txt):
        return "Городище"
    return None


def dobrozem_created(offer_id: str) -> str | None:
    """Дата створення оголошення (DD.MM.YYYY) зі сторінки оголошення; у списку її немає."""
    try:
        r = requests.get(DOBROZEM_URL.format(id=offer_id),
                         headers={**HEADERS, "Accept": "text/html"}, timeout=60)
        m = re.search(r"Створено:\s*(?:<!-- -->)?\s*(\d{2}\.\d{2}\.\d{4})", r.text)
        return m.group(1) if m else None
    except requests.RequestException:
        return None


def parse_dobrozem(page_html: str) -> list[dict]:
    out = []
    for card in re.split(r'<a[^>]+href="/offer/view/', page_html)[1:]:
        m = re.match(r"(L-\d+)", card)
        if not m:
            continue
        chunk = card[:8000].replace("<!-- -->", "")
        spans = [html.unescape(x.strip()) for x in re.findall(r"<span[^>]*>([^<]{2,})</span>", chunk)]
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)))
        area = re.search(r"([\d.]+)\s*га", text)
        price = re.search(r"([\d\s\u00a0]+₴)\s*\(", text)
        yld = re.search(r"Дохідність:\s*([\d.]+)", text)
        out.append({
            "id": m.group(1),
            "status": spans[0] if spans else "",
            "address": next((x for x in spans if "область" in x), ""),
            "area_ha": float(area.group(1)) if area else None,
            "price": " ".join(price.group(1).split()) if price else None,
            "yield": yld.group(1) if yld else None,
        })
    return out


def dobrozem_search(max_seen: int) -> tuple[list[dict], int] | None:
    """Повертає (наші оголошення від MIN_HECTARES, найбільший номер ID на сайті).
    ID зростають (L-000020726), список — найновіші першими, тож після першого
    запуску досить дійти до сторінки з оголошеннями, старішими за max_seen."""
    found, top = [], max_seen
    for page in range(1, DOBROZEM_MAX_PAGES + 1):
        try:
            r = requests.get(DOBROZEM_SEARCH, params={"regionId": DOBROZEM_REGION_ID, "page": page},
                             headers={**HEADERS, "Accept": "text/html"}, timeout=60)
        except requests.RequestException as e:
            print("Доброзем:", e, file=sys.stderr)
            return None if page == 1 else (found, top)
        if r.status_code != 200:
            print(f"Доброзем: HTTP {r.status_code}", file=sys.stderr)
            return None if page == 1 else (found, top)
        cards = parse_dobrozem(r.text)
        if not cards:
            break
        nums = [int(c["id"][2:]) for c in cards]
        top = max(top, *nums)
        for c in cards:
            place = text_place(c["address"])
            if (place and c["area_ha"] and c["area_ha"] >= MIN_HECTARES
                    and not re.search(r"продан", c["status"], re.I)):
                found.append({**c, "place": place})
        if max_seen and min(nums) <= max_seen:
            break
        time.sleep(1)
    print(f"Доброзем: знайдено {len(found)} оголошень від {MIN_HECTARES} га")
    return found, top


# ---------------------------------------------------------------- main


def main() -> None:
    state = load_state()
    if not DRY_RUN:  # у пробному режимі нікому не відповідаємо і звернень не підтверджуємо
        collect_subscribers(state)
    if SUBSCRIBERS_ONLY:
        if not DRY_RUN:
            save_state(state)
        return
    first_run = not state.get("initialized")
    since = parse_dt(state.get("cursor")) or (now() - timedelta(days=BACKFILL_DAYS))
    pending = state.setdefault("pending", {"lots": [], "reminders": [], "olx": [], "dz": []})

    lots, cursor = fetch_changes(since)

    for l in lots:
        known = state["lots"].get(l["id"])
        if known is None:
            state["lots"][l["id"]] = {**l, "reminded": False}
            if l["status"] in ACTIVE_STATUSES:
                pending["lots"].append(l["id"])
        else:
            known.update({k: v for k, v in l.items()})

    # Нагадування (один раз на лот) про кінець прийому заявок — ставимо в чергу.
    t = now()
    for lid, l in list(state["lots"].items()):
        dl = parse_dt(l.get("deadline"))
        if not dl:
            continue
        if dl < t - timedelta(days=30):
            del state["lots"][lid]  # старі лоти прибираємо зі стану
            continue
        if l.get("reminded") or l.get("status") not in ACTIVE_STATUSES:
            continue
        if t < dl <= t + timedelta(days=REMIND_DAYS):
            l["reminded"] = True
            if lid not in pending["lots"]:
                pending["reminders"].append(lid)

    olx = olx_search()
    if olx is not None:
        seen = set(state.get("olx_seen", []))
        pending["olx"] += [o for o in olx if o["id"] not in seen]
        state["olx_seen"] = sorted(seen | {o["id"] for o in olx})

    dz_seen = set(state.get("dobrozem_seen", []))
    res = dobrozem_search(int(state.get("dobrozem_max_id", 0)))
    dz = None
    if res is not None:
        dz, state["dobrozem_max_id"] = res
        pending["dz"] += [{**d, "created": dobrozem_created(d["id"])}
                          for d in dz if d["id"] not in dz_seen]
        state["dobrozem_seen"] = sorted(dz_seen | {d["id"] for d in dz})

    # ---- чи час надсилати звіт
    last_sent = parse_dt(state.get("last_sent"))
    due = (first_run or FORCE_SEND or last_sent is None
           or t - last_sent >= timedelta(days=SEND_EVERY_DAYS) - timedelta(hours=3))
    if due:
        report = build_report(state, pending, first_run, olx is None, dz is None)
        send(report, state)
        state["last_report"] = report
        state["last_report_date"] = t.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y")
        state["last_sent"] = t.isoformat()
        state["pending"] = {"lots": [], "reminders": [], "olx": [], "dz": []}
    else:
        n = sum(len(v) for v in pending.values())
        print(f"Звіт не надсилаю (останній {last_sent:%d.%m %H:%M} UTC); у черзі {n} позицій.")

    if not DRY_RUN:
        state["cursor"] = cursor
        state["initialized"] = True
        save_state(state)


def build_report(state: dict, pending: dict, first_run: bool, olx_failed: bool,
                 dz_failed: bool) -> str:
    t = now()

    def still_open(lid: str) -> dict | None:
        """Лот з актуальними даними, якщо він досі активний і строк заявок не минув."""
        l = state["lots"].get(lid)
        if not l or l.get("status") not in ACTIVE_STATUSES:
            return None
        dl = parse_dt(l.get("deadline"))
        return l if (dl is None or dl > t) else None

    new_lots = [l for l in map(still_open, dict.fromkeys(pending["lots"])) if l]
    new_ids = {l["id"] for l in new_lots}
    reminders = [l for l in map(still_open, dict.fromkeys(pending["reminders"]))
                 if l and l["id"] not in new_ids]
    new_olx = list({o["id"]: o for o in pending["olx"]}.values())
    new_dz = list({d["id"]: d for d in pending["dz"]}.values())

    msg = []
    title = "Стартовий знімок: активні лоти" if first_run else "Нові аукціони"
    major = [l for l in new_lots if not is_minor(l)]
    minor = [l for l in new_lots if is_minor(l)]
    if major:
        msg.append(f"🏷 <b>{title} ({len(major)})</b>")
        msg += [lot_line(l) for l in sorted(major, key=lambda x: x.get("deadline") or "")]
    if minor:
        places = sorted({l["place"] for l in minor})
        msg.append(f"\nДрібні лоти (погодинна оренда, щебінь тощо): {len(minor)} — "
                   f"{', '.join(places)}")
    if reminders:
        msg.append(f"\n⏰ <b>До кінця заявок ≤ {REMIND_DAYS} днів ({len(reminders)})</b>")
        msg += [lot_line(l) for l in sorted(reminders, key=lambda x: x.get("deadline") or "")]
    if new_olx:
        msg.append(f"\n🌾 <b>OLX: {'земля' if first_run else 'нова земля'} від "
                   f"{MIN_HECTARES:g} га ({len(new_olx)})</b>")
        for o in new_olx:
            pub = f" · опубл. {fmt_day(o['created'])}" if o.get("created") else ""
            msg.append(f"• <b>{esc(o['city'])}</b> · {o['area_ha']} га · {esc(o['price'] or '—')}{pub}\n"
                       f"  {esc(o['title'])}\n  {o['url']}")
    if new_dz:
        msg.append(f"\n🟩 <b>Доброзем: {'земля' if first_run else 'нова земля'} від "
                   f"{MIN_HECTARES:g} га ({len(new_dz)})</b>")
        for d in new_dz:
            y = f" · дохідність {d['yield']}%" if d.get("yield") else ""
            y += f" · створено {d['created']}" if d.get("created") else ""
            msg.append(f"• <b>{esc(d['place'])}</b> · {d['area_ha']} га · {esc(d['price'] or '—')}{y}\n"
                       f"  {esc(d['address'])}\n  {DOBROZEM_URL.format(id=d['id'])}")
    if not msg:
        since = "за добу" if SEND_EVERY_DAYS == 1 else f"за останні {SEND_EVERY_DAYS} дні"
        msg.append(f"Prozorro/OLX/Доброзем: {since} нових лотів і оголошень немає.")
    if olx_failed:
        msg.append("\n(OLX сьогодні не відповів — перевірю завтра.)")
    if dz_failed:
        msg.append("(Доброзем сьогодні не відповів — перевірю завтра.)")
    return "\n".join(msg).strip()


if __name__ == "__main__":
    main()
