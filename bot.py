import asyncio
import os
import sys
import random
import re
import logging
import time
import shutil
import base64
from datetime import datetime, timedelta
import aiosqlite
import boto3
from aiohttp import web
from dotenv import load_dotenv

# Telegram & AI
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
# تم إضافة FSInputFile هنا لإرسال الصور
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent, FSInputFile 

import google.generativeai as genai

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

# استيراد آمن يتوافق مع إصدارات المكتبة المختلفة لـ Stealth
try:
    from playwright_stealth import stealth_async
except ImportError:
    from playwright_stealth.stealth import stealth_async

import sentry_sdk
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

# ==========================================
# 1. إعدادات البيئة والمراقبة (Config & Monitoring)
# ==========================================
load_dotenv()

BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.environ.get('ADMIN_IDS', '').split(',') if id.strip()]
PORT = int(os.environ.get('PORT', 3000))
MAX_CONCURRENT_BROWSERS = int(os.environ.get('MAX_CONCURRENT_BROWSERS', 2))
MAX_TASK_RETRIES = int(os.environ.get('MAX_TASK_RETRIES', 3))
BROWSER_TYPE_ENV = os.environ.get('BROWSER_TYPE', 'chromium')

# Web Dashboard Auth
DASHBOARD_USER = os.environ.get('DASHBOARD_USER', 'admin')
DASHBOARD_PASS = os.environ.get('DASHBOARD_PASS', 'admin123')

# إعدادات Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if GEMINI_API_KEY and GEMINI_API_KEY.strip():
    genai.configure(api_key=GEMINI_API_KEY)
    AI_ENABLED = True
else:
    AI_ENABLED = False

# Sentry Setup
try:
    SENTRY_DSN = os.environ.get('SENTRY_DSN')
    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
        )
        print("✅ Sentry initialized successfully.")
    else:
        print("⚠️ Sentry DSN not found, skipping initialization...")
except Exception as e:
    print(f"❌ Failed to initialize Sentry: {e}")

# AWS S3
S3_BUCKET = os.environ.get('S3_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    region_name=os.environ.get('AWS_REGION')
) if S3_BUCKET else None

DB_NAME = "enterprise_queue.db"
BACKUP_DIR = "backups"
LOCAL_MEDIA_DIR = "media"
os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Prometheus
PROMETHEUS_TASKS_PROCESSED = Counter('tasks_processed_total', 'Total processed tasks')
PROMETHEUS_TASKS_FAILED = Counter('tasks_failed_total', 'Total failed tasks')
PROMETHEUS_QUEUE_SIZE = Gauge('tasks_queue_size', 'Current pending tasks in queue')

# ==========================================
# 2. السجلات وحماية الخصوصية (Logging & PII Redaction)
# ==========================================
class PIIFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[REDACTED_EMAIL]', str(record.msg))
        record.msg = re.sub(r'\+?\d{8,15}', '[REDACTED_PHONE]', str(record.msg))
        return True

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.addFilter(PIIFilter())

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ==========================================
# 3. دعم اللغات (i18n)
# ==========================================
LANG = {
    'ar': {
        'start': "<b>مرحباً بك في نظام الدعم الآلي المتقدم 👑</b>\n" + ("النظام متصل بالذكاء الاصطناعي." if AI_ENABLED else "النظام يعمل بالوضع القياسي (بدون ذكاء اصطناعي)."),
        'btn_new': "🚀 إرسال طلب جديد",
        'btn_status': "📊 حالة النظام",
        'btn_cancel': "❌ إلغاء",
        'spam_warn': "⚠️ يرجى الانتظار لتجنب الحظر (Rate Limit).",
        'unauthorized': "⛔ غير مصرح لك باستخدام هذا النظام.",
        'invalid_phone': "⚠️ رقم الهاتف غير صالح. يرجى إدخال أرقام فقط.",
        'invalid_email': "⚠️ البريد الإلكتروني غير صالح. يرجى إدخال بريد صحيح.",
        'invalid_text': "⚠️ النص قصير جداً. يرجى توضيح المشكلة بشكل أفضل.",
        'cancel_msg': "✅ تم إلغاء العملية.",
        'choose_country': "اختر رمز الدولة:",
        'enter_phone': "أرسل رقم الهاتف (أرقام فقط):",
        'enter_email': "📧 أرسل البريد الإلكتروني:",
        'enter_message': "📝 أرسل تفاصيل المشكلة:",
        'processing': "⏳ جاري المعالجة والحفظ في الطابور...",
        'saved': "🔄 <b>تم الحفظ في الطابور!</b>\nرقم التذكرة: #{id}\n\n<b>النص المعتمد:</b>\n<i>{msg}</i>"
    },
    'en': {
        'start': "<b>Welcome to the Advanced Auto-Support System 👑</b>\n" + ("Powered by AI." if AI_ENABLED else "Running in standard mode (No AI)."),
        'btn_new': "🚀 New Request",
        'btn_status': "📊 System Status",
        'btn_cancel': "❌ Cancel",
        'spam_warn': "⚠️ Please wait (Rate Limit).",
        'unauthorized': "⛔ Unauthorized access.",
        'invalid_phone': "⚠️ Invalid phone number. Digits only.",
        'invalid_email': "⚠️ Invalid email format.",
        'invalid_text': "⚠️ Message too short. Please provide more details.",
        'cancel_msg': "✅ Process cancelled.",
        'choose_country': "Select Country Code:",
        'enter_phone': "Send phone number (digits only):",
        'enter_email': "📧 Send email address:",
        'enter_message': "📝 Send issue details:",
        'processing': "⏳ Processing and saving to queue...",
        'saved': "🔄 <b>Saved to queue!</b>\nTicket ID: #{id}\n\n<b>Submitted Text:</b>\n<i>{msg}</i>"
    }
}

