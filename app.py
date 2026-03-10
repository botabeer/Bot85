from flask import Flask, request, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import os, random, json, logging, threading, time
from datetime import datetime, date

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SECRET       = os.getenv("LINE_CHANNEL_SECRET")
PORT         = int(os.getenv("PORT", 5000))

configuration = Configuration(access_token=ACCESS_TOKEN)
handler       = WebhookHandler(SECRET)

DATA_FILE    = "data.json"
CONTENT_FILE = "content.json"
RAMADAN_FILE = "ramadan.json"

data_lock = threading.Lock()

SALAM_WORDS = [
    "السلام عليكم",
    "السلام عليكم ورحمة الله",
    "السلام عليكم ورحمة الله وبركاته"
]

HELP_WORDS = {"مساعدة", "بداية", "ابدأ", "الاوامر", "الأوامر"}

VALID_COMMANDS = {
    "فضل", "تسبيح", "تسبيح مشترك", "مشترك", "ذكرني",
    "إعادة", "رمضان"
} | HELP_WORDS

TASBIH_LIMITS = 33
TASBIH_KEYS   = ["استغفر الله", "سبحان الله", "الحمد لله", "الله أكبر"]

def load_json(file, default):
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
    try:
        with data_lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "users"            : list(target_users),
                    "groups"           : list(target_groups),
                    "tasbih"           : tasbih_counts,
                    "last_reset"       : last_reset_dates,
                    "used_ramadan_duaa": {k: list(v) for k, v in used_ramadan_duaa.items()},
                    "tasbih_sessions"  : tasbih_sessions
                }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطأ في الحفظ: {e}")

data = load_json(DATA_FILE, {
    "users": [], "groups": [], "tasbih": {},
    "last_reset": {},
    "used_ramadan_duaa": {}, "tasbih_sessions": {}
})

target_users      = set(data.get("users", []))
target_groups     = set(data.get("groups", []))
tasbih_counts     = data.get("tasbih", {})
last_reset_dates  = data.get("last_reset", {})
used_ramadan_duaa = {k: set(v) for k, v in data.get("used_ramadan_duaa", {}).items()}
tasbih_sessions   = data.get("tasbih_sessions", {})

content           = load_json(CONTENT_FILE, {"adhkar": []})
fadl_content      = load_json("fadl.json", {"fadl": []}).get("fadl", [])
ramadan_duaa_list = load_json(RAMADAN_FILE, {"duaa": []}).get("duaa", [])

def send_message(target_id, text):
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
        return True
    except Exception as e:
        logger.error(f"فشل الإرسال إلى {target_id}: {e}")
        return False

def reply_message(reply_token, text):
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))
        return True
    except Exception as e:
        logger.error(f"فشل الرد: {e}")
        return False

def get_user_name(user_id):
    try:
        with ApiClient(configuration) as api_client:
            return MessagingApi(api_client).get_profile(user_id).display_name
    except:
        return "مستخدم"

def get_name(user_id, gid=None):
    if gid:
        try:
            with ApiClient(configuration) as api_client:
                return MessagingApi(api_client).get_group_member_profile(gid, user_id).display_name
        except:
            return "مستخدم"
    return get_user_name(user_id)

def get_ramadan_duaa(user_id):
    if not ramadan_duaa_list:
        return "لا توجد أدعية متاحة حالياً."
    if user_id not in used_ramadan_duaa:
        used_ramadan_duaa[user_id] = set()
    if len(used_ramadan_duaa[user_id]) >= len(ramadan_duaa_list):
        used_ramadan_duaa[user_id] = set()
    remaining = [d for i, d in enumerate(ramadan_duaa_list)
                 if i not in used_ramadan_duaa[user_id]]
    selected = random.choice(remaining)
    used_ramadan_duaa[user_id].add(ramadan_duaa_list.index(selected))
    save_data()
    return selected

def normalize_tasbih(text):
    t = (text.strip()
         .replace(" ", "")
         .replace("ٱ", "ا").replace("أ", "ا").replace("إ", "ا").replace("ة", "ه"))
    mapping = {
        "استغفرالله": "استغفر الله",
        "سبحانالله" : "سبحان الله",
        "الحمدلله"  : "الحمد لله",
        "اللهأكبر"  : "الله أكبر",
        "اللهاكبر"  : "الله أكبر",
    }
    return mapping.get(t)

def ensure_user_counts(uid):
    if uid not in tasbih_counts:
        tasbih_counts[uid] = {k: 0 for k in TASBIH_KEYS}
        last_reset_dates[uid] = str(date.today())
        save_data()

def reset_tasbih_if_needed(user_id):
    today = str(date.today())
    if last_reset_dates.get(user_id) != today:
        tasbih_counts[user_id] = {k: 0 for k in TASBIH_KEYS}
        last_reset_dates[user_id] = today
        save_data()
        return True
    return False

