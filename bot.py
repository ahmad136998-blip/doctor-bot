"""Telegram bot: photo archive of patient files for a doctor."""
import logging
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageOps
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs containing the token
log = logging.getLogger("doctor-bot")

ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(",", " ").split()}
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
FILES_DIR = DATA_DIR / "files"
MAX_SIDE = 2400  # px, longest side of a stored image
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram bot download limit

BTN_NEW = "➕ بیمار جدید"
BTN_SEARCH = "🔍 جستجو"
MENU = ReplyKeyboardMarkup([[BTN_NEW, BTN_SEARCH]], resize_keyboard=True)
KINDS = {"visit": "ویزیت", "lab": "آزمایش", "rx": "نسخه", "other": "سایر"}


def kb(*rows):
    return InlineKeyboardMarkup([[Btn(t, callback_data=d) for t, d in row] for row in rows])


def allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED)


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    text = f"⛔ دسترسی ندارید.\nشناسه شما: {uid}"
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


def card_text(p) -> str:
    docs = db.list_docs(p["id"])
    counts = {}
    for d in docs:
        counts[d["kind"]] = counts.get(d["kind"], 0) + 1
    parts = "، ".join(f"{KINDS.get(k, k)}: {n}" for k, n in counts.items()) or "بدون عکس"
    last = docs[-1]["created_at"][:10] if docs else "—"
    return (
        f"👤 {p['name']}\n"
        f"🗂 شماره پرونده: {p['id']}\n"
        f"📞 موبایل: {p['phone'] or '—'}\n"
        f"🖼 {parts}\n"
        f"🕒 آخرین ثبت: {last}"
    )


async def send_card(message, pid: int) -> None:
    p = db.get_patient(pid)
    if not p:
        await message.reply_text("این بیمار پیدا نشد.")
        return
    await message.reply_text(
        card_text(p),
        reply_markup=kb(
            [("📷 افزودن عکس", f"add:{pid}")],
            [("📂 نمایش پرونده", f"show:{pid}"), ("📄 PDF", f"pdf:{pid}")],
            [("✏️ ویرایش", f"edit:{pid}"), ("🗑 حذف بیمار", f"del:{pid}")],
        ),
    )


def remove_files(paths) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", p)


def to_jpeg(src: Path, dest: Path) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=88)


def build_pdf(paths: list[str], dest: Path) -> None:
    pages = []
    for p in paths:
        with Image.open(p) as im:
            pages.append(im.convert("RGB"))
    pages[0].save(dest, "PDF", save_all=True, append_images=pages[1:], resolution=150)


# ---------- handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    ctx.user_data.clear()
    await update.message.reply_text(
        "سلام.\n"
        "برای پیدا کردن بیمار، نام، موبایل یا شماره پرونده را بفرستید.\n"
        "برای بیمار تازه «بیمار جدید» را بزنید.",
        reply_markup=MENU,
    )