def get_text(user_lang, key, **kwargs):
    lang_code = 'ar' if user_lang not in LANG else user_lang
    text = LANG[lang_code].get(key, f"Missing text: {key}")
    return text.format(**kwargs) if kwargs else text

# ==========================================
# 4. قاعدة البيانات (Performance & Indexes)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country_code TEXT,
                local_phone TEXT,
                email TEXT,
                original_msg TEXT,
                ai_rewritten_msg TEXT,
                status TEXT DEFAULT 'pending',
                retries INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON queue(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON queue(created_at)")
        # إعادة تعيين المهام المعلقة عند إعادة التشغيل
        await db.execute("UPDATE queue SET status = 'pending' WHERE status = 'processing'")
        await db.commit()
    logger.info("🗄️ Database and indexes initialized.")

# ==========================================
# 5. تكامل الذكاء الاصطناعي (Gemini AI Fallback)
# ==========================================
async def rewrite_with_gemini(text: str) -> str:
    if not AI_ENABLED:
        return text
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"قم بإعادة صياغة طلب الدعم التالي ليكون احترافياً، واضحاً، ومباشراً وموجهاً لفريق دعم فني، دون إضافة معلومات غير موجودة في النص الأصلي:\n\n{text}"
        response = await model.generate_content_async(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return text

# ==========================================
# 6. إدارة الوسائط (S3 Cloud / Local Storage)
# ==========================================
async def upload_media(file_path: str, task_id: int, type_str: str) -> str:
    filename = f"{type_str}_task_{task_id}_{int(time.time())}.png"
    if s3_client and S3_BUCKET:
        try:
            await asyncio.to_thread(s3_client.upload_file, file_path, S3_BUCKET, filename)
            os.remove(file_path)
            return f"S3: {filename}"
        except Exception as e:
            logger.error(f"S3 Upload failed: {e}")
    
    local_path = os.path.join(LOCAL_MEDIA_DIR, filename)
    shutil.move(file_path, local_path)
    return local_path

# ==========================================
# 7. Middlewares (Security & Rate Limiting)
# ==========================================
user_rate_limit = {}

@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def security_middleware(handler, event, data):
    user_id = event.from_user.id
    lang = event.from_user.language_code or 'ar'
    data['lang'] = lang if lang in LANG else 'ar'

    if user_id not in ADMIN_IDS:
        logger.warning(f"Unauthorized access attempt! UserID: {user_id}")
        if isinstance(event, Message): 
            await event.answer(get_text(data['lang'], 'unauthorized'))
        return
    
    now = time.time()
    if user_id in user_rate_limit and now - user_rate_limit[user_id] < 1.0:
        if isinstance(event, Message): 
            await event.answer(get_text(data['lang'], 'spam_warn'))
        return
    user_rate_limit[user_id] = now
    return await handler(event, data)

@dp.error()
async def global_error_handler(event: ErrorEvent):
    logger.error(f"Global Error: {event.exception}", exc_info=True)
    if SENTRY_DSN: sentry_sdk.capture_exception(event.exception)

# ==========================================
# 8. واجهة التلغرام (UI / Forms / Validations)
# ==========================================
class FormSteps(StatesGroup):
    get_phone = State()
    get_email = State()
    get_message = State()

def get_main_menu(lang):
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=get_text(lang, 'btn_new')), KeyboardButton(text=get_text(lang, 'btn_status'))],
        [KeyboardButton(text=get_text(lang, 'btn_cancel'))]
    ], resize_keyboard=True)

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext, lang: str):
    await state.clear()
    await message.answer(get_text(lang, 'start'), reply_markup=get_main_menu(lang))

