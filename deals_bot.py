# -*- coding: utf-8 -*-
"""بوت صيد العروض الحقيقية — متاجر مصر -> تيليجرام
التشغيل: python deals_bot.py          (دورة واحدة)
         python deals_bot.py --loop   (كل N دقيقة حسب config.yaml)
"""
import json, re, sqlite3, statistics, sys, time, random, logging
from datetime import datetime, timedelta
from urllib.parse import urljoin
import requests, yaml
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("deals")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def num(s):
    if s is None: return None
    s = str(s).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    m = re.search(r"\d[\d,]*\.?\d*", s.replace("٬", ","))
    return float(m.group().replace(",", "")) if m else None

# ---------- جلب الصفحات ----------
_pw = None; _browser = None
def browser():
    """يفتح المتصفح مرة واحدة: كروم المثبت على الجهاز أولًا، وإلا Chromium الخاص بـ Playwright."""
    global _pw, _browser
    if _browser: return _browser
    from playwright.sync_api import sync_playwright
    if _pw is None: _pw = sync_playwright().start()
    for opts in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            _browser = _pw.chromium.launch(headless=True, **opts); return _browser
        except Exception as e:
            log.info("تعذر فتح المتصفح %s: %s", opts or "chromium", str(e).splitlines()[0])
    import subprocess
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    _browser = _pw.chromium.launch(headless=True); return _browser

def fetch(url, render=False):
    if render:
        ctx = browser().new_context(user_agent=UA, locale="ar-EG")
        pg = ctx.new_page()
        try:
            pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
            for _ in range(3): pg.mouse.wheel(0, 3000); pg.wait_for_timeout(1000)
            return pg.content()
        finally:
            ctx.close()
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ar-EG,en;q=0.8"}, timeout=30)
    r.raise_for_status(); return r.text

# ---------- استخراج المنتجات ----------
def from_jsonld(soup, base):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try: data = json.loads(tag.string or "")
        except Exception: continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list): stack += d; continue
            if not isinstance(d, dict): continue
            for k in ("@graph", "itemListElement"):
                if k in d: stack += d[k] if isinstance(d[k], list) else [d[k]]
            if "item" in d and isinstance(d["item"], dict): stack.append(d["item"])
            if d.get("@type") == "Product":
                off = d.get("offers") or {}
                if isinstance(off, list): off = off[0] if off else {}
                price = num(off.get("price") or off.get("lowPrice"))
                if price and d.get("name") and (d.get("url") or off.get("url")):
                    out.append({"title": d["name"], "price": price, "old": None,
                                "url": urljoin(base, d.get("url") or off.get("url"))})
    return out

def from_selectors(soup, base, sel):
    out = []
    for card in soup.select(sel["card"]):
        t = card.select_one(sel["title"]); p = card.select_one(sel["price"])
        a = card if card.name == "a" else card.select_one(sel.get("link", "a"))
        o = card.select_one(sel["old"]) if sel.get("old") else None
        if not (t and p and a and a.get("href")): continue
        price = num(p.get_text())
        if not price: continue
        out.append({"title": t.get_text(" ", strip=True), "price": price,
                    "old": num(o.get_text()) if o else None,
                    "url": urljoin(base, a["href"].split("?")[0])})
    return out

def scrape(store):
    items = []
    for url in store["urls"]:
        try:
            html = fetch(url, store.get("render", False))
            soup = BeautifulSoup(html, "html.parser")
            got = from_selectors(soup, url, store["selectors"]) if store.get("selectors") else []
            if not got: got = from_jsonld(soup, url)
            log.info("%s | %s -> %d منتج", store["name"], url, len(got))
            items += got
        except Exception as e:
            log.warning("%s | %s فشل: %s", store["name"], url, e)
        time.sleep(random.uniform(2, 5))
    return items

