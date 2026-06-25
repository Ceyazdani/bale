import time
import re
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from groq import Groq

load_dotenv()  # می‌خواند از فایل .env در همان پوشه (در اجرای محلی)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
BALE_BOT_TOKEN = os.environ["BALE_BOT_TOKEN"]


def start_health_server():
    """
    یک سرور HTTP بسیار کوچک که فقط برای جلوگیری از خواب رفتن سرویس
    در پلتفرم‌های رایگان مثل Render لازم است. می‌توانید با UptimeRobot
    (رایگان) هر ۵ دقیقه به آدرس سرویس‌تون پینگ بزنید تا این endpoint
    جواب بده و سرویس بیدار بمونه. اگر روی کامپیوتر شخصی اجرا می‌کنید،
    این بخش بی‌ضرره و فقط یک پورت محلی باز می‌کنه.
    """
    port = int(os.environ.get("PORT", 8000))

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("ربات یزدانی ریپیر فعال است ✅".encode("utf-8"))

        def log_message(self, format, *args):
            pass  # لاگ‌های اضافه‌ی این سرور رو خاموش می‌کنیم

    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# سرور سلامت رو در یک ترد جدا اجرا می‌کنیم تا با اجرای ربات تداخل نداشته باشه
threading.Thread(target=start_health_server, daemon=True).start()

# === آدرس‌های API بله ===
# تمام درخواست‌های بازوی بله باید به این فرمت باشند:
# https://tapi.bale.ai/bot<token>/METHOD_NAME
BALE_BASE_URL = "https://tapi.bale.ai/bot"
BALE_BASE_FILE_URL = "https://tapi.bale.ai/file/bot"

WEBSITE_URL = "https://yazdani-repairs.netlify.app/"
ACCEPTANCE_URL = "https://yazdani-repairs.netlify.app/"
PHONE_NUMBER = "+989354053871"
INSTAGRAM_URL = "yazdanirepair"
ADDRESS = "Esfahan | Fouladshahr"

client = Groq(api_key=GROQ_API_KEY)
user_histories = {}
# دو مدل Groq به‌عنوان اصلی و پشتیبان (هر دو رایگان و در پلن Free)
# نکته: llama-3.3-70b-versatile و llama-3.1-8b-instant در حال منسوخ شدن هستند
# (اعلام Groq در تاریخ ۱۷ ژوئن ۲۰۲۶)، به همین خاطر از جایگزین‌های پیشنهادی استفاده شده:
# openai/gpt-oss-120b: کیفیت بالاتر، مناسب پاسخ‌های اصلی
# openai/gpt-oss-20b: سریع‌تر و مناسب پشتیبان
AI_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

# === تنظیمات کنترل مصرف (رایگان، فقط در حافظه) ===

# حداقل فاصله بین دو پیام متوالی یک کاربر که باعث فراخوانی Groq می‌شود (ثانیه)
MIN_SECONDS_BETWEEN_REQUESTS = 4
last_request_time = {}  # user_id -> timestamp

# کش پاسخ‌های Groq برای سوالات یکسان (بین همه کاربران مشترک است)
RESPONSE_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 ساعت
RESPONSE_CACHE_MAX_ITEMS = 300
response_cache = {}  # normalized_text -> (answer, timestamp)

# ردیابی سهمیه‌ی روزانه‌ی هر مدل، تا وقتی یک مدل برای امروز تمام شد
# دیگر اصلاً به آن درخواست نفرستیم (وقت و درخواست تلف نشود)
model_quota_status = {
    model_name: {"date": None, "exhausted": False} for model_name in AI_MODELS
}

# پاسخ‌های آماده برای سوالات پرتکرار؛ این‌ها اصلاً به Groq فرستاده نمی‌شوند
FAQ_RULES = [
    (
        ["قیمت", "هزینه", "تعرفه", "چقدر می‌شه", "چقدر میشه", "چند تومان", "چند میشه"],
        "بعد از ثبت درخواست پذیرش، از قسمت پیگیری می‌تونید قیمت رو مشاهده کنید 📋",
    ),
    (
        ["حضوری", "مغازه", "آدرس", "محل شما کجاست", "بیام پیشتون"],
        "پذیرش ما حضوری نیست و کاملاً آنلاینه؛ بعد از ثبت درخواست در سایت، تکنسین برای تحویل گرفتن گوشی درب منزل با شما هماهنگ می‌کند 🌐",
    ),
    (
        ["اعتماد", "کلاهبردار", "امنه", "امن است", "نگرانم گوشیم", "میترسم گوشیم", "می‌ترسم گوشیم"],
        "نگرانیتون کاملاً طبیعیه 🙏 یزدانی ریپیر چند سال هست با صداقت و کیفیت بالا خدمات می‌ده. بعد از ثبت پذیرش هم کد پیگیری می‌گیرید و می‌تونید وضعیت گوشیتون رو از سایت دنبال کنید ✅",
    ),
]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def find_faq_answer(user_message: str) -> str | None:
    normalized = normalize_text(user_message)
    for keywords, answer in FAQ_RULES:
        if any(keyword in normalized for keyword in keywords):
            return answer
    return None