@dp.message(F.text.in_([LANG['ar']['btn_cancel'], LANG['en']['btn_cancel']]))
async def cancel_process(message: Message, state: FSMContext, lang: str):
    await state.clear()
    await message.answer(get_text(lang, 'cancel_msg'), reply_markup=get_main_menu(lang))

@dp.message(F.text.in_([LANG['ar']['btn_new'], LANG['en']['btn_new']]))
async def new_request(message: Message, state: FSMContext, lang: str):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🇾🇪 +967', callback_data='code_+967'), InlineKeyboardButton(text='🇸🇦 +966', callback_data='code_+966')],
        [InlineKeyboardButton(text='🇪🇬 +20', callback_data='code_+20'), InlineKeyboardButton(text='🇺🇸 +1', callback_data='code_+1')]
    ])
    await message.answer(get_text(lang, 'choose_country'), reply_markup=markup)

@dp.callback_query(F.data.startswith('code_'))
async def process_country(callback: CallbackQuery, state: FSMContext, lang: str):
    code = callback.data.replace('code_', '')
    await state.update_data(country_code=code)
    await state.set_state(FormSteps.get_phone)
    await callback.message.edit_text(f"✅ ({code})\n{get_text(lang, 'enter_phone')}")

@dp.message(FormSteps.get_phone)
async def process_phone(message: Message, state: FSMContext, lang: str):
    if not message.text.isdigit() or len(message.text) < 5:
        return await message.answer(get_text(lang, 'invalid_phone'))
    await state.update_data(local_phone=message.text)
    await state.set_state(FormSteps.get_email)
    await message.answer(get_text(lang, 'enter_email'))

@dp.message(FormSteps.get_email)
async def process_email(message: Message, state: FSMContext, lang: str):
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", message.text):
        return await message.answer(get_text(lang, 'invalid_email'))
    await state.update_data(email=message.text)
    await state.set_state(FormSteps.get_message)
    await message.answer(get_text(lang, 'enter_message'))

@dp.message(FormSteps.get_message)
async def process_message(message: Message, state: FSMContext, lang: str):
    if len(message.text.strip()) < 10:
        return await message.answer(get_text(lang, 'invalid_text'))
        
    processing_msg = await message.answer(get_text(lang, 'processing'))
    
    data = await state.get_data()
    original_msg = message.text
    ai_msg = await rewrite_with_gemini(original_msg)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            '''INSERT INTO queue (country_code, local_phone, email, original_msg, ai_rewritten_msg) VALUES (?, ?, ?, ?, ?)''', 
            (data['country_code'], data['local_phone'], data['email'], original_msg, ai_msg)
        )
        await db.commit()
        task_id = cursor.lastrowid
        
        cursor = await db.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'")
        PROMETHEUS_QUEUE_SIZE.set((await cursor.fetchone())[0])

    await processing_msg.edit_text(get_text(lang, 'saved', id=task_id, msg=ai_msg))
    await state.clear()

