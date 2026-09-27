#!/usr/bin/env python3
"""
Перевірка OLX (продаж землі від 1 га) — запускається на домашньому Mac.

OLX блокує сервери GitHub, тому цю частину виконує комп'ютер із домашньою
IP-адресою. Скрипт:
  1. Шукає на OLX продаж землі в тих самих населених пунктах, що й основний бот.
  2. Бере список отримувачів із зашифрованого state.json у репозиторії
     (той самий, що веде GitHub-частина), тож OLX приходить тим самим людям і групам.
  3. Надсилає лише НОВІ оголошення; якщо нових немає — мовчить.
  4. Пам'ятає побачені оголошення в локальному файлі.

Налаштування: файл config поруч зі скриптом (його створює install_olx_mac.sh):
  TELEGRAM_BOT_TOKEN=...        — токен того самого бота
  TELEGRAM_CHAT_ID=...          — необов'язково, додаткові адресати через кому
  MIN_HECTARES=1                — мінімальна площа
Ключ DRY_RUN=1 у середовищі — лише надрукувати, нічого не надсилати.
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config"
STATE_FILE = HERE / "olx_state.json"
REPO_STATE_URL = "https://raw.githubusercontent.com/bmarchenko2/auction-watch/main/state.json"

# Той самий список, що й в auction_watch.py (Городищенська громада + В'язівок,
# Карашин, Корсунь-Шевченківський, Воронівка). Для OLX фільтр — за назвою міста/села.
PLACES = [r"хлистунів", r"дирдин", r"ксаверов", r"цвітков", r"валяв", r"орлов[еця]",
          r"набоків", r"в['’ʼ`]?язів", r"карашин", r"корсун", r"калинів", r"петропавлів",
          r"воронів", r"^городище$"]
CITY_RE = re.compile("|".join(PLACES), re.I)
REGION_RE = re.compile(r"черкас", re.I)
QUERIES = ["городище", "хлистунівка", "дирдин", "ксаверове", "цвіткове", "валява",
           "орловець", "набоків", "калинівка", "петропавлівка", "вязівок", "в'язівок",
           "карашин", "корсунь", "воронівка"]
OLX_API = "https://www.olx.ua/api/v1/offers/"
LAND_SALE_CATEGORY = 1608
OLX_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Referer": "https://www.olx.ua/uk/nedvizhimost/zemlya/prodazha-zemli/chk/",
}


def load_config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                                                            "MIN_HECTARES", "DRY_RUN")})
    return cfg


CFG = load_config()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
MIN_HECTARES = float(CFG.get("MIN_HECTARES", "1") or 1)
DRY_RUN = CFG.get("DRY_RUN") == "1"


def http_json(url: str, params: dict | None = None, headers: dict | None = None,
              data: dict | None = None, tries: int = 3):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (400, 403, 404):
                try:
                    return json.loads(text)
                except ValueError:
                    print(f"HTTP {e.code} {url[:90]}: {' '.join(text[:200].split())}", file=sys.stderr)
                    return None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"Мережа: {e}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    return None


# ---------------------------------------------------------------- отримувачі


def subscribers() -> list[str]:
    """Той самий список, що веде GitHub-частина (state.json, зашифровано токеном)."""
    fixed = [x for x in re.split(r"[,;\s]+", CFG.get("TELEGRAM_CHAT_ID", "")) if x]
    subs: list[str] = []
    try:
        req = urllib.request.Request(REPO_STATE_URL + f"?t={int(time.time())}")
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = json.loads(r.read().decode("utf-8")).get("subscribers_enc")
        if blob:
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(hashlib.sha256(b"auction-watch:" + TOKEN.encode()).digest())
            subs = json.loads(Fernet(key).decrypt(blob.encode()))
    except Exception as e:  # немає мережі, змінився токен тощо
        print(f"Не вдалося прочитати список підписників: {e}", file=sys.stderr)
    return list(dict.fromkeys([*fixed, *subs]))


def send(text: str, recipients: list[str]) -> bool:
    """True, якщо хоч один адресат отримав повідомлення (або це пробний запуск)."""
    if DRY_RUN or not TOKEN or not recipients:
        print(f"----- MESSAGE (адресатів: {len(recipients)}) -----\n{text}\n-------------------")
        return DRY_RUN
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    delivered = 0
    for rcpt in recipients:
        for c in chunks:
            r = http_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": rcpt, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"})
            if not r or not r.get("ok"):
                print(f"Telegram ({rcpt}): {r}", file=sys.stderr)
                break
        else:
            delivered += 1
    print(f"Надіслано {delivered} з {len(recipients)} адресатів")
    return delivered > 0

# ---------------------------------------------------------------- OLX


def area_ha(offer: dict) -> float | None:
    area = None
    for p in offer.get("params") or []:
        if p.get("key") == "land_area":
            v = p.get("value") or {}
            m = re.search(r"[\d.,]+", str(v.get("key") or v.get("label") or "").replace(" ", ""))
            if m:
                area = float(m.group().replace(",", ".")) / 100  # сотки -> га
    # Продавці часто помиляються з одиницями — звіряємо з «X га» у назві.
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*га\b", offer.get("title") or "", re.I)
    if m:
        t = float(m.group(1).replace(",", "."))
        if area is None or area > t * 5:
            area = t
    return area


def search() -> list[dict] | None:
    found: dict[int, dict] = {}
    for q in QUERIES:
        for offset in range(0, 200, 50):
            data = http_json(OLX_API, {"offset": offset, "limit": 50, "query": q,
                                       "category_id": LAND_SALE_CATEGORY}, OLX_HEADERS)
            if data is None or "data" not in data:
                return None
            for o in data["data"]:
                loc = o.get("location") or {}
                region = (loc.get("region") or {}).get("name", "")
                city = (loc.get("city") or {}).get("name", "")
                if not REGION_RE.search(region) or not CITY_RE.search(city):
                    continue
                a = area_ha(o)
                if a is None or a < MIN_HECTARES:
                    continue
                price = next((p.get("value", {}).get("label") for p in o.get("params", [])
                              if p.get("key") == "price"), None)
                found[o["id"]] = {"id": o["id"], "title": o.get("title", ""), "city": city,
                                  "area_ha": round(a, 4), "price": price, "url": o.get("url"),
                                  "created": o.get("created_time")}
            if len(data["data"]) < 50:
                break
            time.sleep(0.5)
    print(f"OLX: {len(found)} оголошень від {MIN_HECTARES:g} га")
    return list(found.values())


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def day(s: str | None) -> str | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return None


def main() -> None:
    if not TOKEN and not DRY_RUN:
        sys.exit("Немає TELEGRAM_BOT_TOKEN у файлі config — запустіть install_olx_mac.sh ще раз.")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    first = "seen" not in state
    seen = set(state.get("seen", []))

    offers = search()
    if offers is None:
        print("OLX не відповів — спробую наступного разу.", file=sys.stderr)
        sys.exit(1)
    new = [o for o in offers if o["id"] not in seen]

    if new:
        head = (f"OLX: земля від {MIN_HECTARES:g} га ({len(new)}) — стартовий знімок" if first
                else f"OLX: нова земля від {MIN_HECTARES:g} га ({len(new)})")
        lines = [f"🌾 <b>{head}</b>"]
        for o in sorted(new, key=lambda x: x.get("created") or "", reverse=True):
            pub = f" · опубл. {day(o['created'])}" if day(o.get("created")) else ""
            lines.append(f"• <b>{esc(o['city'])}</b> · {o['area_ha']} га · {esc(o['price'] or '—')}{pub}\n"
                         f"  {esc(o['title'])}\n  {o['url']}")
        if not send("\n".join(lines), subscribers()):
            # Нікому не доставлено (немає мережі чи списку отримувачів) —
            # не позначаємо оголошення побаченими, надішлемо наступного разу.
            sys.exit("Не вдалося надіслати — спробую наступного разу.")
    else:
        print("Нових оголошень немає.")

    if not DRY_RUN:
        state["seen"] = sorted(seen | {o["id"] for o in offers})
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
