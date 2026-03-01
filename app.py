from flask import Flask, request, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import os, random, json, logging, threading, time, re, requests
from datetime import datetime, date

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SECRET = os.getenv("LINE_CHANNEL_SECRET")
PORT = int(os.getenv("PORT", 5000))
HEROKU_URL = os.getenv("HEROKU_URL", "")

configuration = Configuration(access_token=ACCESS_TOKEN)
handler = WebhookHandler(SECRET)

DATA_FILE = "data.json"
CONTENT_FILE = "content.json"
RAMADAN_FILE = "ramadan.json"

# Lock لمنع مشاكل الكتابة المتزامنة
data_lock = threading.Lock()

# تخزين الروابط لكل مستخدم
user_links = {}

# تخزين الأدعية المستخدمة لكل مستخدم
used_ramadan_duaa = {}

# كلمات السلام المعتمدة
SALAM_WORDS = [
    "السلام عليكم",
    "السلام عليكم ورحمة الله",
    "السلام عليكم ورحمة الله وبركاته"
]

def load_json(file, default):
    """تحميل ملف JSON مع معالجة الأخطاء"""
    if not os.path.exists(file):
        with open(file, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"خطأ في تحميل {file}: {e}")
        return default

def save_data():
    """حفظ البيانات إلى ملف JSON مع Thread Safety"""
    try:
        with data_lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "users": list(target_users),
                    "groups": list(target_groups),
                    "tasbih": tasbih_counts,
                    "last_reset": last_reset_dates,
                    "notifications_off": list(notifications_off),
                    "used_ramadan_duaa": {k: list(v) for k, v in used_ramadan_duaa.items()}
                }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطأ في الحفظ: {e}")

# تحميل البيانات
data = load_json(DATA_FILE, {
    "users": [], 
    "groups": [], 
    "tasbih": {}, 
    "last_reset": {},
    "notifications_off": [],
    "used_ramadan_duaa": {}
})
target_users = set(data.get("users", []))
target_groups = set(data.get("groups", []))
tasbih_counts = data.get("tasbih", {})
last_reset_dates = data.get("last_reset", {})
notifications_off = set(data.get("notifications_off", []))
used_ramadan_duaa = {k: set(v) for k, v in data.get("used_ramadan_duaa", {}).items()}

content = load_json(CONTENT_FILE, {"adhkar": []})
fadl_content = load_json("fadl.json", {"fadl": []}).get("fadl", [])
ramadan_duaa_list = load_json(RAMADAN_FILE, {"duaa": []}).get("duaa", [])

TASBIH_LIMITS = 33
TASBIH_KEYS = ["استغفر الله", "سبحان الله", "الحمد لله", "الله أكبر"]

# إعدادات التذكير التلقائي
AUTO_REMINDER_ENABLED = True
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 8

def extract_links(text):
    """استخراج الروابط من النص"""
    url_pattern = r'(https?://\S+|www\.\S+)'
    return re.findall(url_pattern, text)

def get_next_fadl():
    """الحصول على فضل عشوائي"""
    if not fadl_content:
        return "لا يوجد فضل متاح حاليا"
    return random.choice(fadl_content)

def get_ramadan_duaa(user_id):
    """الحصول على دعاء رمضان بدون تكرار"""
    if not ramadan_duaa_list:
        return "لا توجد أدعية متاحة حاليا"
    
    # إذا لم يكن للمستخدم قائمة، أنشئها
    if user_id not in used_ramadan_duaa:
        used_ramadan_duaa[user_id] = set()
    
    # إذا استخدم جميع الأدعية، أعد التصفير
    if len(used_ramadan_duaa[user_id]) >= len(ramadan_duaa_list):
        used_ramadan_duaa[user_id] = set()
        message = ""
    else:
        message = ""
    
    # احصل على الأدعية المتبقية
    remaining_duaa = [d for i, d in enumerate(ramadan_duaa_list) if i not in used_ramadan_duaa[user_id]]
    
    # اختر دعاء عشوائي من المتبقية
    selected_duaa = random.choice(remaining_duaa)
    selected_index = ramadan_duaa_list.index(selected_duaa)
    
    # أضفه للقائمة المستخدمة
    used_ramadan_duaa[user_id].add(selected_index)
    
    # احفظ البيانات
    save_data()
    
    # فقط الدعاء بدون أي إضافات
    return message + selected_duaa

