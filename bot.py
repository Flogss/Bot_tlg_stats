"""Bot Telegram de cagnotte : /top <montant> pour ajouter, /cagnotte pour voir le classement en image."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
    Update,
    User,
)
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

from card import Entry, format_eur, render, warmup

load_dotenv()

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN manquant : ajoute-le dans les variables d'environnement (ou dans .env).")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8925708293"))
# Sur Railway, si un volume est monté, on y stocke les données pour qu'elles survivent aux redéploiements.
_default_dir = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(_default_dir, "data.json"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cagnotte")

PUBLIC_COMMANDS = [
    BotCommand("top", "Ajouter un montant : /top 50"),
    BotCommand("cagnotte", "Voir la cagnotte et le classement"),
    BotCommand("aide", "Afficher l'aide"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("solde", "Admin : fixer le solde d'un utilisateur"),
    BotCommand("ajouter", "Admin : ajouter/retirer un montant à un utilisateur"),
    BotCommand("supprimer", "Admin : retirer un utilisateur du top"),
    BotCommand("reset", "Admin : remettre la cagnotte à zéro"),
]

_lock = asyncio.Lock()
_admin_scoped_chats: set[int] = set()


# ---------- Stockage ----------

def load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def display_name(user: User) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


def upsert_user(data: dict, user: User) -> dict:
    rec = data["users"].setdefault(str(user.id), {"amount": 0.0})
    rec["name"] = display_name(user)
    rec["username"] = user.username
    return rec


# ---------- Utilitaires ----------

def parse_amount(text: str) -> float | None:
    cleaned = re.sub(r"[€\s]", "", text).replace(",", ".")
    try:
        value = round(float(cleaned), 2)
    except ValueError:
        return None
    if value != value or abs(value) > 1e9:  # NaN / valeurs absurdes
        return None
    return value


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def resolve_target(update: Update, data: dict, args: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Trouve l'utilisateur ciblé : réponse à un message, @username ou ID numérique.

    Retourne (user_id, nom, arguments restants).
    """
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        target = msg.reply_to_message.from_user
        upsert_user(data, target)
        return str(target.id), display_name(target), args

    if not args:
        return None, None, args
    ref, rest = args[0], args[1:]

    if ref.startswith("@"):
        wanted = ref[1:].lower()
        for uid, rec in data["users"].items():
            if (rec.get("username") or "").lower() == wanted:
                return uid, rec.get("name", ref), rest
        return None, None, rest

    if ref.lstrip("-").isdigit():
        uid = ref
        if uid not in data["users"]:
            name = uid
            try:
                chat = await update.get_bot().get_chat(int(uid))
                name = chat.full_name or chat.username or uid
            except Exception:
                pass
            data["users"][uid] = {"amount": 0.0, "name": name, "username": None}
        return uid, data["users"][uid].get("name", uid), rest

    return None, None, rest


AVATAR_TTL = 30 * 60  # on garde les photos de profil 30 min en mémoire
_avatar_cache: dict[int, tuple[float, bytes | None]] = {}