async def create_patient(message, ctx, phone: str) -> None:
    pid = db.add_patient(ctx.user_data["name"], phone)
    ctx.user_data.clear()
    await message.reply_text("✅ بیمار ثبت شد.")
    await send_card(message, pid)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    msg, ud = update.message, ctx.user_data
    text = msg.text.strip()
    state = ud.get("state")

    if text == BTN_NEW:
        ud.clear()
        ud["state"] = "new_name"
        return await msg.reply_text("نام و نام خانوادگی بیمار؟")
    if text == BTN_SEARCH:
        ud.clear()
        return await msg.reply_text("نام، موبایل یا شماره پرونده را بفرستید.")

    if state == "new_name":
        ud["name"] = text
        ud["state"] = "new_phone"
        dup = db.same_name(text)
        if dup:
            await msg.reply_text(
                "⚠️ بیمار هم‌نام از قبل وجود دارد. اگر همان است، انتخابش کنید:",
                reply_markup=kb(*[[(f"{p['name']} — #{p['id']} {p['phone']}", f"p:{p['id']}")] for p in dup]),
            )
        return await msg.reply_text(
            "شماره موبایل بیمار؟", reply_markup=kb([("بدون شماره", "nophone")])
        )
    if state in ("new_phone", "edit_phone"):
        phone = db.digits(text)
        if not 7 <= len(phone) <= 15:
            return await msg.reply_text("شماره درست نیست. دوباره بفرستید.")
        if state == "new_phone":
            return await create_patient(msg, ctx, phone)
        pid = ud["edit_pid"]
        ud.clear()
        db.update_patient(pid, phone=phone)
        return await send_card(msg, pid)
    if state == "edit_name":
        pid = ud["edit_pid"]
        ud.clear()
        db.update_patient(pid, name=text)
        return await send_card(msg, pid)

    # default: search (also leaves upload mode, to avoid filing under the wrong patient)
    ud.clear()
    rows = db.search(text)
    if not rows:
        return await msg.reply_text("چیزی پیدا نشد.", reply_markup=MENU)
    if len(rows) == 1:
        return await send_card(msg, rows[0]["id"])
    await msg.reply_text(
        f"{len(rows)} نتیجه:",
        reply_markup=kb(*[[(f"{p['name']} — #{p['id']} {p['phone']}", f"p:{p['id']}")] for p in rows]),
    )


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    msg = update.message
    act = ctx.user_data.get("active")
    p = db.get_patient(act["pid"]) if act else None
    if not p:
        ctx.user_data.pop("active", None)
        return await msg.reply_text(
            "ذخیره نشد. اول بیمار را پیدا کنید و «افزودن عکس» را بزنید."
        )
    src = msg.photo[-1] if msg.photo else msg.document
    if (src.file_size or 0) > MAX_DOWNLOAD:
        return await msg.reply_text("حجم فایل بیشتر از ۲۰ مگابایت است. به‌صورت عکس بفرستید.")

    pid, kind = p["id"], act["kind"]
    dest = FILES_DIR / str(pid) / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.jpg"
    tmp = DATA_DIR / f"tmp-{uuid.uuid4().hex}"
    try:
        tg_file = await src.get_file()
        await tg_file.download_to_drive(tmp)
        to_jpeg(tmp, dest)
    except Exception:
        log.exception("saving photo failed")
        return await msg.reply_text("❌ ذخیره نشد. دوباره بفرستید.")
    finally:
        tmp.unlink(missing_ok=True)

    doc_id = db.add_doc(pid, kind, str(dest))
    await msg.reply_text(
        f"✅ {KINDS[kind]} ذخیره شد — {p['name']} (#{pid})",
        reply_to_message_id=msg.message_id,
        reply_markup=kb([("🗑 حذف این عکس", f"dd:{doc_id}"), ("پایان", f"done:{pid}")]),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    q, ud = update.callback_query, ctx.user_data
    msg = q.message
    action, _, arg = q.data.partition(":")
    await q.answer()

    if action == "nophone":
        if ud.get("state") == "new_phone":
            await create_patient(msg, ctx, "")
        return

    if action == "dd":
        path = db.delete_doc(int(arg))
        if path:
            remove_files([path])
        return await q.edit_message_text("🗑 حذف شد." if path else "قبلاً حذف شده.")

    if action == "k":
        pid_s, kind = arg.split(":")
        pid = int(pid_s)
    else:
        pid = int(arg)
    p = db.get_patient(pid)
    if not p:
        return await msg.reply_text("این بیمار پیدا نشد.")

    if action == "p":
        ud.clear()
        await send_card(msg, pid)
    elif action == "add":
        await msg.reply_text(
            f"نوع برگه برای {p['name']}؟",
            reply_markup=kb(
                [(KINDS["visit"], f"k:{pid}:visit"), (KINDS["lab"], f"k:{pid}:lab")],
                [(KINDS["rx"], f"k:{pid}:rx"), (KINDS["other"], f"k:{pid}:other")],
            ),
        )
    elif action == "k":
        if kind not in KINDS:
            return
        ud.clear()
        ud["active"] = {"pid": pid, "kind": kind}
        await msg.reply_text(
            f"📷 عکس‌های «{KINDS[kind]}» را بفرستید.\nبیمار: {p['name']} (#{pid})",
            reply_markup=kb([("پایان", f"done:{pid}")]),
        )
    elif action == "done":
        ud.clear()
        await send_card(msg, pid)
    elif action == "show":
        docs = db.list_docs(pid)
        docs = [d for d in docs if Path(d["path"]).exists()]
        if not docs:
            return await msg.reply_text("این پرونده عکسی ندارد.")
        for i in range(0, len(docs), 10):
            chunk = docs[i : i + 10]
            media = [
                InputMediaPhoto(
                    Path(d["path"]).read_bytes(),
                    caption=f"{KINDS.get(d['kind'], d['kind'])} — {d['created_at'][:16]}",
                )
                for d in chunk
            ]
            if len(media) == 1:
                await msg.reply_photo(media[0].media, caption=media[0].caption)
            else:
                await msg.reply_media_group(media)
    elif action == "pdf":
        paths = [d["path"] for d in db.list_docs(pid) if Path(d["path"]).exists()]
        if not paths:
            return await msg.reply_text("این پرونده عکسی ندارد.")
        out = DATA_DIR / f"tmp-{uuid.uuid4().hex}.pdf"
        try:
            build_pdf(paths, out)
            await msg.reply_document(
                out.read_bytes(), filename=f"patient-{pid}.pdf", caption=f"{p['name']} — {len(paths)} صفحه"
            )
        finally:
            out.unlink(missing_ok=True)
    elif action == "edit":
        await msg.reply_text(
            "کدام را تغییر می‌دهید؟",
            reply_markup=kb([("نام", f"en:{pid}"), ("موبایل", f"ep:{pid}")]),
        )
    elif action in ("en", "ep"):
        ud.clear()
        ud["state"] = "edit_name" if action == "en" else "edit_phone"
        ud["edit_pid"] = pid
        await msg.reply_text("نام جدید؟" if action == "en" else "شماره موبایل جدید؟")
    elif action == "del":
        n = len(db.list_docs(pid))
        await msg.reply_text(
            f"⚠️ {p['name']} (#{pid}) با {n} عکس برای همیشه حذف شود؟",
            reply_markup=kb([("بله، حذف شود", f"delok:{pid}"), ("انصراف", f"p:{pid}")]),
        )
    elif action == "delok":
        ud.clear()
        remove_files(db.delete_patient(pid))
        await msg.reply_text("🗑 بیمار و پرونده‌اش حذف شد.", reply_markup=MENU)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ خطا رخ داد. دوباره امتحان کنید.")


def build_app(token: str, request=None) -> Application:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init(str(DATA_DIR / "patients.db"))
    b = Application.builder().token(token)
    if request is not None:
        b = b.request(request).get_updates_request(request)
    app = b.build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "cancel"], cmd_start, filters=private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


if __name__ == "__main__":
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED:
        log.warning("ALLOWED_USER_IDS is empty: nobody can use the bot. Send /start to see your ID.")
    build_app(token).run_polling(allowed_updates=["message", "callback_query"])