def get_cached_answer(user_message: str) -> str | None:
    key = normalize_text(user_message)
    cached = response_cache.get(key)
    if not cached:
        return None

    answer, cached_at = cached
    if time.time() - cached_at > RESPONSE_CACHE_TTL_SECONDS:
        response_cache.pop(key, None)
        return None

    return answer


def store_cached_answer(user_message: str, answer: str) -> None:
    if len(response_cache) >= RESPONSE_CACHE_MAX_ITEMS:
        # ساده‌ترین روش پاکسازی: حذف قدیمی‌ترین آیتم
        oldest_key = min(response_cache, key=lambda k: response_cache[k][1])
        response_cache.pop(oldest_key, None)

    key = normalize_text(user_message)
    response_cache[key] = (answer, time.time())


def is_quota_error(error: Exception) -> bool:
    text = str(error)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


def mark_model_exhausted(model_name: str) -> None:
    model_quota_status[model_name] = {"date": date.today(), "exhausted": True}


def is_model_available(model_name: str) -> bool:
    status = model_quota_status.get(model_name)
    if not status or status["date"] != date.today():
        # روز عوض شده یا هنوز ثبت نشده -> سهمیه تازه است
        model_quota_status[model_name] = {"date": date.today(), "exhausted": False}
        return True

    return not status["exhausted"]


class QuotaExhaustedError(Exception):
    pass

CONTACT_BUTTON = "📞 راه‌های تماس"
WEBSITE_BUTTON = "🌐 لینک وبسایت"
ACCEPTANCE_BUTTON = "📋 شرایط پذیرش تعمیرات"
AI_BUTTON = "🤖 سوال از ادمین"