# ---------- قاعدة البيانات وتاريخ الأسعار ----------
def db_open(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS prices(url TEXT, store TEXT, title TEXT, price REAL, old REAL, ts TEXT);
    CREATE INDEX IF NOT EXISTS ix ON prices(url, ts);
    CREATE TABLE IF NOT EXISTS alerts(url TEXT, price REAL, ts TEXT);""")
    return db

def history(db, url, days):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    return [r[0] for r in db.execute("SELECT price FROM prices WHERE url=? AND ts>=?", (url, since))]

def judge(item, hist, rules):
    """يرجع (هل هو عرض حقيقي؟, السبب). لا يثق بالسعر 'قبل الخصم' المعروض في الموقع."""
    p = item["price"]
    if p < rules["min_price"]: return False, ""
    if len(hist) >= rules["min_history"]:
        med = statistics.median(hist); low = min(hist)
        drop = (med - p) / med * 100
        if drop >= rules["real_drop_pct"] and p <= low:
            return True, f"✅ عرض حقيقي: أقل من متوسط سعره ({med:,.0f}) بـ {drop:.0f}% وأقل سعر رصدناه"
        return False, ""
    # منتج جديد بلا تاريخ: لا ننبّه إلا لخصم ضخم، ونوسمه بـ "غير مؤكد"
    if item.get("old") and item["old"] > p:
        claimed = (item["old"] - p) / item["old"] * 100
        if claimed >= rules["unverified_min_claimed_pct"]:
            return True, f"⚠️ خصم معلن {claimed:.0f}% (غير مؤكد — لا يوجد تاريخ سعر بعد)"
    return False, ""

def already_alerted(db, url, price, cooldown_h):
    since = (datetime.now() - timedelta(hours=cooldown_h)).isoformat()
    r = db.execute("SELECT MIN(price) FROM alerts WHERE url=? AND ts>=?", (url, since)).fetchone()[0]
    return r is not None and price >= r

# ---------- تيليجرام ----------
def tg_send(cfg, text):
    tok, chat = cfg["telegram"]["token"], cfg["telegram"]["chat_id"]
    if not tok or "PUT" in tok:
        print("----\n" + text); return
    if not chat or "PUT" in str(chat):
        # يلتقط رقم المحادثة تلقائيًا من أول رسالة/Start أُرسلت للبوت ويحفظه
        upd = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20).json().get("result", [])
        ids = [u.get("message", {}).get("chat", {}).get("id") for u in upd if u.get("message")]
        if not ids:
            print("⚠️ افتح البوت في تيليجرام واضغط Start\n----\n" + text); return
        chat = cfg["telegram"]["chat_id"] = str(ids[-1])
        c = open("config.yaml", encoding="utf-8").read().replace("PUT_CHAT_ID_HERE", chat)
        open("config.yaml", "w", encoding="utf-8").write(c)
        log.info("تم حفظ chat_id = %s", chat)
    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                  data={"chat_id": chat, "text": text, "disable_web_page_preview": False}, timeout=20)

def esc(s): return s.replace("<", "").replace(">", "")

# ---------- الدورة ----------
def run_once(cfg):
    db = db_open(cfg["db"]); rules = cfg["rules"]; now = datetime.now().isoformat()
    sent = 0
    for store in cfg["stores"]:
        if not store.get("enabled", True): continue
        seen = set()
        for it in scrape(store):
            if it["url"] in seen: continue
            seen.add(it["url"])
            hist = history(db, it["url"], rules["history_days"])
            ok, why = judge(it, hist, rules)
            db.execute("INSERT INTO prices VALUES(?,?,?,?,?,?)",
                       (it["url"], store["name"], it["title"], it["price"], it["old"], now))
            if ok and not already_alerted(db, it["url"], it["price"], rules["alert_cooldown_hours"]):
                tg_send(cfg, f"🛒 {store['name']}\n{esc(it['title'])[:200]}\n💰 {it['price']:,.0f} ج.م\n{why}\n{it['url']}")
                db.execute("INSERT INTO alerts VALUES(?,?,?)", (it["url"], it["price"], now)); sent += 1
        db.commit()
    log.info("انتهت الدورة — %d تنبيه", sent)
    return sent

if __name__ == "__main__":
    import os
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    # على GitHub يُقرأ التوكن ورقم المحادثة من الأسرار (Secrets) لا من الملف
    if os.environ.get("TELEGRAM_TOKEN"): cfg["telegram"]["token"] = os.environ["TELEGRAM_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"): cfg["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if "--test-telegram" in sys.argv:
        tg_send(cfg, "✅ بوت العروض متصل بنجاح"); sys.exit()
    while True:
        run_once(cfg)
        if "--loop" not in sys.argv: break
        time.sleep(cfg["interval_minutes"] * 60)
