"""
Bot Emploi228 - publie automatiquement les nouvelles offres d'emploi
de la CEDEAO sur le canal Telegram @Emploi228.

Fonctionnement :
  1. lit la liste des sources (flux RSS) dans sources.txt
  2. repere les offres jamais publiees (memoire dans seen.json)
  3. publie chaque nouvelle offre sur le canal Telegram
"""

import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

BASE = Path(__file__).parent
SOURCES_FILE = BASE / "sources.txt"
SEEN_FILE = BASE / "seen.json"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@Emploi228").strip()
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"           # 1 = teste sans publier
MAX_POSTS = int(os.environ.get("MAX_POSTS", "20"))        # max d'offres par passage
FIRST_RUN_POSTS = int(os.environ.get("FIRST_RUN_POSTS", "5"))  # offres publiees au tout 1er passage
MAX_SEEN = 8000
USER_AGENT = "Mozilla/5.0 (compatible; Emploi228Bot/1.0; +https://t.me/Emploi228)"

COUNTRIES = {
    "Togo": ["togo", "lomé", "lome", "kara", "sokodé", "atakpamé"],
    "Bénin": ["bénin", "benin", "cotonou", "porto-novo", "parakou"],
    "Côte d'Ivoire": ["côte d'ivoire", "cote d'ivoire", "côte d’ivoire", "ivoirien", "abidjan", "yamoussoukro", "bouaké"],
    "Sénégal": ["sénégal", "senegal", "dakar", "thiès"],
    "Burkina Faso": ["burkina", "ouagadougou", "bobo-dioulasso"],
    "Mali": ["mali", "bamako"],
    "Niger": ["niger ", "niamey"],
    "Nigeria": ["nigeria", "nigéria", "lagos", "abuja"],
    "Ghana": ["ghana", "accra", "kumasi"],
    "Guinée": ["guinée", "guinee", "conakry"],
    "Guinée-Bissau": ["guinée-bissau", "guinee-bissau", "bissau"],
    "Sierra Leone": ["sierra leone", "freetown"],
    "Liberia": ["liberia", "libéria", "monrovia"],
    "Gambie": ["gambie", "gambia", "banjul"],
    "Cap-Vert": ["cap-vert", "cabo verde", "cape verde", "praia"],
}


def log(msg):
    print(msg, flush=True)


def load_sources():
    urls = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def load_seen():
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return []


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=0), encoding="utf-8")


def offer_id(entry):
    key = entry.get("link") or entry.get("id") or entry.get("title", "")
    return hashlib.sha1(key.strip().lower().encode("utf-8")).hexdigest()[:16]


def clean_text(raw, limit):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    # retire les mentions automatiques WordPress du type "The post ... appeared first on ..."
    text = re.sub(r"(L’article|L'article|The post) .*$", "", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def detect_countries(text):
    low = f" {text.lower()} "
    found = [name for name, keys in COUNTRIES.items() if any(k in low for k in keys)]
    return found[:3]


def fetch_feed(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"  ⚠️  source ignorée ({url}) : {e}")
        return []
    feed = feedparser.parse(r.content)
    site = clean_text(feed.feed.get("title", ""), 60) or url.split("/")[2]
    entries = []
    for e in feed.entries:
        e["_site"] = site
        entries.append(e)
    log(f"  ✅ {site} : {len(entries)} offres lues")
    return entries


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return time.mktime(t) if t else 0


def format_message(e):
    title = clean_text(e.get("title", "Offre d'emploi"), 200)
    summary = clean_text(e.get("summary", ""), 220)
    link = e.get("link", "")
    countries = detect_countries(f"{title} {summary}")

    lines = [f"💼 <b>{html.escape(title, quote=False)}</b>", ""]
    if countries:
        lines.append(f"🌍 {html.escape(', '.join(countries), quote=False)}")
    if summary:
        lines.append(f"📝 {html.escape(summary, quote=False)}")
    lines += [
        "",
        f"🔗 <a href=\"{html.escape(link, quote=True)}\">Voir l'offre et postuler</a>",
        f"📰 Source : {html.escape(e['_site'], quote=False)}",
        "",
    ]
    tags = ["#Emploi", "#CEDEAO"] + ["#" + re.sub(r"[^\wÀ-ÿ]", "", c) for c in countries]
    lines.append(" ".join(tags))
    lines.append("👉 Rejoignez @Emploi228 pour ne rien manquer")
    return "\n".join(lines)


def send(text):
    if DRY_RUN:
        log("----- (TEST, non publié) -----\n" + text)
        return True
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    for attempt in range(1, 4):
        try:
            r = requests.post(url, data=payload, timeout=(10, 60))
        except requests.RequestException as e:
            log(f"  ⚠️  Telegram ne répond pas (essai {attempt}/3) : {type(e).__name__}")
            time.sleep(10 * attempt)
            continue
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.ok and data.get("ok"):
            return True
        retry = data.get("parameters", {}).get("retry_after")
        if retry:
            log(f"  ⏳ Telegram demande d'attendre {retry}s")
            time.sleep(int(retry) + 1)
            continue
        log(f"  ❌ Échec Telegram : {data.get('description') or r.text[:200]}")
        return False
    log("  ❌ Telegram injoignable pour cette offre, nouvel essai au prochain passage")
    return False


def main():
    if not TOKEN and not DRY_RUN:
        log("❌ Le secret TELEGRAM_BOT_TOKEN est manquant. Voir le guide, étape 3.")
        sys.exit(1)

    seen = load_seen()
    seen_set = set(seen)
    first_run = len(seen) == 0

    log("📡 Lecture des sources…")
    entries = []
    for url in load_sources():
        entries.extend(fetch_feed(url))

    new = []
    batch_ids = set()
    for e in entries:
        oid = offer_id(e)
        if oid in seen_set or oid in batch_ids:
            continue
        batch_ids.add(oid)
        e["_id"] = oid
        new.append(e)
    new.sort(key=entry_time)  # les plus anciennes d'abord, pour garder l'ordre chronologique
    log(f"🆕 {len(new)} nouvelles offres trouvées")

    if first_run:
        # Au tout premier passage on ne publie que les plus récentes,
        # les autres sont marquées comme déjà vues (évite d'inonder le canal).
        to_post = new[-FIRST_RUN_POSTS:] if FIRST_RUN_POSTS else []
        for e in new[: len(new) - len(to_post)]:
            seen.append(e["_id"])
    else:
        to_post = new[-MAX_POSTS:]
        for e in new[: len(new) - len(to_post)]:
            seen.append(e["_id"])  # trop d'offres d'un coup : on garde les plus récentes

    posted = 0
    for e in to_post:
        if send(format_message(e)):
            seen.append(e["_id"])
            posted += 1
            if not DRY_RUN:
                save_seen(seen)  # on mémorise au fur et à mesure
            time.sleep(3)  # rythme doux pour Telegram

    if not DRY_RUN:
        save_seen(seen)
    log(f"✅ Terminé : {posted} offre(s) publiée(s) sur {CHANNEL}")


if __name__ == "__main__":
    main()