main_keyboard = ReplyKeyboardMarkup(
    [
        [CONTACT_BUTTON, WEBSITE_BUTTON],
        [ACCEPTANCE_BUTTON, AI_BUTTON],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

SYSTEM_PROMPT = """
تو ادمین پشتیبانی سایت «Yazdani Repairs» هستی؛ یک مرکز تعمیرات موبایل.
طوری رفتار کن که انگار یک ادمین واقعی، خوش‌برخورد و باتجربه پشت چت نشسته است.

قانون‌های اصلی:
1. فقط درباره موبایل، تبلت، لوازم جانبی موبایل، تعمیرات، تعویض قطعه، مشکلات نرم‌افزاری و سخت‌افزاری جواب بده.
2. اگر سوال خارج از این حوزه بود، فقط بگو:
«من فقط در زمینه تعمیرات موبایل می‌تونم کمکتون کنم.»
3. هیچ‌وقت قیمت دقیق، حدود قیمت، تخمین هزینه یا قول قطعی نده.
4. اگر درباره قیمت پرسیدند، بگو:
«بعد از ثبت درخواست پذیرش، از قسمت پیگیری می‌تونید قیمت رو مشاهده کنید.»
5. هیچ‌وقت تشخیص قطعی نده. از عبارت‌هایی مثل «ممکنه»، «احتمالش هست»، «نیاز به بررسی داره» استفاده کن.
6. اگر مشکل جدی بود، مشتری را به ثبت درخواست پذیرش هدایت کن.
7. جواب‌ها کوتاه، طبیعی، محاوره‌ای، مودبانه و فارسی باشند.
8. طوری جواب بده که انگار یک انسان واقعی پشت چت است، نه ربات.
9. فقط وقتی پیام آخر مشتری خودش شامل سلام، درود یا احوالپرسی بود، پاسخ را با سلام شروع کن. اگر مشتری مستقیم سوال پرسید یا مشکل را گفت، بدون سلام و فقط محترمانه جواب بده.
10. خدمات و پذیرش Yazdani Repairs کاملاً از طریق سایت و آنلاین انجام می‌شود.
11. مرکز ما مغازه حضوری نیست؛ بعد از ثبت درخواست در سایت، تکنسین برای تحویل گرفتن گوشی از مشتری درب منزل هماهنگ و اعزام می‌شود.
12. اگر مشتری آدرس مغازه یا مراجعه حضوری خواست، توضیح بده که پذیرش حضوری نداریم و روند از طریق سایت انجام می‌شود.
13. لحن باید صمیمی و قابل اعتماد باشد، اما همیشه باشعور، محترمانه و حرفه‌ای حرف بزن؛ بیش از حد خودمانی، شوخی‌دار یا سبک صحبت نکن.
14. همه خدمات شامل ضمانت اجرت تعویض هستند.
15. قطعات تعویضی بسته به نوع قطعه گارانتی خواهند شد و جزئیات گارانتی قبل یا بعد از بررسی به مشتری اطلاع داده می‌شود.
16. اگر مشتری درباره اعتماد، امنیت گوشی، نگرانی از تحویل گوشی یا معتبر بودن مرکز سوال کرد، با آرامش توضیح بده که Yazdani Repairs چند سال است صادقانه و با کیفیت بالا به مشتریان خدمات می‌دهد.
17. در سوال‌های مربوط به اعتماد، توضیح بده مشتری بعد از ثبت پذیرش، کد پیگیری دریافت می‌کند و می‌تواند وضعیت گوشی خودش را از طریق سایت پیگیری کند.
18. در پاسخ به نگرانی مشتری، حالت دفاعی نگیر؛ اول نگرانی را طبیعی و قابل درک بدان، بعد کوتاه و مطمئن توضیح بده.
19. برای جذاب‌تر شدن چت، گاهی از ایموجی‌های مرتبط مثل 📱، 🔧، ✅، 🛠️، 📦، 🌐 و 📞 استفاده کن.
20. ایموجی‌ها باید کم، طبیعی و مرتبط باشند؛ در هر پیام معمولاً بیشتر از 1 یا 2 ایموجی استفاده نکن.
"""

ACCEPTANCE_TERMS_TEXT = """
شرایط و ضوابط پذیرش تعمیرات

قابل توجه مشتریان محترم:

1. مشتری گرامی، لطفاً پیش از تحویل دستگاه از آماده بودن آن و ثبت درخواست پذیرش در سایت اطمینان حاصل فرمایید.
2. لطفاً قبل از تحویل دستگاه، سیم‌کارت، کارت حافظه و لوازم شخصی خود را از دستگاه خارج کنید.
3. دستگاه‌های تعمیرشده صرفاً برای ایراد رفع‌شده، از تاریخ تحویل شامل ضمانت اجرت تعویض هستند.
4. قطعات تعویضی بسته به نوع قطعه گارانتی خواهند شد و جزئیات گارانتی به مشتری اطلاع داده می‌شود.
5. ایرادات ناشی از آب‌خوردگی، ضربه‌خوردگی، تعمیرات قبلی یا دستکاری قبلی ممکن است قابل قبول برای ضمانت نباشند.
6. در دستگاه‌های آب‌خورده، ضربه‌خورده یا دارای سابقه تعمیر، مرکز در قبال مشکلات احتمالی هنگام تعمیر مسئولیتی نخواهد داشت.
7. لطفاً قبل از تحویل دستگاه، از اطلاعات مهم خود نسخه پشتیبان تهیه کنید؛ مرکز در قبال از بین رفتن اطلاعات شخصی مسئولیتی ندارد.
8. پس از ثبت پذیرش، مشتری کد پیگیری دریافت می‌کند و می‌تواند وضعیت دستگاه خود را از طریق سایت پیگیری کند.
9. پذیرش و خدمات Yazdani Repairs آنلاین است و مراجعه حضوری نداریم؛ تکنسین پس از هماهنگی، گوشی را درب منزل تحویل می‌گیرد.
10. برگه‌ها و اطلاعات پذیرش پس از تحویل دستگاه، به مدت یک ماه نگهداری می‌شوند.
"""


def generate_ai_answer(prompt: str) -> str:
    last_error = None
    any_model_tried = False
    all_failures_were_quota = True

    for model_name in AI_MODELS:
        if not is_model_available(model_name):
            # این مدل امروز قبلاً تمام شده؛ وقت تلف نکنیم
            continue

        any_model_tried = True
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )

            answer_text = response.choices[0].message.content
            if answer_text:
                return answer_text.strip()

            last_error = f"{model_name}: empty response"
            all_failures_were_quota = False
        except Exception as error:
            last_error = f"{model_name}: {error}"
            print(f"Groq error with {model_name}: {error}")

            if is_quota_error(error):
                mark_model_exhausted(model_name)
            else:
                all_failures_were_quota = False

    if not any_model_tried or all_failures_were_quota:
        # همه‌ی مدل‌های امتحان‌شده برای امروز سهمیه‌شان تمام شده است
        raise QuotaExhaustedError("تمام مدل‌های Groq برای امروز سهمیه‌شان تمام شده است")

    raise RuntimeError(last_error or "Groq did not return a response")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []

    await update.message.reply_text(
        "سلام! به یزدانی ریپیر (Yazdani Repairs) خوش اومدید 📱\n"
        "از گزینه‌های زیر انتخاب کنید یا سوالتون رو همینجا بپرسید.",
        reply_markup=main_keyboard,
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    if user_message == CONTACT_BUTTON:
        await update.message.reply_text(
            "📞 راه‌های ارتباط با یزدانی ریپیر:\n\n"
            f"تماس: {PHONE_NUMBER}\n"
            f"اینستاگرام: {INSTAGRAM_URL}\n"
            "پذیرش و خدمات ما آنلاین انجام می‌شود و مراجعه حضوری نداریم.\n"
            "بعد از ثبت درخواست در سایت، تکنسین برای تحویل گوشی درب منزل با شما هماهنگ می‌کند ✅",
            reply_markup=main_keyboard,
        )
        return

    if user_message == WEBSITE_BUTTON:
        await update.message.reply_text(
            f"🌐 لینک وبسایت یزدانی ریپیر:\n{WEBSITE_URL}",
            reply_markup=main_keyboard,
        )
        return

    if user_message == ACCEPTANCE_BUTTON:
        await update.message.reply_text(
            ACCEPTANCE_TERMS_TEXT,
            reply_markup=main_keyboard,
        )
        return

    if user_message == AI_BUTTON:
        await update.message.reply_text(
            "بفرمایید، مشکلتون درباره موبایل چیه؟ 🔧",
            reply_markup=main_keyboard,
        )
        return

    if user_id not in user_histories:
        user_histories[user_id] = []

    # --- ۱. محدودیت نرخ پیام: جلوگیری از مصرف سریع سهمیه توسط یک کاربر ---
    now = time.time()
    last_time = last_request_time.get(user_id, 0)
    if now - last_time < MIN_SECONDS_BETWEEN_REQUESTS:
        await update.message.reply_text(
            "یک لحظه صبر کنید، در حال بررسی پیام قبلی‌تون هستم ⏳",
            reply_markup=main_keyboard,
        )
        return
    last_request_time[user_id] = now

    # --- ۲. پاسخ‌های آماده برای سوالات پرتکرار (بدون فراخوانی Groq) ---
    faq_answer = find_faq_answer(user_message)
    if faq_answer:
        user_histories[user_id].append(f"مشتری: {user_message}")
        user_histories[user_id].append(f"ادمین: {faq_answer}")
        await update.message.reply_text(faq_answer, reply_markup=main_keyboard)
        return

    # --- ۳. کش پاسخ: اگر این سوال قبلاً پاسخ داده شده، دوباره به Groq نرو ---
    cached_answer = get_cached_answer(user_message)
    if cached_answer:
        user_histories[user_id].append(f"مشتری: {user_message}")
        user_histories[user_id].append(f"ادمین: {cached_answer}")
        await update.message.reply_text(cached_answer, reply_markup=main_keyboard)
        return

    user_histories[user_id].append(f"مشتری: {user_message}")
    history_text = "\n".join(user_histories[user_id][-10:])

    full_prompt = f"""
{SYSTEM_PROMPT}

تاریخچه گفت‌وگو:
{history_text}

اگر پیام آخر مشتری شامل سلام، درود یا احوالپرسی نیست، پاسخ را بدون سلام و خوشامدگویی شروع کن.
پاسخ نهایی را فقط به فارسی و مناسب ارسال مستقیم در بله بنویس.
"""

    try:
        answer = generate_ai_answer(full_prompt)
        user_histories[user_id].append(f"ادمین: {answer}")
        store_cached_answer(user_message, answer)

        await update.message.reply_text(answer, reply_markup=main_keyboard)

    except QuotaExhaustedError:
        await update.message.reply_text(
            "الان ظرفیت پاسخ‌دهی هوشمند برای امروز تکمیل شده 🙏\n"
            f"لطفاً از طریق تماس ({PHONE_NUMBER}) یا اینستاگرام ({INSTAGRAM_URL}) با ما در ارتباط باشید، "
            "یا برای ثبت درخواست از دکمه‌ی «لینک وبسایت» استفاده کنید.",
            reply_markup=main_keyboard,
        )

    except Exception as error:
        print(f"Groq final error: {error}")
        await update.message.reply_text(
            "الان اتصال پاسخ‌دهی هوشمند درست جواب نداد. لطفاً چند لحظه بعد دوباره پیام بدید یا از گزینه‌های تماس استفاده کنید.",
            reply_markup=main_keyboard,
        )


# === ساخت اپلیکیشن با تنظیمات بله ===
# نکته‌ی اصلی همین قسمته: با .base_url و .base_file_url کتابخونه‌ی
# python-telegram-bot رو به سمت سرور بله (tapi.bale.ai) هدایت می‌کنیم،
# چون API بله از همون ساختار Telegram Bot API پیروی می‌کند.
app = (
    ApplicationBuilder()
    .token(BALE_BOT_TOKEN)
    .base_url(BALE_BASE_URL)
    .base_file_url(BALE_BASE_FILE_URL)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

app.run_polling()