def send_message(target_id, text):
    """إرسال رسالة إلى مستخدم أو مجموعة"""
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
        return True
    except Exception as e:
        error_str = str(e)
        if "400" not in error_str and "403" not in error_str:
            logger.error(f"فشل الإرسال إلى {target_id}: {e}")
        return False

def reply_message(reply_token, text):
    """الرد على رسالة"""
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))
        return True
    except Exception as e:
        logger.error(f"فشل الرد: {e}")
        return False

def broadcast_text(text, exclude_user=None, exclude_group=None):
    """إرسال رسالة جماعية لجميع المستخدمين والمجموعات"""
    sent, failed = 0, 0
    
    for uid in list(target_users):
        if uid != exclude_user and uid not in notifications_off:
            if send_message(uid, text):
                sent += 1
            else:
                failed += 1
    
    for gid in list(target_groups):
        if gid != exclude_group and gid not in notifications_off:
            if send_message(gid, text):
                sent += 1
            else:
                failed += 1
    
    logger.info(f"الإرسال الجماعي: {sent} نجح، {failed} فشل")
    return sent, failed

def get_user_name(user_id):
    """الحصول على اسم المستخدم"""
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            profile = api.get_profile(user_id)
            return profile.display_name
    except:
        return "المستخدم"

def get_group_member_name(group_id, user_id):
    """الحصول على اسم عضو في المجموعة"""
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            profile = api.get_group_member_profile(group_id, user_id)
            return profile.display_name
    except:
        return "المستخدم"

def ensure_user_counts(uid):
    """التأكد من وجود بيانات التسبيح للمستخدم"""
    if uid not in tasbih_counts:
        tasbih_counts[uid] = {key: 0 for key in TASBIH_KEYS}
        last_reset_dates[uid] = str(date.today())
        save_data()
        logger.info(f"تم إنشاء بيانات تسبيح جديدة للمستخدم: {uid}")

def reset_tasbih_if_needed(user_id):
    """تصفير التسبيح إذا كان يوم جديد"""
    today = str(date.today())
    last_reset = last_reset_dates.get(user_id)
    
    if last_reset != today:
        tasbih_counts[user_id] = {key: 0 for key in TASBIH_KEYS}
        last_reset_dates[user_id] = today
        save_data()
        logger.info(f"تم تصفير التسبيح تلقائيًا للمستخدم: {user_id}")
        return True
    return False

def get_tasbih_status(user_id, gid=None):
    """عرض حالة التسبيح للمستخدم"""
    counts = tasbih_counts[user_id]
    name = get_group_member_name(gid, user_id) if gid else get_user_name(user_id)
    
    status = f"حالة التسبيح\n{name}\n\n"
    
    for key in TASBIH_KEYS:
        count = counts[key]
        status += f"{key}: {count}/33\n"
    
    all_complete = all(counts[k] >= TASBIH_LIMITS for k in TASBIH_KEYS)
    if all_complete:
        status += "\nتم إكمال جميع الأذكار"
    
    return status

def normalize_tasbih(text):
    """تطبيع نص التسبيح للمقارنة"""
    text = text.replace(" ", "").replace("ٱ", "ا").replace("أ", "ا").replace("إ", "ا").replace("ة", "ه")
    
    mapping = {
        "استغفرالله": "استغفر الله",
        "سبحانالله": "سبحان الله",
        "الحمدلله": "الحمد لله",
        "اللهأكبر": "الله أكبر",
        "اللهاكبر": "الله أكبر"
    }
    
    return mapping.get(text)

# ========================================
# خدمة Keep-Alive الجديدة
# ========================================
def keep_heroku_alive():
    """إرسال ping للبوت نفسه كل 5 دقائق لمنع النوم"""
    time.sleep(60)  # انتظر دقيقة قبل البدء
    
    if not HEROKU_URL:
        logger.warning("✗ HEROKU_URL غير موجود - Self-Ping معطل")
        logger.warning("→ أضف المتغير في Heroku: HEROKU_URL=https://your-bot.herokuapp.com")
        return
    
    logger.info(f"✓ بدء خدمة Keep-Alive للرابط: {HEROKU_URL}")
    
    while True:
        try:
            time.sleep(300)  # كل 5 دقائق
            
            response = requests.get(f"{HEROKU_URL}/health", timeout=10)
            
            if response.status_code == 200:
                logger.info("✓ Keep-Alive: البوت نشط")
            else:
                logger.warning(f"✗ Keep-Alive: استجابة {response.status_code}")
                
        except Exception as e:
            logger.error(f"✗ خطأ في Keep-Alive: {e}")
            time.sleep(300)