def reset_shared_sessions_if_needed():
    """تصفير الجلسات المشتركة يومياً — تبقى مفتوحة لكن تعود الأعداد لصفر"""
    today = str(date.today())
    changed = False
    for sid, session in tasbih_sessions.items():
        last = session.get("last_reset", "")
        if last != today:
            for uid in session.get("members", {}):
                session["members"][uid] = {k: 0 for k in TASBIH_KEYS}
            session["last_reset"] = today
            changed = True
    if changed:
        save_data()
        logger.info("تم تصفير الجلسات المشتركة ليوم جديد")

def solo_tasbih_status(user_id, gid=None):
    counts = tasbih_counts[user_id]
    name   = get_name(user_id, gid)
    lines  = [f"تسبيح {name}\n"]
    for key in TASBIH_KEYS:
        c = counts[key]
        lines.append(f"{key}: {c}/{TASBIH_LIMITS}")
    if all(counts[k] >= TASBIH_LIMITS for k in TASBIH_KEYS):
        lines.append("\nاكتمل التسبيح — جزاك الله خيراً ✓")
    return "\n".join(lines)

def find_shared_session(context_key, user_id):
    for sid, session in tasbih_sessions.items():
        if session.get("context") == context_key and user_id in session.get("members", {}):
            return sid, session
    return None, None

def find_open_session(context_key, user_id):
    for sid, session in tasbih_sessions.items():
        if (session.get("context") == context_key
                and session.get("open", True)
                and user_id not in session.get("members", {})):
            return sid, session
    return None, None

def shared_status_text(session, context_key):
    gid     = context_key if context_key.startswith("C") else None
    totals  = {k: 0 for k in TASBIH_KEYS}
    members = session.get("members", {})
    for uid, counts in members.items():
        for k in TASBIH_KEYS:
            totals[k] += counts[k]
    names    = [get_name(uid, gid) for uid in members]
    all_done = all(totals[k] >= TASBIH_LIMITS for k in TASBIH_KEYS)
    lines    = ["تسبيح مشترك\n"]
    lines.append("المشاركون: " + "، ".join(names) + "\n")
    for key in TASBIH_KEYS:
        t = totals[key]
        lines.append(f"{key}: {t}/{TASBIH_LIMITS}")
    if all_done:
        lines.append("\nاكتمل التسبيح — جزاكم الله خيراً ✓")
    return "\n".join(lines), all_done

def is_valid_command(text):
    t = text.strip()
    if t in VALID_COMMANDS:
        return True
    if normalize_tasbih(t):
        return True
    return False