@dp.message(F.text.in_([LANG['ar']['btn_status'], LANG['en']['btn_status']]))
async def check_status(message: Message, lang: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
        stats = dict(await cursor.fetchall())
        
    msg = f"📊 <b>إحصائيات النظام</b>\n\n"
    msg += f"⏳ قيد الانتظار: {stats.get('pending', 0)}\n"
    msg += f"⚙️ قيد المعالجة: {stats.get('processing', 0)}\n"
    msg += f"✅ مكتمل: {stats.get('completed', 0)}\n"
    msg += f"❌ فاشل: {stats.get('failed', 0)}\n"
    
    await message.answer(msg)

# ==========================================
# 9. محرك الأتمتة (Playwright Engine)
# ==========================================
async def safe_page_goto(page: Page, url: str):
    for attempt in range(2):
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            return True
        except PlaywrightTimeoutError:
            if attempt == 1: raise
            await asyncio.sleep(2)

async def run_playwright_task(task: dict):
    browser, context, page = None, None, None
    try:
        async with async_playwright() as p:
            browser_launcher = getattr(p, BROWSER_TYPE_ENV)
            browser = await browser_launcher.launch(headless=True, args=['--no-sandbox'])
            context = await browser.new_context()
            page = await context.new_page()
            await stealth_async(page)

            await safe_page_goto(page, 'https://www.whatsapp.com/contact/noclient/?lang=en')
            await asyncio.sleep(2)

            phone_input = page.locator('input[name="phone_number"]').first
            await phone_input.wait_for(state='visible', timeout=15000)
            await phone_input.type(task['local_phone'], delay=random.randint(30, 80))
            
            email_input = page.locator('input[type="email"]').first
            await email_input.type(task['email'], delay=random.randint(30, 80))
            
            msg_box = page.locator('textarea').first
            await msg_box.type(task['ai_rewritten_msg'], delay=random.randint(10, 30))
            
            temp_img = f"temp_success_{task['id']}.png"
            await page.screenshot(path=temp_img, full_page=True)
            saved_path = await upload_media(temp_img, task['id'], "success")
            
            PROMETHEUS_TASKS_PROCESSED.inc()
            # إرجاع مسار الصورة في حالة النجاح أيضاً
            return True, "Task Completed", saved_path
            
    except Exception as e:
        logger.error(f"Task #{task['id']} failed: {e}")
        PROMETHEUS_TASKS_FAILED.inc()
        error_img_path = None
        if page:
            try:
                temp_err = f"temp_err_{task['id']}.png"
                # التقاط صورة الخطأ وتمرير مسارها للإرسال
                await page.screenshot(path=temp_err, full_page=True, timeout=10000)
                error_img_path = await upload_media(temp_err, task['id'], "error")
            except Exception as pic_err: 
                logger.error(f"Failed to capture screenshot: {pic_err}")
        
        # إرجاع الخطأ + مسار الصورة الملتقطة
        return False, str(e), error_img_path
    finally:
        if page: await page.close()
        if context: await context.close()
        if browser: await browser.close()

# ==========================================
# 10. العمال الخلفية والتقارير (Workers & Reports)
# ==========================================
async def browser_worker(worker_id: int):
    while True:
        task = None
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            # حل مشكلة التزامن (Race Condition): التأكد من تحديث الحالة بأمان
            cursor = await db.execute("SELECT * FROM queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1")
            row = await cursor.fetchone()
            if row:
                update_cursor = await db.execute("UPDATE queue SET status = 'processing' WHERE id = ? AND status = 'pending'", (row['id'],))
                if update_cursor.rowcount > 0:
                    await db.commit()
                    task = row
                else:
                    await db.rollback()
                
        if task:
            task_dict = dict(task)
            
            try:
                await bot.send_message(ADMIN_IDS[0], f"⚙️ بدء معالجة التذكرة #{task_dict['id']} (المنفذ {worker_id})...")
            except: pass

            success, info, img_path = await run_playwright_task(task_dict)
            
            async with aiosqlite.connect(DB_NAME) as db:
                if success:
                    await db.execute("UPDATE queue SET status = 'completed' WHERE id = ?", (task_dict['id'],))
                    msg = f"✅ <b>تمت بنجاح: تذكرة #{task_dict['id']}</b>\nالمسار: {info}"
                else:
                    new_retries = task_dict['retries'] + 1
                    # تم إضافة الخطأ المبرمج لتتمكن من قراءته مباشرة
                    error_text = str(info)[:500] 
                    if new_retries >= MAX_TASK_RETRIES:
                        await db.execute("UPDATE queue SET status = 'failed', retries = ? WHERE id = ?", (new_retries, task_dict['id']))
                        msg = f"❌ <b>فشل نهائي: تذكرة #{task_dict['id']}</b>\nالخطأ: <code>{error_text}</code>"
                    else:
                        await db.execute("UPDATE queue SET status = 'pending', retries = ? WHERE id = ?", (new_retries, task_dict['id']))
                        msg = f"⚠️ <b>تأجيل: تذكرة #{task_dict['id']}</b> سيتم إعادة المحاولة (المحاولة {new_retries}).\nالخطأ: <code>{error_text}</code>"
                await db.commit()
                
                cursor = await db.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'")
                PROMETHEUS_QUEUE_SIZE.set((await cursor.fetchone())[0])

            # إرسال الصورة للمسؤولين
            for admin_id in ADMIN_IDS:
                try:
                    if img_path and not img_path.startswith("S3:"):
                        photo = FSInputFile(img_path)
                        await bot.send_photo(admin_id, photo=photo, caption=msg)
                    else:
                        text_to_send = f"{msg}\n\nرابط الصورة: {img_path}" if img_path else msg
                        await bot.send_message(admin_id, text_to_send)
                except Exception as e:
                    logger.error(f"Failed to send alert to admin {admin_id}: {e}")
        else:
            await asyncio.sleep(2)

async def system_maintenance_worker():
    while True:
        try:
            backup_file = os.path.join(BACKUP_DIR, f"db_backup_{datetime.now().strftime('%Y%m%d')}.sqlite")
            shutil.copy2(DB_NAME, backup_file)
            
            async with aiosqlite.connect(DB_NAME) as db:
                seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
                await db.execute("DELETE FROM queue WHERE status IN ('completed', 'failed') AND created_at < ?", (seven_days_ago,))
                await db.commit()
                await db.execute("VACUUM")
                
            logger.info("🧹 System maintenance & backup completed.")
        except Exception as e:
            logger.error(f"Maintenance Error: {e}")
        await asyncio.sleep(86400)

async def daily_report_worker():
    while True:
        await asyncio.sleep(86400)
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                cursor = await db.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
                stats = dict(await cursor.fetchall())
                
            report = f"📅 <b>التقرير اليومي الآلي</b>\n\n"
            report += f"✅ الطلبات المنجزة: {stats.get('completed', 0)}\n"
            report += f"❌ الطلبات الفاشلة: {stats.get('failed', 0)}\n"
            report += f"⏳ في الطابور: {stats.get('pending', 0)}\n"
            
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, report)
        except Exception as e:
            logger.error(f"Daily Report Error: {e}")