# ========================================
# خدمة التذكير التلقائي المحسّنة
# ========================================
def auto_reminder_service():
    """خدمة التذكير التلقائي - أوقات متفرقة"""
    logger.info("✓ بدء خدمة التذكير التلقائي (من ملف الفضل)")
    
    while AUTO_REMINDER_ENABLED:
        try:
            sleep_hours = random.uniform(MIN_INTERVAL_HOURS, MAX_INTERVAL_HOURS)
            sleep_seconds = sleep_hours * 3600
            
            logger.info(f"→ التذكير القادم بعد {sleep_hours:.1f} ساعة")
            time.sleep(sleep_seconds)
            
            if len(target_users) > 0 or len(target_groups) > 0:
                if fadl_content:
                    message = random.choice(fadl_content)
                    sent, failed = broadcast_text(message)
                    logger.info(f"→ تذكير تلقائي (فضل): تم الإرسال إلى {sent} - فشل {failed}")
                else:
                    logger.warning("✗ لا يوجد فضل متاح للإرسال في fadl.json")
            else:
                logger.info("✗ لا يوجد مستخدمين مسجلين")
                
        except Exception as e:
            logger.error(f"✗ خطأ في التذكير التلقائي: {e}")
            time.sleep(3600)

# تشغيل خدمة Keep-Alive
keep_alive_thread = threading.Thread(target=keep_heroku_alive, daemon=True)
keep_alive_thread.start()
logger.info("✓ تم تشغيل خدمة Keep-Alive")

# تشغيل خدمة التذكير التلقائي
if AUTO_REMINDER_ENABLED:
    reminder_thread = threading.Thread(target=auto_reminder_service, daemon=True)
    reminder_thread.start()
    logger.info("✓ تم تشغيل خدمة التذكير التلقائي")

