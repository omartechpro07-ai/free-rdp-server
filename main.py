# -*- coding: utf-8 -*-
import os
import re
import io
import time
import json
import asyncio
import logging
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from PIL import Image
from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= Configuration =================
BASE = "https://kooralive.la/"
MATCHES_TODAY_URL = "https://kooralive.la/matches-today/"
MATCHES_YESTERDAY_URL = "https://kooralive.la/matches-yesterday/"
MATCHES_TOMORROW_URL = "https://kooralive.la/matches-tomorrow/"

TOKEN = os.getenv("BOT_TOKEN", "8756140722:AAFIr-OXKcdGQQb7nL0CD14fdgjLvlwJdAQ").strip()
PORT = int(os.getenv("PORT", 8080))
HOST = "0.0.0.0"
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "https://kora-live-api.onrender.com").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Referer": BASE,
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}

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
CACHED_JSON_BYTES = b'{"status":"init","matches":{"yesterday":[],"today":[],"tomorrow":[]}}'

# ================= Scraper Engine =================
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

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

def parse_aya_match(card):
    # الفريق الأول
    tm1_node = card.select_one(".MT_Team.TM1")
    t1_name = clean(tm1_node.select_one(".TM_Name").get_text()) if tm1_node and tm1_node.select_one(".TM_Name") else "الفريق الأول"
    t1_img = tm1_node.select_one(".TM_Logo img") if tm1_node else None
    t1_logo = (t1_img.get("data-src") or t1_img.get("src") or "") if t1_img else ""
    if t1_logo.startswith("data:image"):
        t1_logo = t1_img.get("data-src") or ""

    # الفريق الثاني
    tm2_node = card.select_one(".MT_Team.TM2")
    t2_name = clean(tm2_node.select_one(".TM_Name").get_text()) if tm2_node and tm2_node.select_one(".TM_Name") else "الفريق الثاني"
    t2_img = tm2_node.select_one(".TM_Logo img") if tm2_node else None
    t2_logo = (t2_img.get("data-src") or t2_img.get("src") or "") if t2_img else ""
    if t2_logo.startswith("data:image"):
        t2_logo = t2_img.get("data-src") or ""

    # التوقيت
    time_node = card.select_one(".MT_Time")
    match_time = clean(time_node.get_text()) if time_node else "00:00"

    # النتيجة
    goals = card.select(".RS-goals")
    if len(goals) >= 2:
        score = f"{clean(goals[0].get_text())}-{clean(goals[1].get_text())}"
    else:
        score_node = card.select_one(".MT_Result")
        score = clean(score_node.get_text()) if score_node else "0-0"

    # الحالة
    stat_node = card.select_one(".MT_Stat")
    raw_stat = clean(stat_node.get_text()) if stat_node else "لم تبدأ بعد"
    if "جارية" in raw_stat or "live" in raw_stat.lower() or "مباشر" in raw_stat:
        status = "مباشر"
    elif "انتهت" in raw_stat or "finished" in raw_stat.lower():
        status = "انتهت"
    else:
        status = "لم تبدأ بعد"

    # المعلومات الإضافية (القناة، المعلق، البطولة)
    info_items = [clean(s.get_text()) for s in card.select(".MT_Info ul li span")]
    channel = info_items[0] if len(info_items) > 0 else "beIN SPORTS"
    league = info_items[2] if len(info_items) > 2 else (info_items[1] if len(info_items) > 1 else "الدوري الممتاز")

    # رابط البث المباشر
    link_node = card.find("a", href=True)
    stream_url = link_node["href"] if link_node else BASE

    return {
        "teams": [t1_name, t2_name],
        "team1": t1_name,
        "team2": t2_name,
        "logos": [t1_logo, t2_logo],
        "logo1": t1_logo,
        "logo2": t2_logo,
        "time": match_time,
        "score": score,
        "status": status,
        "channel": channel,
        "league": league,
        "url": stream_url,
    }

def scrape_kooralive_page(url):
    out = []
    seen = set()
    try:
        html = fetch(url)
        soup = BeautifulSoup(html, "html.parser")
        match_cards = soup.select(".AY_Match")
        for card in match_cards:
            m = parse_aya_match(card)
            if not is_target_match(m):
                continue
            key = (tuple(m["teams"]), m["time"])
            if key not in seen:
                seen.add(key)
                out.append(m)
    except Exception as e:
        logging.warning("Error scraping %s: %s", url, e)
    return out

def update_cache():
    global CACHED_JSON_BYTES
    try:
        logging.info("Updating matches cache from KooraLive.la...")
        today_data = scrape_kooralive_page(MATCHES_TODAY_URL)
        yesterday_data = scrape_kooralive_page(MATCHES_YESTERDAY_URL)
        tomorrow_data = scrape_kooralive_page(MATCHES_TOMORROW_URL)

        MATCHES_CACHE["yesterday"] = yesterday_data
        MATCHES_CACHE["today"] = today_data
        MATCHES_CACHE["tomorrow"] = tomorrow_data
        MATCHES_CACHE["last_updated"] = time.time()

        payload = {
            "status": "success",
            "last_updated": MATCHES_CACHE["last_updated"],
            "counts": {
                "yesterday": len(yesterday_data),
                "today": len(today_data),
                "tomorrow": len(tomorrow_data)
            },
            "matches": {
                "yesterday": yesterday_data,
                "today": today_data,
                "tomorrow": tomorrow_data
            }
        }
        CACHED_JSON_BYTES = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logging.info("KooraLive Cache updated! (Yesterday: %d, Today: %d, Tomorrow: %d)",
                     len(yesterday_data), len(today_data), len(tomorrow_data))
    except Exception as e:
        logging.error("Failed to update KooraLive cache: %s", e)

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
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, max-age=180",
    }
    return web.Response(body=CACHED_JSON_BYTES, status=200, headers=headers)

async def api_options(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    return web.Response(status=200, headers=headers)

async def api_health(request):
    return web.Response(text="KooraLive API Server is Healthy and Running ⚽", status=200)

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
    if m.get("score") and m["score"] != "0-0":
        lines.append(f"🔢 النتيجة: {m['score']}")
    match_link = m.get("url", BASE)
    lines.append(f'🔗 <a href="{match_link}">رابط البث / المباراة</a>')
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
    if not MATCHES_CACHE["today"]:
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

# ============ Background Schedulers ============
async def background_cache_worker():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, update_cache)
        except Exception as e:
            logging.error("Worker error: %s", e)
        await asyncio.sleep(300)

async def keep_awake_worker():
    await asyncio.sleep(60)
    while True:
        try:
            ping_url = f"{SELF_URL}/"
            async with ClientSession() as session:
                async with session.get(ping_url, timeout=15) as resp:
                    logging.info("Keep-Awake Ping sent to %s, status: %d", ping_url, resp.status)
        except Exception as e:
            logging.warning("Keep-Awake Ping error: %s", e)
        await asyncio.sleep(540)

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
    asyncio.create_task(keep_awake_worker())

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