HELP_TEXT = (
    "بوت 85\n"
    "أوامر البوت\n"
    "─────────────────\n"
    "تسبيح\n"
    "تسبيح فردي — يعدّ لك وحدك\n\n"
    "تسبيح مشترك\n"
    "تبدأ به جلسة مشتركة\n\n"
    "مشترك\n"
    "للانضمام لجلسة مشتركة قائمة\n\n"
    "إعادة\n"
    "تصفير عداد تسبيحك\n\n"
    "ذكرني\n"
    "إرسال ذكر أو دعاء\n\n"
    "فضل\n"
    "فضل من فضائل العبادات\n\n"
    "رمضان\n"
    "دعاء من أدعية رمضان\n\n"
    "─────────────────\n"
    "تم إنشاء هذا البوت بواسطة عبير الدوسري"
)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        user_text = event.message.text.strip()
        user_id   = event.source.user_id
        gid       = getattr(event.source, "group_id", None)
        context_key = gid if gid else user_id

        # السلام
        if user_text in SALAM_WORDS:
            if gid:
                target_groups.add(gid)
            else:
                target_users.add(user_id)
            save_data()
            reply_message(event.reply_token, "وعليكم السلام ورحمة الله وبركاته")
            return

        # تسجيل القروب أو المستخدم
        if gid:
            if gid not in target_groups:
                target_groups.add(gid)
                save_data()
        else:
            if user_id not in target_users:
                target_users.add(user_id)
                save_data()

        # البوت صامت على أي شيء غير الأوامر
        if not is_valid_command(user_text):
            return

        cmd = user_text.strip()

        # مساعدة
        if cmd in HELP_WORDS:
            reply_message(event.reply_token, HELP_TEXT)
            return

        # رمضان
        if cmd == "رمضان":
            reply_message(event.reply_token, get_ramadan_duaa(user_id))
            return

        # فضل
        if cmd == "فضل":
            msg = random.choice(fadl_content) if fadl_content else "لا يوجد محتوى متاح حالياً."
            reply_message(event.reply_token, msg)
            return


        # تسبيح فردي — عرض الحالة
        if cmd == "تسبيح":
            ensure_user_counts(user_id)
            was_reset = reset_tasbih_if_needed(user_id)
            status = solo_tasbih_status(user_id, gid)
            if was_reset:
                status = "تم تصفير العداد ليوم جديد.\n\n" + status
            reply_message(event.reply_token, status)
            return

        # تسبيح مشترك — بدء جلسة جديدة
        if cmd == "تسبيح مشترك":
            ensure_user_counts(user_id)
            reset_tasbih_if_needed(user_id)
            reset_shared_sessions_if_needed()
            sid, session = find_shared_session(context_key, user_id)
            if sid:
                status_text, _ = shared_status_text(session, context_key)
                reply_message(event.reply_token,
                    "أنت في جلسة مشتركة بالفعل.\n\n" + status_text)
                return
            sid = f"{context_key}_{user_id}_{int(time.time())}"
            tasbih_sessions[sid] = {
                "context": context_key,
                "open"   : True,
                "members": {user_id: {k: 0 for k in TASBIH_KEYS}},
                "started": str(datetime.now())
            }
            save_data()
            u_name = get_name(user_id, gid)
            msg = (
                f"بدأ {u_name} جلسة تسبيح مشترك.\n\n"
                "من يريد الانضمام يكتب: مشترك\n\n"
                "اكتب أي ذكر للبدء:\n"
                "استغفر الله  |  سبحان الله\n"
                "الحمد لله  |  الله أكبر"
            )
            reply_message(event.reply_token, msg)
            return

        # مشترك — انضمام لجلسة قائمة
        if cmd == "مشترك":
            ensure_user_counts(user_id)
            reset_tasbih_if_needed(user_id)
            reset_shared_sessions_if_needed()
            sid, session = find_shared_session(context_key, user_id)
            if sid:
                status_text, _ = shared_status_text(session, context_key)
                reply_message(event.reply_token,
                    "أنت في جلسة مشتركة بالفعل.\n\n" + status_text)
                return
            open_sid, open_session = find_open_session(context_key, user_id)
            if open_sid:
                open_session["members"][user_id] = {k: 0 for k in TASBIH_KEYS}
                save_data()
                names = [get_name(uid, gid) for uid in open_session["members"]]
                msg = (
                    f"انضم {get_name(user_id, gid)} للتسبيح المشترك.\n\n"
                    "المشاركون: " + "، ".join(names) + "\n\n"
                    "اكتب أي ذكر للبدء:\n"
                    "استغفر الله  |  سبحان الله\n"
                    "الحمد لله  |  الله أكبر"
                )
                reply_message(event.reply_token, msg)
            else:
                # إنشاء جلسة جديدة تلقائياً عند كتابة "مشترك"
                sid = f"{context_key}_{user_id}_{int(time.time())}"
                tasbih_sessions[sid] = {
                    "context": context_key,
                    "open"   : True,
                    "members": {user_id: {k: 0 for k in TASBIH_KEYS}},
                    "started": str(datetime.now())
                }
                save_data()
                u_name = get_name(user_id, gid)
                msg = (
                    f"بدأ {u_name} جلسة تسبيح مشترك.\n\n"
                    "من يريد الانضمام يكتب: مشترك\n\n"
                    "اكتب أي ذكر للبدء:\n"
                    "استغفر الله  |  سبحان الله\n"
                    "الحمد لله  |  الله أكبر"
                )
                reply_message(event.reply_token, msg)
            return

        # إعادة
        if cmd == "إعادة":
            ensure_user_counts(user_id)
            with data_lock:
                tasbih_counts[user_id] = {k: 0 for k in TASBIH_KEYS}
                last_reset_dates[user_id] = str(date.today())
                sid, session = find_shared_session(context_key, user_id)
                if sid:
                    session["members"].pop(user_id, None)
                    if not session["members"]:
                        del tasbih_sessions[sid]
            save_data()
            reply_message(event.reply_token,
                "تم تصفير عداد تسبيحك.\nيمكنك البدء من جديد.")
            return

        # ذكرني
        if cmd == "ذكرني":
            adhkar_list = content.get("adhkar", [])
            if not adhkar_list:
                reply_message(event.reply_token, "لا يوجد أذكار متاحة حالياً.")
                return
            message = random.choice(adhkar_list)
            reply_message(event.reply_token, message)
            def do_broadcast():
                sent = 0
                for g in list(target_groups):
                    if g != gid:
                        if send_message(g, message): sent += 1
                for uid in list(target_users):
                    if uid != user_id:
                        if send_message(uid, message): sent += 1
                logger.info(f"ذكرني: أُرسل إلى {sent}")
            threading.Thread(target=do_broadcast, daemon=True).start()
            return

        # كلمات التسبيح — فردي أو مشترك
        normalized = normalize_tasbih(user_text)
        if normalized:
            ensure_user_counts(user_id)
            reset_tasbih_if_needed(user_id)
            reset_shared_sessions_if_needed()
            sid, session = find_shared_session(context_key, user_id)

            if sid:
                # التسبيح المشترك
                with data_lock:
                    session["members"][user_id][normalized] += 1
                    tasbih_counts[user_id][normalized] = session["members"][user_id][normalized]
                save_data()
                total_this = sum(
                    session["members"][uid][normalized]
                    for uid in session["members"]
                )
                status_text, all_done = shared_status_text(session, context_key)
                if total_this > TASBIH_LIMITS:
                    reply_message(event.reply_token,
                        f"اكتمل {normalized} مسبقاً في هذه الجلسة.")
                elif total_this == TASBIH_LIMITS:
                    reply_message(event.reply_token,
                        f"اكتمل {normalized} — {total_this}/{TASBIH_LIMITS} ✓\n\n" + status_text)
                    if all_done:
                        names = [get_name(uid, gid) for uid in session["members"]]
                        final = (
                            "اكتمل التسبيح المشترك\n"
                            "─────────────────\n"
                            + "\n".join(f"✓ {n}" for n in names)
                            + "\n─────────────────\n"
                            "جزاكم الله خيراً وتقبّل منكم"
                        )
                        time.sleep(1)
                        send_message(context_key, final)
                        # تصفير الأعداد وإبقاء الجلسة مفتوحة ليوم جديد
                        for uid in session["members"]:
                            session["members"][uid] = {k: 0 for k in TASBIH_KEYS}
                        session["last_reset"] = str(date.today())
                        save_data()
                else:
                    reply_message(event.reply_token, status_text)
            else:
                # التسبيح الفردي
                with data_lock:
                    counts = tasbih_counts[user_id]
                    if counts[normalized] >= TASBIH_LIMITS:
                        reply_message(event.reply_token,
                            f"أكملت {normalized} مسبقاً.\nاستخدم: إعادة")
                        return
                    counts[normalized] += 1
                save_data()
                counts = tasbih_counts[user_id]
                if counts[normalized] == TASBIH_LIMITS:
                    reply_message(event.reply_token,
                        f"{get_name(user_id, gid)} أكمل {normalized} ✓")
                    if all(counts[k] >= TASBIH_LIMITS for k in TASBIH_KEYS):
                        time.sleep(1)
                        final = (
                            f"اكتمل تسبيح {get_name(user_id, gid)}\n"
                            "─────────────────\n"
                            + "\n".join(f"✓ {k}" for k in TASBIH_KEYS)
                            + "\n─────────────────\n"
                            "جزاك الله خيراً وتقبّل منك"
                        )
                        send_message(context_key, final)
                else:
                    reply_message(event.reply_token, solo_tasbih_status(user_id, gid))
            return

    except Exception as e:
        logger.error(f"خطأ في معالجة الرسالة: {e}", exc_info=True)


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running", "bot": "بوت85",
        "creator": "عبير الدوسري", "version": "4.0",
        "groups": len(target_groups), "users": len(target_users),
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy", "groups": len(target_groups),
        "users": len(target_users), "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
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
    total_tasbih = sum(sum(c.values()) for c in tasbih_counts.values())
    return jsonify({
        "bot_name": "بوت85", "creator": "عبير الدوسري",
        "total_groups": len(target_groups), "total_users": len(target_users),
        "total_tasbih": total_tasbih,
        "adhkar_count": len(content.get("adhkar", [])),
        "fadl_count": len(fadl_content),
        "ramadan_duaa_count": len(ramadan_duaa_list),
        "active_sessions": len(tasbih_sessions),
    }), 200

@app.route("/test_reminder", methods=["GET"])
def test_reminder():
    try:
        if fadl_content:
            message = random.choice(fadl_content)
            sent = sum(1 for g in list(target_groups)
                       if send_message(g, message))
            return jsonify({"status": "success", "preview": message[:120], "sent": sent}), 200
        return jsonify({"status": "error", "message": "fadl.json فارغ"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("بوت85 — عبير الدوسري  v4.0")
    logger.info(f"القروبات: {len(target_groups)}")
    logger.info(f"المستخدمون: {len(target_users)}")
    logger.info(f"الأذكار: {len(content.get('adhkar', []))}")
    logger.info(f"الفضائل: {len(fadl_content)}")
    logger.info(f"أدعية رمضان: {len(ramadan_duaa_list)}")
    logger.info("=" * 50)
    app.run(host="0.0.0.0", port=PORT)