async def fetch_avatar(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bytes | None:
    cached = _avatar_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < AVATAR_TTL:
        return cached[1]
    avatar = None
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count:
            # taille moyenne (~320px) : largement suffisant pour l'image, plus rapide à télécharger
            sizes = photos.photos[0]
            photo = next((p for p in sizes if p.width >= 300), sizes[-1])
            file = await photo.get_file()
            avatar = bytes(await file.download_as_bytearray())
    except Exception as e:
        log.info("Pas d'avatar pour %s : %s", user_id, e)
    _avatar_cache[user_id] = (time.monotonic(), avatar)
    return avatar


async def build_card(context: ContextTypes.DEFAULT_TYPE, data: dict) -> tuple[bytes, float, list]:
    users = [(uid, u) for uid, u in data["users"].items() if u.get("amount", 0) != 0]
    users.sort(key=lambda x: x[1]["amount"], reverse=True)
    total = sum(u["amount"] for _, u in users)
    avatars = await asyncio.gather(*(fetch_avatar(context, int(uid)) for uid, _ in users[:10]))
    entries = [
        Entry(u.get("name", uid), u["amount"], avatars[i] if i < len(avatars) else None)
        for i, (uid, u) in enumerate(users)
    ]
    image = await asyncio.to_thread(render, entries, total)
    return image, total, users


async def ensure_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les commandes admin dans les suggestions de l'admin, y compris dans les groupes."""
    chat = update.effective_chat
    if not is_admin(update) or not chat or chat.id in _admin_scoped_chats:
        return
    if chat.type == ChatType.PRIVATE:
        scope = BotCommandScopeChat(chat.id)
    else:
        scope = BotCommandScopeChatMember(chat.id, ADMIN_ID)
    try:
        await context.bot.set_my_commands(ADMIN_COMMANDS, scope=scope)
        _admin_scoped_chats.add(chat.id)
    except Exception as e:
        log.warning("Impossible de définir le menu admin pour %s : %s", chat.id, e)


# ---------- Commandes publiques ----------

HELP_TEXT = (
    "💰 <b>Bot Cagnotte</b>\n\n"
    "/top <i>montant</i> — ajoute ton montant (ex : <code>/top 25,50</code>)\n"
    "/cagnotte — affiche la cagnotte et le classement\n"
)
ADMIN_HELP = (
    "\n🔐 <b>Admin</b> (en réponse à un message, ou avec @pseudo / ID) :\n"
    "/solde <i>@pseudo montant</i> — fixe le solde\n"
    "/ajouter <i>@pseudo montant</i> — ajoute (ou retire avec un négatif)\n"
    "/supprimer <i>@pseudo</i> — retire l'utilisateur du top\n"
    "/reset confirmer — remet la cagnotte à zéro\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    text = HELP_TEXT + (ADMIN_HELP if is_admin(update) else "")
    await update.effective_message.reply_html(text)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    msg = update.effective_message
    if not context.args:
        await msg.reply_html("Utilisation : <code>/top 50</code>")
        return
    amount = parse_amount("".join(context.args))
    if amount is None or amount <= 0:
        await msg.reply_text("❌ Montant invalide. Exemple : /top 25,50")
        return

    async with _lock:
        data = load()
        rec = upsert_user(data, update.effective_user)
        rec["amount"] = round(rec["amount"] + amount, 2)
        save(data)

    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    image, total, _ = await build_card(context, data)
    await msg.reply_photo(
        image,
        caption=f"✅ <b>+{format_eur(amount)}</b> pour {html.escape(rec['name'])} · "
                f"total : <b>{format_eur(rec['amount'])}</b>",
        parse_mode="HTML",
    )


async def cmd_cagnotte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    async with _lock:
        data = load()
    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    image, total, users = await build_card(context, data)

    caption = f"💰 Cagnotte : <b>{format_eur(total)}</b>"
    if users:
        caption += f"\n👑 En tête : <b>{html.escape(users[0][1].get('name', ''))}</b> ({format_eur(users[0][1]['amount'])})"
    await update.effective_message.reply_photo(image, caption=caption, parse_mode="HTML")


# ---------- Commandes admin ----------

async def _deny(update: Update) -> bool:
    if is_admin(update):
        return False
    await update.effective_message.reply_text("⛔ Seul l'administrateur peut utiliser cette commande.")
    return True


async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    if await _deny(update):
        return
    msg = update.effective_message
    async with _lock:
        data = load()
        uid, name, rest = await resolve_target(update, data, list(context.args))
        amount = parse_amount("".join(rest)) if rest else None
        if uid is None or amount is None:
            await msg.reply_html("Utilisation : <code>/solde @pseudo 120</code> (ou en réponse à un message : <code>/solde 120</code>)")
            return
        data["users"][uid]["amount"] = amount
        save(data)
    await msg.reply_html(f"✏️ Solde de <b>{html.escape(name)}</b> fixé à <b>{format_eur(amount)}</b>")


async def cmd_ajouter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    if await _deny(update):
        return
    msg = update.effective_message
    async with _lock:
        data = load()
        uid, name, rest = await resolve_target(update, data, list(context.args))
        amount = parse_amount("".join(rest)) if rest else None
        if uid is None or amount is None:
            await msg.reply_html("Utilisation : <code>/ajouter @pseudo 30</code> ou <code>/ajouter @pseudo -30</code>")
            return
        rec = data["users"][uid]
        rec["amount"] = round(rec.get("amount", 0) + amount, 2)
        save(data)
    sign = "+" if amount >= 0 else ""
    await msg.reply_html(f"✏️ {sign}{format_eur(amount)} pour <b>{html.escape(name)}</b> → <b>{format_eur(rec['amount'])}</b>")


async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    if await _deny(update):
        return
    msg = update.effective_message
    async with _lock:
        data = load()
        uid, name, _ = await resolve_target(update, data, list(context.args))
        if uid is None:
            await msg.reply_html("Utilisation : <code>/supprimer @pseudo</code> (ou en réponse à un message)")
            return
        data["users"].pop(uid, None)
        save(data)
    await msg.reply_html(f"🗑 <b>{html.escape(name)}</b> a été retiré du top.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin_menu(update, context)
    if await _deny(update):
        return
    if context.args != ["confirmer"]:
        await update.effective_message.reply_html("⚠️ Ceci efface toute la cagnotte. Tape <code>/reset confirmer</code> pour valider.")
        return
    async with _lock:
        save({"users": {}})
    await update.effective_message.reply_text("🔄 Cagnotte remise à zéro.")


# ---------- Lancement ----------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    try:
        await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(ADMIN_ID))
    except Exception as e:
        log.warning("Menu admin en privé non défini (l'admin doit d'abord démarrer le bot) : %s", e)
    await asyncio.to_thread(warmup)
    log.info("Bot démarré en tant que @%s", app.bot.username)


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "aide", "help"], cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("cagnotte", cmd_cagnotte))
    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("ajouter", cmd_ajouter))
    app.add_handler(CommandHandler("supprimer", cmd_supprimer))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