def is_valid_command(text):
    """التحقق من صحة الأمر"""
    valid_commands = ["مساعدة", "فضل", "تسبيح", "ذكرني", "إعادة", "إيقاف", "تشغيل", "احصائيات", "رمضان"]
    txt = text.lower().strip()
    
    if txt in [c.lower() for c in valid_commands]:
        return True
    if normalize_tasbih(text):
        return True
    return False

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """معالج الرسائل الرئيسي"""
    try:
        user_text = event.message.text.strip()
        user_id = event.source.user_id
        gid = getattr(event.source, "group_id", None)

        # الرد على السلام
        if user_text in SALAM_WORDS:
            reply_message(event.reply_token, "وعليكم السلام ورحمة الله وبركاته")
            return

        # تسجيل المستخدم الجديد
        if user_id not in target_users:
            target_users.add(user_id)
            save_data()
            logger.info(f"مستخدم جديد: {user_id}")

        # تسجيل المجموعة الجديدة
        if gid and gid not in target_groups:
            target_groups.add(gid)
            save_data()
            logger.info(f"مجموعة جديدة: {gid}")

        # تحذير من تكرار الروابط (فقط في المجموعات)
        if gid:
            links = extract_links(user_text)
            
            if links:
                if user_id not in user_links:
                    user_links[user_id] = set()
                
                for link in links:
                    if link in user_links[user_id]:
                        reply_message(
                            event.reply_token,
                            "تنبيه:\nممنوع تكرار نفس الرابط أكثر من مرة"
                        )
                        return
                    else:
                        user_links[user_id].add(link)

        # التأكد من وجود بيانات التسبيح
        ensure_user_counts(user_id)
        was_reset = reset_tasbih_if_needed(user_id)

        # تجاهل الرسائل غير الصحيحة - البوت صامت
        if not is_valid_command(user_text):
            return

        text_lower = user_text.lower()

        # أمر المساعدة
        if text_lower == "مساعدة":
            help_text = """بوت85 - الأوامر المتاحة

ذكرني
ارسال ذكر لجميع المستخدمين والمجموعات

فضل
عرض فضل العبادات والأذكار

رمضان
عرض دعاء من أدعية رمضان

تسبيح
عرض حالة التسبيح الخاصة بك

إعادة
تصفير عداد التسبيح وبدء من جديد

تم إنشاء هذا البوت بواسطة عبير الدوسري"""
            reply_message(event.reply_token, help_text)
            return

        # أمر رمضان
        if text_lower == "رمضان":
            duaa = get_ramadan_duaa(user_id)
            reply_message(event.reply_token, duaa)
            logger.info(f"تم إرسال دعاء رمضان للمستخدم: {user_id}")
            return

        # أمر إيقاف التذكير
        if text_lower == "إيقاف":
            target_id = gid if gid else user_id
            if target_id not in notifications_off:
                notifications_off.add(target_id)
                save_data()
                msg = "تم إيقاف التذكير التلقائي لهذه المجموعة" if gid else "تم إيقاف التذكير التلقائي لك"
                reply_message(event.reply_token, msg)
                logger.info(f"إيقاف التذكير: {target_id}")
            else:
                reply_message(event.reply_token, "التذكير موقف مسبقا")
            return

        # أمر تشغيل التذكير
        if text_lower == "تشغيل":
            target_id = gid if gid else user_id
            if target_id in notifications_off:
                notifications_off.remove(target_id)
                save_data()
                msg = "تم تشغيل التذكير التلقائي لهذه المجموعة" if gid else "تم تشغيل التذكير التلقائي لك"
                reply_message(event.reply_token, msg)
                logger.info(f"تشغيل التذكير: {target_id}")
            else:
                reply_message(event.reply_token, "التذكير يعمل مسبقا")
            return

        # أمر الفضل
        if text_lower == "فضل":
            reply_message(event.reply_token, get_next_fadl())
            return

        # أمر التسبيح
        if text_lower == "تسبيح":
            status = get_tasbih_status(user_id, gid)
            if was_reset:
                status = "تم تصفير العداد ليوم جديد\n\n" + status
            reply_message(event.reply_token, status)
            return

        # أمر الإعادة
        if text_lower == "إعادة":
            tasbih_counts[user_id] = {key: 0 for key in TASBIH_KEYS}
            last_reset_dates[user_id] = str(date.today())
            save_data()
            reply_message(event.reply_token, "تم تصفير عداد التسبيح بنجاح\nيمكنك البدء من جديد")
            logger.info(f"تم تصفير التسبيح يدويا: {user_id}")
            return

        # أمر الإحصائيات
        if text_lower == "احصائيات":
            total_tasbih = sum(sum(counts.values()) for counts in tasbih_counts.values())
            active_receivers = len(target_users) + len(target_groups) - len(notifications_off)
            
            stats_text = f"""احصائيات بوت85

إجمالي المستخدمين: {len(target_users)}
إجمالي المجموعات: {len(target_groups)}
إجمالي التسبيحات: {total_tasbih}
المستخدمون النشطون: {len(tasbih_counts)}
المستقبلون النشطون: {active_receivers}
التذكير موقف: {len(notifications_off)}
التذكير التلقائي: {'مفعل' if AUTO_REMINDER_ENABLED else 'معطل'}
Keep-Alive: {'مفعل' if HEROKU_URL else 'معطل'}
فترة التذكير: {MIN_INTERVAL_HOURS}-{MAX_INTERVAL_HOURS} ساعة (أوقات متفرقة)
أدعية رمضان: {len(ramadan_duaa_list)} دعاء

تم إنشاء هذا البوت بواسطة عبير الدوسري"""
            reply_message(event.reply_token, stats_text)
            return

        # معالجة التسبيح
        normalized = normalize_tasbih(user_text)
        if normalized:
            counts = tasbih_counts[user_id]
            
            if counts[normalized] >= TASBIH_LIMITS:
                reply_message(event.reply_token, f"تم اكتمال {normalized} مسبقا\nاستخدم أمر: إعادة\nلتصفير العداد")
                return
            
            counts[normalized] += 1
            save_data()

            if counts[normalized] == TASBIH_LIMITS:
                reply_message(event.reply_token, f"تم اكتمال {normalized}")
                
                if all(counts[k] >= TASBIH_LIMITS for k in TASBIH_KEYS):
                    time.sleep(1)
                    send_message(user_id, "تم اكتمال جميع التسبيحات الأربعة\nجزاك الله خيرا")
                return
            
            reply_message(event.reply_token, get_tasbih_status(user_id, gid))
            return

        # أمر ذكرني
        if text_lower == "ذكرني":
            try:
                adhkar_list = content.get("adhkar", [])
                
                if not adhkar_list:
                    reply_message(event.reply_token, "لا يوجد أذكار متاحة")
                    logger.warning("لا يوجد أذكار في content.json")
                    return
                
                message = random.choice(adhkar_list)
                
                logger.info(f"تم اختيار ذكر: {message[:50]}...")
                
                reply_message(event.reply_token, message)
                sent, failed = broadcast_text(message, exclude_user=user_id, exclude_group=gid)
                
                logger.info(f"تم تنفيذ أمر ذكرني من {user_id}")
                logger.info(f"تم الإرسال إلى: {sent} - فشل: {failed}")
                
            except Exception as e:
                logger.error(f"خطأ في أمر ذكرني: {e}", exc_info=True)
                reply_message(event.reply_token, "حدث خطأ، حاول مرة أخرى")
            return

    except Exception as e:
        logger.error(f"خطأ في معالجة الرسالة: {e}", exc_info=True)