# ==========================================
# 11. خادم الويب (Dashboard Authentication & Metrics)
# ==========================================
@web.middleware
async def auth_middleware(request, handler):
    if request.path == '/':
        auth_header = request.headers.get('Authorization')
        expected_auth = f"Basic {base64.b64encode(f'{DASHBOARD_USER}:{DASHBOARD_PASS}'.encode('utf-8')).decode('utf-8')}"
        
        if not auth_header or auth_header != expected_auth:
            return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Dashboard Login"'})
    return await handler(request)

async def web_dashboard(request):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
            stats = dict(await cursor.fetchall())
            
        html = f"""
        <html>
            <head>
                <title>Enterprise Bot Dashboard</title>
                <style>
                    body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; padding: 20px; color: #333; }}
                    .container {{ max-width: 800px; margin: auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
                    h2 {{ text-align: center; color: #007bff; }}
                    .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-top: 20px; }}
                    .card {{ padding: 20px; border-radius: 8px; font-size: 18px; font-weight: bold; text-align: center; color: white; }}
                    .bg-primary {{ background: #007bff; }}
                    .bg-warning {{ background: #ffc107; color: #333; }}
                    .bg-success {{ background: #28a745; }}
                    .bg-danger {{ background: #dc3545; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h2>📊 Enterprise Automation Dashboard</h2>
                    <p style="text-align: center;">AI Status: <b>{'Active' if AI_ENABLED else 'Inactive'}</b></p>
                    <div class="grid">
                        <div class="card bg-warning">⏳ Pending: {stats.get('pending', 0)}</div>
                        <div class="card bg-primary">⚙️ Processing: {stats.get('processing', 0)}</div>
                        <div class="card bg-success">✅ Completed: {stats.get('completed', 0)}</div>
                        <div class="card bg-danger">❌ Failed: {stats.get('failed', 0)}</div>
                    </div>
                </div>
            </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    except:
        return web.Response(text="Server Booting...")

async def metrics_handler(request):
    data = generate_latest()
    return web.Response(body=data, content_type=CONTENT_TYPE_LATEST)

async def start_web_server():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get('/', web_dashboard)
    app.router.add_get('/metrics', metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    logger.info(f"🌐 Dashboard protected by Basic Auth running on port {PORT}")

# ==========================================
# 12. حلقة التشغيل الرئيسية (Main Loop)
# ==========================================
async def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN is missing!")
        sys.exit(1)
    if not ADMIN_IDS:
        logger.critical("❌ ADMIN_IDS are missing!")
        sys.exit(1)

    await init_db()
    
    for i in range(MAX_CONCURRENT_BROWSERS):
        asyncio.create_task(browser_worker(i + 1))
        
    asyncio.create_task(system_maintenance_worker())
    asyncio.create_task(daily_report_worker())
        
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"🟢 Enterprise Bot started with {MAX_CONCURRENT_BROWSERS} workers using {BROWSER_TYPE_ENV.upper()}.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("System gracefully stopped.")
