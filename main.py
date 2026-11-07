# -*- coding: utf-8 -*-
import os
import re
import io
import time
import asyncio
import logging
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from PIL import Image
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= Configuration =================
BASE = "https://dootsport.co/"
TOKEN = os.getenv("BOT_TOKEN", "8756140722:AAFIr-OXKcdGQQb7nL0CD14fdgjLvlwJdAQ").strip()
PORT = int(os.getenv("PORT", 8080))
HOST = "0.0.0.0"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Referer": BASE,
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b", re.I)
SCORE_RE = re.compile(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TARGET_KEYWORDS = [
    "إنجليزي", "انجليزي", "premier league", "بريميرليج", "انجلترا", "إنجلترا",
    "إسباني", "اسباني", "la liga", "لاليغا", "إسبانيا", "اسبانيا",
    "إيطالي", "ايطالي", "serie a", "سيريا", "إيطاليا", "ايطاليا",
    "ألماني", "الماني", "bundesliga", "بوندسليغا", "ألمانيا", "المانيا",
    "فرنسي", "ligue 1", "ليغ 1", "فرنسا",
    "أبطال أوروبا", "ابطال اوروبا", "champions league", "تشامبيونز",
    "سعودي", "روشن", "saudi", "المملكة"
]

TOP_TEAMS = [
    "ريال مدريد", "برشلونة", "برشلونه", "أتلتيكو مدريد", "اتلتيكو مدريد", "إشبيلية", "اشبيلية", "فياريال", "ريال سوسيداد", "أتلتيك بيلباو", "فالنسيا", "جيرونا",
    "مانشستر سيتي", "ليفربول", "آرسنال", "ارسنال", "تشيلسي", "مانشستر يونايتد", "توتنهام", "نيوكاسل", "أستون فيلا", "استون فيلا", "إيفرتون",
    "إنتر ميلان", "انتر ميلان", "إنتر", "ميلان", "إيه سي ميلان", "يوفنتوس", "نابولي", "روما", "لاتسيو", "أتالانتا", "فيورنتينا",
    "بايرن ميونخ", "بايرن ميونيخ", "بوروسيا دورتموند", "دورتموند", "باير ليفركوزن", "ليفركوزن", "لايبزيغ", "فرانكفورت",
    "باريس سان جيرمان", "باريس", "مارسيليا", "موناكو", "ليون", "ليل",
    "الهلال", "النصر", "الاتحاد", "الأهلي السعودي", "الاهلي السعودي", "الشباب", "الاتفاق", "القادسية", "التعاون"
]

MATCHES_CACHE = {
    "yesterday": [],
    "today": [],
    "tomorrow": [],
    "last_updated": 0
}

# ================= Scraper Engine =================
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def txt(node):
    return clean(node.get_text(" ", strip=True)) if node else ""

def day_links(soup):
    result = {"yesterday": [], "today": [], "tomorrow": []}
    words = {
        "yesterday": ["الأمس", "امس", "yesterday"],
        "today": ["اليوم", "today"],
        "tomorrow": ["الغد", "غدا", "غداً", "tomorrow"],
    }
    for a in soup.find_all("a", href=True):
        label = txt(a).lower()
        url = urljoin(BASE, a["href"])
        if not url.startswith(BASE):
            continue
        for day, aliases in words.items():
            if any(x.lower() in label for x in aliases) and url not in result[day]:
                result[day].append(url)
    if not result["today"]:
        result["today"] = [BASE]
    return result

def is_card(node):
    s = txt(node)
    if not s or len(s) > 1800:
        return False
    signal = bool(
        TIME_RE.search(s)
        or SCORE_RE.search(s)
        or any(x in s.lower() for x in ["مباشر", "لم تبدأ", "انتهت", "live", "finished", "تأجيل"])
    )
    return signal and len(node.find_all("img")) >= 1

def cards(soup):
    tags = ["article", "li", "div", "a"]
    out = []
    seen = set()
    for node in soup.find_all(tags):
        if not is_card(node):
            continue
        if any(is_card(c) for c in node.find_all(tags, recursive=False)):
            continue
        s = txt(node)
        if s and s not in seen:
            seen.add(s)
            out.append(node)
    return out

def is_target_match(m):
    full_text = f"{m.get('league', '')} {' '.join(m.get('teams', []))}".lower()
    for kw in TARGET_KEYWORDS:
        if kw in full_text:
            return True
    for team in m.get("teams", []):
        for top in TOP_TEAMS:
            if top.lower() in team.lower():
                return True
    return False

def parse_card(card):
    s = txt(card)
    teams = []
    logos = []
    for im in card.find_all("img"):
        n = clean(im.get("alt") or im.get("title") or "")
        n = re.sub(r"\.(png|jpg|jpeg|webp|svg)(\?.*)?$", "", n, flags=re.I)
        if n and n not in teams:
            teams.append(n)
        logo = im.get("data-src") or im.get("src") or ""
        if logo and not logo.startswith("data:image"):
            logo = urljoin(BASE, logo)
            if logo not in logos:
                logos.append(logo)

    links = [urljoin(BASE, a["href"]) for a in card.find_all("a", href=True)]
    status = next(
        (x for x in ["لم تبدأ بعد", "لم تبدأ", "مباشر", "انتهت", "تأجيل", "تأجلت", "Live", "Finished"]
         if x.lower() in s.lower()),
        "لم تبدأ بعد",
    )
    channel = next(
        (clean(line) for line in card.stripped_strings
         if re.search(r"beIN|SSC|أبو ظبي|الكأس|القناة|Shahid|ON Sport", line, re.I)),
        "قناة غير محددة",
    )
    league = next(
        (clean(line) for line in card.stripped_strings
         if re.search(r"دوري|كأس|بطولة|league|cup|champions", line, re.I)),
        "مباراة ودية / دوري ممتاز",
    )
    tm = TIME_RE.search(s)
    score = SCORE_RE.search(s)

    team1 = teams[0] if len(teams) > 0 else "الفريق الأول"
    team2 = teams[1] if len(teams) > 1 else "الفريق الثاني"
    logo1 = logos[0] if len(logos) > 0 else ""
    logo2 = logos[1] if len(logos) > 1 else ""

    return {
        "teams": [team1, team2],
        "team1": team1,
        "team2": team2,
        "logos": [logo1, logo2],
        "logo1": logo1,
        "logo2": logo2,
        "time": tm.group(0) if tm else "00:00",
        "score": score.group(0) if score else "0 - 0",
        "status": status,
        "channel": channel,
        "league": league,
        "url": links[0] if links else BASE,
    }

def scrape_day(day, urls):
    out = []
    seen = set()
    for url in urls.get(day, []):
        try:
            soup = BeautifulSoup(fetch(url), "html.parser")
            for card in cards(soup):
                m = parse_card(card)
                if not is_target_match(m):
                    continue
                key = (tuple(m["teams"]), m["time"])
                if key not in seen:
                    seen.add(key)
                    out.append(m)
        except Exception as e:
            logging.warning("Scrape error on %s: %s", url, e)
    return out

def update_cache():
    try:
        logging.info("Updating matches cache from DotSport...")
        soup = BeautifulSoup(fetch(BASE), "html.parser")
        links = day_links(soup)
        yesterday_data = scrape_day("yesterday", links)
        today_data = scrape_day("today", links)
        tomorrow_data = scrape_day("tomorrow", links)

        MATCHES_CACHE["yesterday"] = yesterday_data
        MATCHES_CACHE["today"] = today_data
        MATCHES_CACHE["tomorrow"] = tomorrow_data
        MATCHES_CACHE["last_updated"] = time.time()
        logging.info("Cache successfully updated! (Yesterday: %d, Today: %d, Tomorrow: %d)",
                     len(yesterday_data), len(today_data), len(tomorrow_data))
    except Exception as e:
        logging.error("Failed to update cache: %s", e)

# ============ Image Combine ============
def download_image(url, timeout=10):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None

def combine_logos(logo1_url, logo2_url, size=180):
    img1 = download_image(logo1_url) if logo1_url else None
    img2 = download_image(logo2_url) if logo2_url else None
    if not img1 and not img2:
        return None
    canvas = Image.new("RGBA", (size * 2 + 40, size + 20), (255, 255, 255, 255))
    def prep(im):
        if im is None:
            return None
        im.thumbnail((size, size))
        bg = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        offset = ((size - im.width) // 2, (size - im.height) // 2)
        bg.paste(im, offset, im)
        return bg
    p1 = prep(img1)
    p2 = prep(img2)
    if p1: canvas.paste(p1, (10, 10), p1)
    if p2: canvas.paste(p2, (size + 30, 10), p2)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf

# ============ HTTP REST API Server ============
async def api_get_matches(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "public, max-age=60",
    }
    if time.time() - MATCHES_CACHE["last_updated"] > 300:
        asyncio.get_event_loop().run_in_executor(None, update_cache)

    payload = {
        "status": "success",
        "last_updated": MATCHES_CACHE["last_updated"],
        "counts": {
            "yesterday": len(MATCHES_CACHE["yesterday"]),
            "today": len(MATCHES_CACHE["today"]),
            "tomorrow": len(MATCHES_CACHE["tomorrow"])
        },
        "matches": {
            "yesterday": MATCHES_CACHE["yesterday"],
            "today": MATCHES_CACHE["today"],
            "tomorrow": MATCHES_CACHE["tomorrow"]
        }
    }
    return web.json_response(payload, headers=headers)

async def api_options(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    return web.Response(status=200, headers=headers)

async def api_health(request):
    return web.Response(text="Bot & API Server is Healthy and Running ⚽", status=200)

# ============ Telegram Bot Handlers ============
def match_caption(i, m):
    title = f"{m['team1']} VS {m['team2']}"
    lines = [
        f"<b>{i}. {title}</b>",
        f"🏆 {m['league']}",
        f"🕒 التوقيت: {m['time']}",
        f"📺 القناة: {m['channel']}",
        f"📌 الحالة: {m['status']}"
    ]
    if m.get("score") and m["score"] != "0 - 0":
        lines.append(f"🔢 النتيجة: {m['score']}")
    lines.append(f'🔗 <a href="{m[\'url\']}">رابط البث / المباراة</a>')
    return "\n".join(lines)

async def send_matches_to_tg(update: Update, label, matches):
    chat = update.effective_chat
    if not chat:
        return
    if not matches:
        await chat.send_message(f"⚽ <b>مباريات {label}</b>\n\nلا توجد مباريات كبرى مسجلة لهذا اليوم.", parse_mode="HTML")
        return

    await chat.send_message(f"⚽ <b>أهم مباريات {label}</b> — عدد: {len(matches)}", parse_mode="HTML")
    for i, m in enumerate(matches, 1):
        caption = match_caption(i, m)
        combined = None
        if m.get("logo1") and m.get("logo2"):
            combined = await asyncio.get_event_loop().run_in_executor(None, combine_logos, m["logo1"], m["logo2"])
        try:
            if combined:
                await chat.send_photo(photo=combined, caption=caption, parse_mode="HTML")
            else:
                await chat.send_message(caption, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await chat.send_message(caption, parse_mode="HTML", disable_web_page_preview=True)

async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("⏳ مرحباً بك! جارٍ جلب أهم مباريات الدوريات الكبرى والدوري السعودي...")
    if time.time() - MATCHES_CACHE["last_updated"] > 300 or not MATCHES_CACHE["today"]:
        await asyncio.get_event_loop().run_in_executor(None, update_cache)
    await send_matches_to_tg(update, "اليوم", MATCHES_CACHE["today"])
    await send_matches_to_tg(update, "الغد", MATCHES_CACHE["tomorrow"])
    await send_matches_to_tg(update, "الأمس", MATCHES_CACHE["yesterday"])

async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "⚽ <b>أوامر البوت:</b>\n\n"
            "/start أو /matches — عرض مباريات اليوم والغد والأمس لأهم الدوريات\n"
            "/api — رابط API المخصص للمدونة",
            parse_mode="HTML"
        )

# ============ Background Scheduler ============
async def background_cache_worker():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, update_cache)
        except Exception as e:
            logging.error("Worker error: %s", e)
        await asyncio.sleep(300)

# ============ Main Entry ============
async def main_async():
    app_web = web.Application()
    app_web.router.add_get("/", api_health)
    app_web.router.add_get("/api/matches", api_get_matches)
    app_web.router.add_options("/api/matches", api_options)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    logging.info("🚀 Web API Server is running on http://%s:%d/api/matches", HOST, PORT)

    asyncio.create_task(background_cache_worker())

    if TOKEN:
        tg_app = Application.builder().token(TOKEN).build()
        tg_app.add_handler(CommandHandler("start", tg_start))
        tg_app.add_handler(CommandHandler("matches", tg_start))
        tg_app.add_handler(CommandHandler("help", tg_help))

        logging.info("🤖 Starting Telegram Bot polling...")
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)

        while True:
            await asyncio.sleep(3600)
    else:
        logging.warning("⚠️ BOT_TOKEN not set. Running in Web API Mode only.")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Server stopped.")
