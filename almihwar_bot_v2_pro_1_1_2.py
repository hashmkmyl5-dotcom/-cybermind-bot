import logging
import logging.handlers
import os
import asyncio
import aiosqlite
import aiohttp
import re
import json
import time
import google.generativeai as genai
from openai import OpenAI
client = OpenAI()
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from telegram import Bot, InputMediaPhoto, InputMediaVideo, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter, TelegramError
from dotenv import load_dotenv
from urllib.parse import urlparse

# تحميل الإعدادات
load_dotenv("/home/ubuntu/bot_project/bot_config_2.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL")
MY_CHANNEL_LINK = os.getenv("MY_CHANNEL_LINK", "https://t.me/almihwar_news")
DB_FILE = os.getenv("DB_FILE", "almihwar.db")
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", 0.85))
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# إعداد Gemini
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash')
    except Exception as e:
        print(f"Gemini Config Error: {e}")
        model = None
else:
    model = None

DEFAULT_CHANNELS = [
    "almasirah", "mmy_news", "AnsarAllahMC", "ansarallah_news", 
    "Yemen_News_Agency", "muqawam313", "axisofresistance313",
    "Azaha_Setar", "sasat_almaserah", "alalam_arabia", "almanarnews",
    "C_Military1", "Palinfo", "Hezbollah", "sarayaps", "qassam_brigades",
    "UunionNews", "yemennow_news", "AlmayadeenLive", "a7l733i", "ALYEMENNET"
]

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