@app.route("/", methods=["GET"])
def home():
    """الصفحة الرئيسية"""
    return jsonify({
        "status": "running",
        "bot": "بوت85",
        "creator": "عبير الدوسري",
        "version": "2.3",
        "users": len(target_users),
        "groups": len(target_groups),
        "notifications_disabled": len(notifications_off),
        "auto_reminder": AUTO_REMINDER_ENABLED,
        "keep_alive": bool(HEROKU_URL),
        "ramadan_duaa_count": len(ramadan_duaa_list)
    }), 200

@app.route("/health", methods=["GET"])
def health():
    """فحص صحة البوت"""
    return jsonify({
        "status": "healthy",
        "users": len(target_users),
        "groups": len(target_groups),
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/callback", methods=["POST"])
def callback():
    """معالج webhook من LINE"""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("توقيع غير صالح")
        return "Invalid signature", 400
    except Exception as e:
        logger.error(f"خطأ في webhook: {e}", exc_info=True)
    
    return "OK", 200

@app.route("/stats", methods=["GET"])
def stats():
    """عرض الإحصائيات"""
    total_tasbih = sum(sum(counts.values()) for counts in tasbih_counts.values())
    active_receivers = len(target_users) + len(target_groups) - len(notifications_off)
    
    return jsonify({
        "bot_name": "بوت85",
        "creator": "عبير الدوسري",
        "total_users": len(target_users),
        "total_groups": len(target_groups),
        "total_tasbih_count": total_tasbih,
        "active_users": len(tasbih_counts),
        "notifications_disabled": len(notifications_off),
        "active_receivers": active_receivers,
        "auto_reminder_enabled": AUTO_REMINDER_ENABLED,
        "keep_alive_enabled": bool(HEROKU_URL),
        "reminder_interval": f"{MIN_INTERVAL_HOURS}-{MAX_INTERVAL_HOURS} hours",
        "adhkar_count": len(content.get("adhkar", [])),
        "fadl_count": len(fadl_content),
        "ramadan_duaa_count": len(ramadan_duaa_list)
    }), 200

@app.route("/test_reminder", methods=["GET"])
def test_reminder():
    """اختبار التذكير اليدوي"""
    try:
        if fadl_content:
            message = random.choice(fadl_content)
            sent, failed = broadcast_text(message)
            return jsonify({
                "status": "success",
                "message": message[:100] + "..." if len(message) > 100 else message,
                "sent": sent,
                "failed": failed,
                "disabled_count": len(notifications_off)
            }), 200
        else:
            return jsonify({"status": "error", "message": "no fadl available in fadl.json"}), 400
    except Exception as e:
        logger.error(f"خطأ في اختبار التذكير: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("بوت85 - تم إنشاؤه بواسطة عبير الدوسري")
    logger.info("=" * 50)
    logger.info(f"المنفذ: {PORT}")
    logger.info(f"المستخدمون: {len(target_users)}")
    logger.info(f"المجموعات: {len(target_groups)}")
    logger.info(f"التذكير موقف لـ: {len(notifications_off)}")
    logger.info(f"محتوى الأذكار: {len(content.get('adhkar', []))}")
    logger.info(f"محتوى الفضل: {len(fadl_content)}")
    logger.info(f"أدعية رمضان: {len(ramadan_duaa_list)}")
    logger.info(f"التذكير التلقائي: {'مفعل' if AUTO_REMINDER_ENABLED else 'معطل'}")
    logger.info(f"Keep-Alive: {'مفعل ✓' if HEROKU_URL else 'معطل ✗'}")
    logger.info(f"فترة التذكير: {MIN_INTERVAL_HOURS}-{MAX_INTERVAL_HOURS} ساعة (أوقات متفرقة)")
    logger.info("مصدر التذكير اليدوي (ذكرني): content.json (أذكار)")
    logger.info("مصدر التذكير التلقائي: fadl.json (فضائل)")
    logger.info("مصدر أمر رمضان: ramadan.json (أدعية رمضان)")
    logger.info("=" * 50)
    app.run(host="0.0.0.0", port=PORT)