IS_RUNNING = True
TOTAL_POSTED_TODAY = 0

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً بك في بوت شبكة المحور الإخبارية. البوت يعمل الآن في الخلفية.")

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS posts (post_id TEXT PRIMARY KEY, clean_text TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS content_history (id INTEGER PRIMARY KEY AUTOINCREMENT, normalized_text TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS channels (username TEXT PRIMARY KEY)''')
        async with db.execute("SELECT COUNT(*) FROM channels") as cursor:
            if (await cursor.fetchone())[0] == 0:
                for ch in DEFAULT_CHANNELS:
                    await db.execute("INSERT INTO channels (username) VALUES (?)", (ch,))
        await db.commit()

async def get_all_channels():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT username FROM channels") as cursor:
            return [row[0] async for row in cursor]

async def is_content_duplicate(new_text):
    if not new_text or len(new_text) < 20: return False
    norm_new = re.sub(r'[^\w\u0600-\u06FF]', '', new_text)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT normalized_text FROM content_history ORDER BY timestamp DESC LIMIT 1000") as cursor:
            recent_contents = [row[0] async for row in cursor]
    for old_norm in recent_contents:
        if old_norm and SequenceMatcher(None, norm_new, old_norm).ratio() > SIMILARITY_THRESHOLD:
            return True
    return False

async def save_content_history(text):
    norm_text = re.sub(r'[^\w\u0600-\u06FF]', '', text)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO content_history (normalized_text) VALUES (?)", (norm_text,))
        await db.commit()

async def ai_process_news(text):
    if not text: return text
    
    prompt = f"""أنت الآن المحرر الإخباري الرئيسي لـ "شبكة المحور الإخبارية". مهمتك هي إعادة صياغة الخبر التالي بأسلوب صحفي احترافي، قوي، وجذاب.

القواعد الصارمة:
1. **فلترة المحتوى**: إذا كان النص ليس خبراً (مثلاً: ترحيب، تحية، إعلان عن قناة احتياطية)، فرد بكلمة واحدة فقط: "IGNORE".
2. **الحذف الجذري للمصادر**: احذف نهائياً وبشكل قطعي أي إشارة للمصادر الأصلية أو القنوات الأخرى.
3. **التنسيق**: 
   - ابدأ الخبر بعنوان عريض (Bold) ومثير للاهتمام.
   - استخدم نقاط واضحة إذا كان الخبر يحتوي على تفاصيل متعددة.
   - لا تستخدم إيموجيات عشوائية، استخدم فقط (🔹، 📍، 🛡، 🔴) بشكل منسق.
4. **الأسلوب**: صياغة رصينة، لغة عربية فصيحة، وتجنب التكرار الممل.
5. **النهاية**: تأكد من أن الخبر ينتهي بجملة تامة.

الخبر المراد صياغته:
{text}

أخرج الخبر النهائي المنسق فقط، أو كلمة IGNORE إذا كان المحتوى غير إخباري."""
    
    # محاولة استخدام Gemini أولاً
    if model:
        try:
            response = await asyncio.to_thread(model.generate_content, prompt)
            if response and response.text:
                result = response.text.strip()
                if "IGNORE" in result: return "IGNORE"
                return re.sub(r'#[\w_\u0600-\u06FF]+', '', result)
        except Exception as e:
            logger.warning(f"Gemini Error, switching to OpenAI: {e}")

    # استخدام OpenAI كبديل (Backup)
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.choices[0].message.content.strip()
        if "IGNORE" in result: return "IGNORE"
        return re.sub(r'#[\w_\u0600-\u06FF]+', '', result)
    except Exception as e:
        logger.error(f"OpenAI Error: {e}")
        return text

def super_clean(text):
    if not text: return ""
    # حذف الروابط واليوزرات والهاشتاقات
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r't\.me/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'#[\w_\u0600-\u06FF]+', '', text)
    
    bad_phrases = ["حياكم الله", "السلام عليكم", "تحية طيبة", "قناة احتياطية", "انضم إلينا"]
    for phrase in bad_phrases:
        if phrase in text: return "IGNORE"
        
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    return '\n'.join(lines).strip()

def generate_smart_hashtags(text):
    hashtags = ["#شبكة_المحور_الإخبارية"]
    keywords = {"فلسطين": "#فلسطين", "غزة": "#غزة", "اليمن": "#اليمن", "لبنان": "#لبنان", "المقاومة": "#محور_المقاومة", "عاجل": "#عاجل"}
    for key, tag in keywords.items():
        if key in text: hashtags.append(tag)
    return " ".join(list(dict.fromkeys(hashtags)))

async def format_rich_content(cleaned_text):
    ai_text = await ai_process_news(cleaned_text)
    if ai_text == "IGNORE": return "IGNORE"
    
    hashtags = generate_smart_hashtags(ai_text)
    footer = (
        "\n\nـــــــــــــــــــــــــــــــــــــــــــــــــ\n"
        "🚩 <b>شبكة المحور الإخبارية</b>\n"
        f"🔗 {MY_CHANNEL_LINK}\n"
        f"{hashtags}"
    )
    header = "🔹 <b>متابعـات إخباريـة |</b>\n\n"
    return f"{header}{ai_text}{footer}"

async def fetch_channel_posts(session, channel):
    url = f"https://t.me/s/{channel}"
    try:
        async with session.get(url, timeout=20) as res:
            if res.status != 200: return []
            soup = BeautifulSoup(await res.text(), 'html.parser')
            msgs = soup.find_all('div', class_='tgme_widget_message_wrap')
            results = []
            for m in msgs[-10:]: # فحص آخر 10 رسائل لضمان عدم فوات شيء
                msg_div = m.find('div', class_='tgme_widget_message')
                if not msg_div: continue
                p_id = f"{channel}_{msg_div.get('data-post')}"
                txt_div = m.find('div', class_='tgme_widget_message_text')
                txt = txt_div.get_text(separator='\n') if txt_div else ""
                
                photos = []
                photo_elements = m.find_all('a', class_='tgme_widget_message_photo_wrap')
                for p in photo_elements:
                    style = p.get('style')
                    if style:
                        match = re.search(r"url\('([^']+)'\)", style)
                        if match:
                            img_url = match.group(1)
                            if img_url and "telegram.org" not in img_url:
                                if not img_url.startswith('http'):
                                    img_url = 'https:' + img_url if img_url.startswith('//') else 'https://t.me' + img_url
                                photos.append(img_url)
                
                video = None
                video_div = m.find('a', class_='tgme_widget_message_video_player')
                if video_div:
                    video_url = video_div.get('href')
                    if video_url: video = video_url
                
                results.append({"id": p_id, "text": txt, "photos": photos, "video": video, "channel": channel})
            return results
    except Exception as e:
        logger.error(f"Fetch Error for {channel}: {e}")
        return []

async def scraping_job(context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_POSTED_TODAY
    if not IS_RUNNING: return
    
    async with aiohttp.ClientSession(headers={'User-Agent': 'Mozilla/5.0'}) as session:
        channels = await get_all_channels()
        for channel in channels:
            posts = await fetch_channel_posts(session, channel)
            for post in posts:
                async with aiosqlite.connect(DB_FILE) as db:
                    async with db.execute("SELECT 1 FROM posts WHERE post_id=?", (post['id'],)) as cursor:
                        if await cursor.fetchone(): continue
                
                cleaned = super_clean(post["text"])
                if cleaned == "IGNORE" or not cleaned:
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute("INSERT INTO posts (post_id, clean_text) VALUES (?, ?)", (post['id'], "SKIP"))
                        await db.commit()
                    continue

                if await is_content_duplicate(cleaned):
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute("INSERT INTO posts (post_id, clean_text) VALUES (?, ?)", (post['id'], "SKIP"))
                        await db.commit()
                    continue
                
                final_msg = await format_rich_content(cleaned)
                if final_msg == "IGNORE":
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute("INSERT INTO posts (post_id, clean_text) VALUES (?, ?)", (post['id'], "SKIP"))
                        await db.commit()
                    continue

                try:
                    target = TARGET_CHANNEL
                    try:
                        if post["photos"]:
                            if len(post["photos"]) == 1:
                                await context.bot.send_photo(target, photo=post["photos"][0], caption=final_msg, parse_mode=ParseMode.HTML)
                            else:
                                media_group = [InputMediaPhoto(post["photos"][0], caption=final_msg, parse_mode=ParseMode.HTML)]
                                for p in post["photos"][1:5]: media_group.append(InputMediaPhoto(p))
                                await context.bot.send_media_group(target, media=media_group)
                        elif post["video"]:
                            await context.bot.send_video(target, video=post["video"], caption=final_msg, parse_mode=ParseMode.HTML)
                        else:
                            await context.bot.send_message(target, final_msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    except Exception as media_err:
                        logger.warning(f"Media Send Error, sending as text only: {media_err}")
                        await context.bot.send_message(target, final_msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute("INSERT INTO posts (post_id, clean_text) VALUES (?, ?)", (post["id"], cleaned))
                        await db.commit()
                    await save_content_history(cleaned)
                    TOTAL_POSTED_TODAY += 1
                    await asyncio.sleep(12) # زيادة التأخير بين المنشورات لتجنب تجاوز حصة Gemini
                except Exception as e:
                    logger.error(f"Post Error: {e}")

def main():
    if not BOT_TOKEN: return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    # جلب الأخبار كل 60 ثانية (دقيقة واحدة)
    app.job_queue.run_repeating(scraping_job, interval=60, first=5)
    app.run_polling()

if __name__ == "__main__":
    main()
