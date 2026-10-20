#!/usr/bin/env python3
import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message, MessageOriginChannel
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE = Path(__file__).parent
PREVIEW_DIR = BASE / "previews"
PREVIEW_DIR.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004443157249"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002666220955"))
DISCUSSION_CHAT_ID = int(os.getenv("DISCUSSION_CHAT_ID", "-1002666044964"))
VOTE_HOURS = float(os.getenv("VOTE_HOURS", "24"))
EARLY_LIKES = int(os.getenv("EARLY_LIKES", "3"))

DEFAULT_OWNER_ID = 7123446502

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mashup")

POST_TEMPLATE = (
    "<b>{level} - {song_author} - {song_name}</b>\n\n"
    "Рекомендуется наличие мода Jukebox!\n"
    "Мешап в комментариях\n"
    "#{hashtag}\n\n"
    "Mashup by {nick}\n\n"
    '<a href="https://t.me/gmdmashups">GMD Mashup</a>'
)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
r = Router()


def esc(x) -> str:
    return html.escape(str(x))


def is_zero_timecode(tc: str) -> bool:
    clean = re.sub(r"[^\d]", "", tc or "")
    return not clean or set(clean) == {"0"}


def make_hashtag(author: str) -> str:
    clean = re.sub(r"[^\w\s]", "", author or "")
    return "".join(clean.split()) if clean else "mashup"


# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(BASE / "mashup.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT DEFAULT '',
    is_owner INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS subs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_name TEXT, file_id TEXT, file_type TEXT DEFAULT 'document',
    level TEXT, song_author TEXT, song_name TEXT, timecode TEXT, nick TEXT,
    status TEXT DEFAULT 'pending', created INTEGER, ends INTEGER,
    admin_msg INTEGER, chan_msg INTEGER, thread_msg INTEGER, gd_id TEXT,
    first_voter_id INTEGER, final_likes INTEGER DEFAULT 0, final_dislikes INTEGER DEFAULT 0, stats_posted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS votes(
    sub_id INTEGER, user_id INTEGER, vote INTEGER, PRIMARY KEY(sub_id, user_id));
"""
)
for col, def_val in [
    ("gd_id", "TEXT"),
    ("first_voter_id", "INTEGER"),
    ("final_likes", "INTEGER DEFAULT 0"),
    ("final_dislikes", "INTEGER DEFAULT 0"),
    ("stats_posted", "INTEGER DEFAULT 0"),
]:
    try:
        db.execute(f"ALTER TABLE subs ADD COLUMN {col} {def_val}")
    except sqlite3.OperationalError:
        pass
db.commit()


def q(sql, args=()):
    return db.execute(sql, args).fetchall()


def q1(sql, args=()):
    return db.execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def is_owner(uid: int) -> bool:
    u = q1("SELECT is_owner FROM users WHERE id=?", (uid,))
    return bool(u and u["is_owner"])


def is_admin(uid: int) -> bool:
    u = q1("SELECT is_admin, is_owner FROM users WHERE id=?", (uid,))
    return bool(u and (u["is_admin"] or u["is_owner"]))


def is_banned(uid: int) -> bool:
    u = q1("SELECT is_banned FROM users WHERE id=?", (uid,))
    return bool(u and u["is_banned"])


def set_flag(uid: int, flag: str, value: int):
    assert flag in ("is_owner", "is_admin", "is_banned")
    ex(f"INSERT INTO users(id, {flag}) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET {flag}=excluded.{flag}", (uid, value))


def vote_counts(sid: int):
    v = q1("SELECT COALESCE(SUM(vote=1),0) l, COALESCE(SUM(vote=0),0) d FROM votes WHERE sub_id=?", (sid,))
    return v["l"], v["d"]


def total_admins_count() -> int:
    res = q1("SELECT COUNT(*) c FROM users WHERE is_admin=1 OR is_owner=1")
    return res["c"] if res else 0


async def sync_group_admins():
    if not ADMIN_CHAT_ID:
        return
    try:
        admins = await bot.get_chat_administrators(ADMIN_CHAT_ID)
        current_admin_ids = set()
        for member in admins:
            user = member.user
            if user.is_bot:
                continue
            current_admin_ids.add(user.id)
            
            # Сохраняем или обновляем пользователя в базе. 
            # Берем актуальный full_name, а если он пустой — ставим fallback на user.first_name или ID
            name_to_save = user.full_name or user.first_name or str(user.id)
            
            ex(
                "INSERT INTO users(id, name, is_admin) VALUES(?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET is_admin=1, name=COALESCE(NULLIF(excluded.name, ''), users.name)",
                (user.id, name_to_save)
            )
        
        # Сбрасываем админку тем, кого сняли с админке в чате (кроме владельца бота)
        db.execute(
            "UPDATE users SET is_admin=0 WHERE is_admin=1 AND is_owner=0 AND id NOT IN ({})".format(
                ",".join("?" * len(current_admin_ids)) if current_admin_ids else "0"
            ),
            tuple(current_admin_ids) if current_admin_ids else ()
        )
        db.commit()
    except Exception:
        log.exception("Не удалось синхронизировать админов из чата")


def safe_name(n: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", n).strip().lower()


def find_preview(level: str):
    key = safe_name(level)
    for p in PREVIEW_DIR.iterdir():
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.stem.lower() == key:
            return p
    return None


def info(s) -> str:
    gd = f"ID GD: {esc(s['gd_id'])}\n" if s["gd_id"] else ""
    return (f"<b>#{s['id']} * {esc(s['level'])}</b>\n" + gd +
            f"{esc(s['song_author'])} - {esc(s['song_name'])}\n"
            f"Таймкод старта: {esc(s['timecode'])}\n"
            f"Мешапер: {esc(s['nick'])}\n"
            f"Отправил: {esc(s['user_name'])}")


def vote_kb(sid: int, likes: int, dislikes: int, banned: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"+ {likes}", callback_data=f"v:{sid}:1")
    b.button(text=f"- {dislikes}", callback_data=f"v:{sid}:0")
    if banned:
        b.button(text="Разблокировать", callback_data=f"unban_cb:{sid}")
    else:
        b.button(text="Заблокировать", callback_data=f"ban:{sid}")
    b.adjust(2, 1)
    return b.as_markup()


def back_kb(cancel_text="Отмена / Начать заново") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=cancel_text, callback_data="wizard_cancel")
    return b.as_markup()


async def publish(s):
    hashtag = make_hashtag(s["song_author"])
    text = POST_TEMPLATE.format(
        level=esc(s["level"]),
        song_author=esc(s["song_author"]),
        song_name=esc(s["song_name"]),
        hashtag=esc(hashtag),
        nick=esc(s["nick"])
    )
    prev = find_preview(s["level"])
    if prev:
        msg = await bot.send_photo(CHANNEL_ID, FSInputFile(prev), caption=text)
    else:
        msg = await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
    ex("UPDATE subs SET chan_msg=? WHERE id=?", (msg.message_id, s["id"]))


async def finalize(sid: int, force_accept: bool = None):
    s = q1("SELECT * FROM subs WHERE id=?", (sid,))
    if not s or s["status"] != "pending":
        return
    likes, dislikes = vote_counts(sid)
    total_admins = max(total_admins_count(), 1)
    voted_total = likes + dislikes

    if force_accept is not None:
        ok = force_accept
    else:
        if likes >= EARLY_LIKES and likes > dislikes:
            ok = True
        elif dislikes >= 3 and dislikes > likes:
            ok = False
        else:
            remaining = total_admins - voted_total
            if likes > dislikes + remaining:
                ok = True
            elif dislikes > likes + remaining:
                ok = False
            else:
                ok = likes > dislikes

    cur = ex("UPDATE subs SET status=? WHERE id=? AND status='pending'", ("accepted" if ok else "rejected", sid))
    if cur.rowcount == 0:
        return
    
    if ok:
        ex("UPDATE subs SET final_likes=?, final_dislikes=?, stats_posted=1 WHERE id=?", (likes, dislikes, sid))

    s = q1("SELECT * FROM subs WHERE id=?", (sid,))
    verdict = "Принят досрочно" if force_accept is True else ("Отклонен досрочно" if force_accept is False else ("Принят" if ok else "Отклонен"))

    try:
        await bot.edit_message_caption(chat_id=ADMIN_CHAT_ID, message_id=s["admin_msg"],
                                       caption=f"{info(s)}\n\n{verdict} ({likes}+ / {dislikes}-)", reply_markup=None)
    except TelegramBadRequest as e:
        log.warning("admin msg edit: %s", e)

    if ok:
        try:
            await publish(s)
        except Exception as e:
            log.exception("publish failed")
            ex("UPDATE subs SET status='publish_error' WHERE id=?", (sid,))
            await bot.send_message(ADMIN_CHAT_ID, f"#{sid}: не удалось выложить в канал: {esc(e)}")
            return
        await notify(s["user_id"], f"Твой мешап {esc(s['level'])} прошел и выложен в тгк!")
    else:
        await notify(s["user_id"], f"Твой мешап {esc(s['level'])} не прошел в тгк.")


async def notify(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


async def watcher():
    while True:
        try:
            await sync_group_admins()
            now = int(time.time())

            for x in q("SELECT id, created FROM subs WHERE status='pending'"):
                sid = x["id"]
                likes, dislikes = vote_counts(sid)
                
                if now - x["created"] >= VOTE_HOURS * 3600 and (likes + dislikes) == 0:
                    ex("UPDATE subs SET status='timeout_rejected' WHERE id=? AND status='pending'", (sid,))
                    s = q1("SELECT * FROM subs WHERE id=?", (sid,))
                    try:
                        await bot.edit_message_caption(chat_id=ADMIN_CHAT_ID, message_id=s["admin_msg"],
                                                       caption=f"{info(s)}\n\nОтклонено по таймауту (нет голосов за {VOTE_HOURS}ч)", reply_markup=None)
                    except Exception:
                        pass
                    await notify(s["user_id"], f"Твой мешап {esc(s['level'])} не получил голосов за {VOTE_HOURS} ч и был отклонен.")
                    continue

                s_full = q1("SELECT ends FROM subs WHERE id=?", (sid,))
                if s_full and s_full["ends"] <= now:
                    await finalize(sid)
        except Exception:
            log.exception("watcher")
        await asyncio.sleep(30)


class NameMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u:
            ex("UPDATE users SET name=? WHERE id=?", (u.full_name, u.id))
        return await handler(event, data)


@r.message(F.chat.id == DISCUSSION_CHAT_ID, F.is_automatic_forward)
async def on_channel_post_in_comments(m: Message):
    fo = m.forward_origin
    if not isinstance(fo, MessageOriginChannel) or fo.chat.id != CHANNEL_ID:
        return
    s = q1("SELECT * FROM subs WHERE chan_msg=?", (fo.message_id,))
    if not s or s["thread_msg"]:
        return

    caption = "" if is_zero_timecode(s["timecode"]) else f"{esc(s['timecode'])}"

    if s["file_type"] == "audio":
        file_msg = await bot.send_audio(m.chat.id, s["file_id"], reply_to_message_id=m.message_id, caption=caption)
    else:
        file_msg = await bot.send_document(m.chat.id, s["file_id"], reply_to_message_id=m.message_id, caption=caption)

    ex("UPDATE subs SET thread_msg=? WHERE id=?", (file_msg.message_id, s["id"]))


HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)


async def search_music(query: str):
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as ses:
            async with ses.get("https://api.deezer.com/search", params={"q": query, "limit": 8}) as resp:
                j = await resp.json(content_type=None)
        out, seen = [], set()
        for t in j.get("data", []):
            a, n = t["artist"]["name"], t["title"]
            if (a.lower(), n.lower()) in seen:
                continue
            seen.add((a.lower(), n.lower()))
            out.append({"author": a, "title": n})
        return out[:6]
    except Exception:
        log.exception("music search")
        return None


async def _gd_browser(query: str):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers={"User-Agent": "mashup-bot"}) as ses:
        url = f"https://gdbrowser.com/api/level/{query}" if query.isdigit() else f"https://gdbrowser.com/api/search/{quote(query, safe='')}?count=8"
        async with ses.get(url) as resp:
            j = await resp.json(content_type=None)
    if j == -1 or j == "-1" or not j:
        return []
    if isinstance(j, dict):
        j = [j]
    return [{"id": str(x["id"]), "name": x["name"], "author": x.get("author", "?")} for x in j[:8]]


async def _gd_boomlings(query: str):
    data = {"str": query, "type": 0, "page": 0, "secret": "Wmfd2893gb7"}
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, skip_auto_headers=["User-Agent"]) as ses:
        async with ses.post("https://www.boomlings.com/database/getGJLevels21.php", data=data) as resp:
            text = await resp.text()
    if not text or text.startswith("-1"):
        return []
    parts = text.split("#")
    creators = {}
    for c in (parts[1].split("|") if len(parts) > 1 else []):
        f = c.split(":")
        if len(f) >= 2:
            creators[f[0]] = f[1]
    out = []
    for lv in parts[0].split("|")[:8]:
        f = lv.split(":")
        d = dict(zip(f[0::2], f[1::2]))
        if "1" in d and "2" in d:
            out.append({"id": d["1"], "name": d["2"], "author": creators.get(d.get("6"), "?")})
    return out


async def search_levels(query: str):
    for fn in (_gd_browser, _gd_boomlings):
        try:
            return await fn(query)
        except Exception:
            log.warning("GD search failed", exc_info=True)
    return None


def short(t: str, n: int = 55) -> str:
    return t if len(t) <= n else t[: n - 1] + "."


class Form(StatesGroup):
    file = State()
    level = State()
    level_pick = State()
    song_author_input = State()
    song_name_input = State()
    song_pick = State()
    timecode = State()
    nick = State()


TIMECODE_RE = re.compile(r"^\d{1,2}:[0-5]\d(:[0-5]\d)?(\.\d{1,3})?$")


@r.message(CommandStart(), F.chat.type.in_({"private", "supergroup", "group"}))
async def cmd_start(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    await state.clear()
    await state.set_state(Form.file)
    await m.answer("Привет! Давай отправим мешап на проверку для ТГК.\n\n"
                   "1/5. Скинь файл мешапа (как документ или аудио).\nОтмена - /cancel")


@r.message(Command("cancel"), F.chat.type.in_({"private", "supergroup", "group"}))
async def cmd_cancel(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    await state.clear()
    await m.answer("Отменено. Чтобы начать заново - /start")


@r.callback_query(F.data == "wizard_cancel")
async def cb_wizard_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено. Отправь /start, чтобы начать заново.")
    await cb.answer()


@r.message(Form.file, F.document | F.audio)
async def f_file(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    file_id = m.audio.file_id if m.audio else m.document.file_id
    file_type = "audio" if m.audio else "document"
        
    await state.update_data(file_id=file_id, file_type=file_type)
    await state.set_state(Form.level)
    await m.answer("2/5. Название уровня или его ID - найду в Geometry Dash:", reply_markup=back_kb("Отмена"))


@r.message(StateFilter(Form.level, Form.level_pick), F.text, ~F.text.startswith("/"))
async def f_level_search(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    query = m.text.strip()[:100]
    wait = await m.answer("Ищу в Geometry Dash...")
    res = await search_levels(query)
    await state.update_data(level_q=query, level_res=res or [])
    await state.set_state(Form.level_pick)
    b = InlineKeyboardBuilder()
    for i, lv in enumerate(res or []):
        b.button(text=short(f"{lv['name']} - {lv['author']} . {lv['id']}"), callback_data=f"lv:{i}")
    b.button(text=short(f"Оставить как есть: {query}", 45), callback_data="lv:raw")
    b.button(text="Ввести другой уровень / Изменить", callback_data="lv:change")
    b.adjust(1)
    text = "Нашел. Выбери свой уровень (или отправь другой запрос):" if res else "Ничего не нашел или сервер не отвечает."
    await wait.edit_text(text, reply_markup=b.as_markup())


@r.callback_query(Form.level_pick, F.data.startswith("lv:"))
async def f_level_pick(cb: CallbackQuery, state: FSMContext):
    pick = cb.data[3:]
    if pick == "change":
        await state.set_state(Form.level)
        await cb.message.edit_text("Введи новое название уровня или его ID:")
        await cb.answer()
        return

    d = await state.get_data()
    try:
        if pick == "raw":
            name, gd_id = d["level_q"], None
        else:
            lv = d["level_res"][int(pick)]
            name, gd_id = lv["name"], lv["id"]
    except (IndexError, ValueError, KeyError):
        await cb.answer("Список устарел, отправь запрос заново", show_alert=True)
        return
    await state.update_data(level=name, gd_id=gd_id)
    await state.set_state(Form.song_author_input)
    
    b = InlineKeyboardBuilder()
    b.button(text="Изменить уровень", callback_data="change_level")
    b.button(text="Отмена", callback_data="wizard_cancel")
    b.adjust(1)

    await cb.message.edit_text(f"Уровень: <b>{esc(name)}</b>" + (f" (ID {esc(gd_id)})" if gd_id else ""), reply_markup=b.as_markup())
    await cb.message.answer("3/5. Введи <b>автора песни</b>:")
    await cb.answer()


@r.callback_query(F.data == "change_level")
async def cb_change_level(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.level)
    await cb.message.edit_text("Введи название уровня или его ID заново:")
    await cb.answer()


@r.message(Form.song_author_input, F.text, ~F.text.startswith("/"))
async def f_song_author_input(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    author = m.text.strip()[:100]
    await state.update_data(song_author=author)
    await state.set_state(Form.song_name_input)
    
    b = InlineKeyboardBuilder()
    b.button(text="Изменить автора", callback_data="change_author")
    b.button(text="Отмена", callback_data="wizard_cancel")
    b.adjust(1)
    await m.answer(f"Автор: <b>{esc(author)}</b>\n\nТеперь введи <b>название песни</b>:", reply_markup=b.as_markup())


@r.callback_query(F.data == "change_author")
async def cb_change_author(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.song_author_input)
    await cb.message.edit_text("Введи автора песни заново:")
    await cb.answer()


@r.message(Form.song_name_input, F.text, ~F.text.startswith("/"))
async def f_song_name_input(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    song_name = m.text.strip()[:100]
    d = await state.get_data()
    author = d.get("song_author", "")
    query = f"{author} {song_name}"
    
    wait = await m.answer("Ищу песню...")
    res = await search_music(query)
    await state.update_data(song_res=res or [], manual_author=author, manual_name=song_name)
    await state.set_state(Form.song_pick)
    
    b = InlineKeyboardBuilder()
    for i, t in enumerate(res or []):
        b.button(text=short(f"{t['author']} - {t['title']}"), callback_data=f"sg:{i}")
    b.button(text=short(f"Оставить свой вариант: {author} - {song_name}", 45), callback_data="sg:manual_custom")
    b.button(text="Изменить автора/название", callback_data="change_song_all")
    b.adjust(1)
    
    if res is None:
        await state.update_data(song_author=author, song_name=song_name)
        await state.set_state(Form.timecode)
        await wait.edit_text("Музыкальный поиск недоступен. Используем введенные данные.")
        await m.answer("4/5. Таймкод, с чего начинается уровень (например 0:42):", reply_markup=back_kb())
        return
    
    text = "Нашел варианты. Выбери песню из списка или оставь свой вариант:" if res else "Ничего не нашел. Можешь оставить свой вариант."
    await wait.edit_text(text, reply_markup=b.as_markup())


@r.callback_query(F.data == "change_song_all")
async def cb_change_song_all(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.song_author_input)
    await cb.message.edit_text("Введи автора песни заново:")
    await cb.answer()


@r.callback_query(Form.song_pick, F.data.startswith("sg:"))
async def f_song_pick(cb: CallbackQuery, state: FSMContext):
    pick = cb.data[3:]
    d = await state.get_data()
    
    if pick == "manual_custom":
        author = d.get("manual_author", "")
        name = d.get("manual_name", "")
    else:
        try:
            t = d["song_res"][int(pick)]
            author, name = t["author"], t["title"]
        except (IndexError, ValueError, KeyError):
            await cb.answer("Список устарел, отправь запрос заново", show_alert=True)
            return

    await state.update_data(song_author=author, song_name=name)
    await state.set_state(Form.timecode)
    
    b = InlineKeyboardBuilder()
    b.button(text="Изменить песню", callback_data="change_song_all")
    b.button(text="Отмена", callback_data="wizard_cancel")
    b.adjust(1)

    await cb.message.edit_text(f"Песня: <b>{esc(author)} - {esc(name)}</b>", reply_markup=b.as_markup())
    await cb.message.answer("4/5. Таймкод, с которого начинается уровень (например 0:42):")
    await cb.answer()


@r.message(Form.timecode, F.text)
async def f_timecode(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    tc = m.text.strip()
    if not TIMECODE_RE.match(tc):
        await m.answer("Не похоже на таймкод. Формат: 0:42 или 1:05:30")
        return
    await state.update_data(timecode=tc)
    await state.set_state(Form.nick)
    
    b = InlineKeyboardBuilder()
    b.button(text="Изменить таймкод", callback_data="change_timecode")
    b.button(text="Отмена", callback_data="wizard_cancel")
    b.adjust(1)
    await m.answer(f"Таймкод: <b>{esc(tc)}</b>\n\n5/5. Ник мешапера (как подписать):", reply_markup=back_kb())


@r.callback_query(F.data == "change_timecode")
async def cb_change_timecode(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Form.timecode)
    await cb.message.edit_text("Введи таймкод заново (например 0:42):")
    await cb.answer()


@r.message(Form.nick, F.text)
async def f_nick(m: Message, state: FSMContext):
    if m.chat.type != "private":
        return
    if is_banned(m.from_user.id):
        await m.answer("Вы были заблокированы.")
        return
    d = await state.get_data()
    await state.clear()
    now = int(time.time())
    ends = now + int(VOTE_HOURS * 3600)
    cur = ex("INSERT INTO subs(user_id,user_name,file_id,file_type,level,song_author,song_name,timecode,nick,created,ends,gd_id) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (m.from_user.id, m.from_user.full_name, d["file_id"], d.get("file_type", "document"), d["level"], d["song_author"],
              d["song_name"], d["timecode"], m.text.strip()[:100], now, ends, d.get("gd_id")))
    sid = cur.lastrowid
    s = q1("SELECT * FROM subs WHERE id=?", (sid,))
    until = datetime.fromtimestamp(ends).strftime("%d.%m %H:%M")
    try:
        caption = f"{info(s)}\n\nПроверка для ТГК. Голосование до {until}" + (f" (досрочно при {EARLY_LIKES}+)" if EARLY_LIKES else "")
        if s["file_type"] == "audio":
            msg = await bot.send_audio(ADMIN_CHAT_ID, s["file_id"], caption=caption, reply_markup=vote_kb(sid, 0, 0))
        else:
            msg = await bot.send_document(ADMIN_CHAT_ID, s["file_id"], caption=caption, reply_markup=vote_kb(sid, 0, 0))
    except Exception as e:
        log.exception("send to admin chat")
        ex("UPDATE subs SET status='error' WHERE id=?", (sid,))
        await m.answer("Не получилось отправить заявку админам, попробуй позже.")
        return
    ex("UPDATE subs SET admin_msg=? WHERE id=?", (msg.message_id, sid))
    await m.answer(f"Мешап на проверке. Заявка #{sid} отправлена админам.\n\nПрислать новый мешап: /start")


@r.callback_query(F.data.startswith("ban:"))
async def on_ban(cb: CallbackQuery):
    actor_id = cb.from_user.id
    if not is_admin(actor_id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    _, sid = cb.data.split(":")
    s = q1("SELECT * FROM subs WHERE id=?", (int(sid),))
    if not s:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    target_uid = s["user_id"]
    if is_owner(actor_id) and target_uid == actor_id:
        await cb.answer("Нельзя заблокировать самого себя", show_alert=True)
        return
    if not is_owner(actor_id) and is_admin(target_uid):
        await cb.answer("Администратор может банить только обычных пользователей", show_alert=True)
        return

    set_flag(target_uid, "is_banned", 1)
    if is_admin(target_uid) and not is_owner(target_uid):
        ex("UPDATE users SET is_admin=0 WHERE id=?", (target_uid,))

    ex("UPDATE subs SET status='rejected' WHERE user_id=? AND status='pending'", (target_uid,))
    await notify(target_uid, "Вы были заблокированы.")
    await cb.answer("Пользователь заблокирован", show_alert=True)
    
    likes, dislikes = vote_counts(int(sid))
    try:
        await cb.message.edit_reply_markup(reply_markup=vote_kb(int(sid), likes, dislikes, banned=True))
    except TelegramBadRequest:
        pass


@r.callback_query(F.data.startswith("unban_cb:"))
async def on_unban_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Только для администраторов", show_alert=True)
        return
    _, sid = cb.data.split(":")
    s = q1("SELECT * FROM subs WHERE id=?", (int(sid),))
    if not s:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    target_uid = s["user_id"]
    set_flag(target_uid, "is_banned", 0)
    await notify(target_uid, "Вы были разблокированы администратором.")
    await cb.answer("Пользователь разблокирован", show_alert=True)
    
    likes, dislikes = vote_counts(int(sid))
    try:
        await cb.message.edit_reply_markup(reply_markup=vote_kb(int(sid), likes, dislikes, banned=False))
    except TelegramBadRequest:
        pass


@r.callback_query(F.data.startswith("v:"))
async def on_vote(cb: CallbackQuery):
    _, sid, val = cb.data.split(":")
    sid, val = int(sid), int(val)
    s = q1("SELECT * FROM subs WHERE id=?", (sid,))
    if not s or s["status"] != "pending":
        await cb.answer("Голосование закрыто", show_alert=True)
        return
    if not is_admin(cb.from_user.id):
        await cb.answer("Голосуют только админы", show_alert=True)
        return
    if cb.from_user.id == s["user_id"]:
        await cb.answer("За свой мешап голосовать нельзя", show_alert=True)
        return
        
    if not s["first_voter_id"]:
        ex("UPDATE subs SET first_voter_id=? WHERE id=?", (cb.from_user.id, sid))

    ex("INSERT INTO votes(sub_id,user_id,vote) VALUES(?,?,?) "
       "ON CONFLICT(sub_id,user_id) DO UPDATE SET vote=excluded.vote", (sid, cb.from_user.id, val))
    
    likes, dislikes = vote_counts(sid)
    await cb.answer("Голос учтен")
    
    total_admins = max(total_admins_count(), 1)
    voted_total = likes + dislikes
    remaining = total_admins - voted_total
    banned = is_banned(s["user_id"])
    
    if likes >= 3 and likes > dislikes + remaining:
        await finalize(sid, force_accept=True)
        return
    elif dislikes >= 3 and dislikes > likes + remaining:
        await finalize(sid, force_accept=False)
        return
    elif EARLY_LIKES and likes >= EARLY_LIKES and likes > dislikes:
        await finalize(sid, force_accept=True)
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=vote_kb(sid, likes, dislikes, banned=banned))
    except TelegramBadRequest:
        pass


def parse_id(cmd: CommandObject):
    a = (cmd.args or "").strip()
    return int(a) if re.fullmatch(r"-?\d+", a) else None


# ───────────────── КОМАНДЫ СОЗДАТЕЛЯ В ТЕЛЕГРАМЕ (/sozd и /import) ─────────────────
@r.message(Command("sozd"))
async def cmd_sozd(m: Message, command: CommandObject):
    if m.chat.type != "private":
        return
    # Если еще нет ни одного власльца в базе, первая команда /sozd сработает от кого угодно.
    # После этого — только текущий владелец.
    owner_exists = q1("SELECT id FROM users WHERE is_owner=1")
    if owner_exists and not is_owner(m.from_user.id):
        await m.answer("У вас нет прав.")
        return

    uid = parse_id(command) or m.from_user.id
    set_flag(uid, "is_owner", 1)
    set_flag(uid, "is_admin", 1)
    await m.answer(f"OK: Пользователь {uid} назначен главным админом (создателем).")


@r.message(Command("import"))
async def cmd_import(m: Message, command: CommandObject):
    if m.chat.type != "private" or not is_owner(m.from_user.id):
        return
    try:
        args = (command.args or "").split()
        if len(args) < 2:
            await m.answer("Формат: /import user_id count user_id count ...")
            return
        
        success_count = 0
        for i in range(0, len(args) - 1, 2):
            uid = int(args[i])
            target_count = int(args[i+1])
            
            u = q1("SELECT id FROM users WHERE id=?", (uid,))
            if not u:
                ex("INSERT INTO users(id, is_admin) VALUES(?, 1)", (uid,))
            
            db.execute("DELETE FROM votes WHERE user_id=? AND sub_id < 0", (uid,))
            for c_idx in range(target_count):
                fake_sub_id = -(uid * 1000 + c_idx + 1)
                db.execute(
                    "INSERT OR IGNORE INTO subs(id, user_id, status, created) VALUES(?, ?, 'accepted', ?)",
                    (fake_sub_id, uid, int(time.time()))
                )
                db.execute(
                    "INSERT OR REPLACE INTO votes(sub_id, user_id, vote) VALUES(?, ?, 1)",
                    (fake_sub_id, uid)
                )
            db.commit()
            success_count += 1
            
        await m.answer(f"OK: Успешно импортирована статистика для {success_count} пользователей.")
    except Exception as e:
        await m.answer(f"Ошибка импорта: {e}")


@r.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    uid = parse_id(command)
    if uid is None:
        await m.answer("Использование: /unban (айди)")
        return
    set_flag(uid, "is_banned", 0)
    await m.answer(f"Успешно: пользователь {uid} разблокирован.")
    await notify(uid, "Вы были разблокированы администратором.")


@r.message(Command("banlist"))
async def cmd_banlist(m: Message):
    if not is_admin(m.from_user.id):
        return
    banned_users = q("SELECT id, name FROM users WHERE is_banned=1")
    if not banned_users:
        await m.answer("Черный список пуст.")
        return
    lines = ["<b>Черный список заблокированных:</b>"]
    for u in banned_users:
        lines.append(f"- {esc(u['name'] or 'Без имени')} (<code>{u['id']}</code>)")
    await m.answer("\n".join(lines))


@r.message(Command("admlist"))
async def cmd_admlist(m: Message):
    if not is_owner(m.from_user.id):
        return
    admins = q("SELECT id, name, is_owner FROM users WHERE is_admin=1 OR is_owner=1")
    if not admins:
        await m.answer("Список администраторов пуст.")
        return
    lines = ["<b>Список администраторов:</b>"]
    for a in admins:
        role = "Главный админ" if a["is_owner"] else "Админ"
        lines.append(f"- {esc(a['name'] or 'Без имени')} (<code>{a['id']}</code>) - {role}")
    await m.answer("\n".join(lines))


@r.message(Command("addadm"))
async def cmd_addadm(m: Message, command: CommandObject):
    if not is_owner(m.from_user.id):
        return
    uid = parse_id(command)
    if uid is None:
        await m.answer("Использование: /addadm (айди)")
        return
    set_flag(uid, "is_admin", 1)
    await m.answer(f"Успешно: {uid} теперь админ")


@r.message(Command("deladm"))
async def cmd_deladm(m: Message, command: CommandObject):
    if not is_owner(m.from_user.id):
        return
    uid = parse_id(command)
    if uid is None:
        await m.answer("Использование: /deladm (айди)")
        return
    if is_owner(uid):
        await m.answer("Главного админа так не убрать.")
        return
    set_flag(uid, "is_admin", 0)
    await m.answer(f"Успешно: {uid} больше не админ")


@r.message(F.text.startswith("/addprev") | F.caption.startswith("/addprev"))
async def cmd_addprev(m: Message):
    if not is_admin(m.from_user.id):
        return
    raw = (m.text or m.caption or "").split(maxsplit=1)
    name = raw[1].strip() if len(raw) > 1 else ""
    if not name:
        await m.answer("Использование: отправь картинку с подписью /addprev (название уровня)")
        return
    if m.photo:
        file_id, ext = m.photo[-1].file_id, ".jpg"
    elif m.document and Path(m.document.file_name or "").suffix.lower() in (".png", ".jpg", ".jpeg"):
        file_id, ext = m.document.file_id, Path(m.document.file_name).suffix.lower()
    else:
        await m.answer("Нужна картинка (png/jpg/jpeg).")
        return
    key = safe_name(name)
    for p in PREVIEW_DIR.iterdir():
        if p.stem.lower() == key:
            p.unlink()
    await bot.download(file_id, destination=PREVIEW_DIR / f"{key}{ext}")
    await m.answer(f"Превью для {esc(name)} сохранено")


@r.message(Command("stat"))
async def cmd_stat(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    arg = (command.args or "").strip().lower()
    period_filter = ""
    now = int(time.time())
    if arg == "month":
        period_filter = f"AND created >= {now - 30 * 86400}"
    elif arg == "year":
        period_filter = f"AND created >= {now - 365 * 86400}"
        
    admins = q("SELECT id, name FROM users WHERE is_admin=1 OR is_owner=1")
    lines = ["<b>Статистика админов:</b>\n"]
    stats_data = []
    
    for adm in admins:
        uid = adm["id"]
        total_votes_res = q1(f"SELECT COUNT(*) c FROM votes v JOIN subs s ON v.sub_id=s.id WHERE v.user_id=? {period_filter}", (uid,))
        eval_count = total_votes_res["c"] if total_votes_res else 0
        
        first_subs = q(f"SELECT id, final_likes, final_dislikes FROM subs WHERE first_voter_id=? AND stats_posted=1 {period_filter}", (uid,))
        total_score_sum = sum((s["final_likes"] - s["final_dislikes"]) for s in first_subs)
        avg_score = (total_score_sum / len(first_subs)) if first_subs else 0
        
        sum_likes = sum(s["final_likes"] for s in first_subs)
        sum_dislikes = sum(s["final_dislikes"] for s in first_subs)
        score_formula = (eval_count * 2) + (avg_score * 3) + sum_likes - (sum_dislikes * 2)
        
        stats_data.append({
            "name": adm["name"] or str(uid),
            "eval_count": eval_count,
            "avg_score": avg_score,
            "likes": sum_likes,
            "dislikes": sum_dislikes,
            "formula_score": score_formula
        })
        
    stats_data.sort(key=lambda x: x["formula_score"], reverse=True)
    for i, data in enumerate(stats_data[:15], 1):
        lines.append(
            f"{i}. <b>{esc(data['name'])}</b>\n"
            f"   - Оценок: <b>{data['eval_count']}</b> | Средняя: <b>{data['avg_score']:.1f}</b>\n"
            f"   - Итоговый балл: <b>{data['formula_score']:.1f}</b>\n"
        )
    await m.answer("\n".join(lines))


@r.message(Command("help"))
async def cmd_help(m: Message):
    if not is_admin(m.from_user.id):
        return
    t = [
        "/start - отправить мешап на проверку",
        "/stat - статистика админов",
        "/addprev (название) + картинка - превью уровня",
        "/banlist, /unban (айди) - черный список"
    ]
    if is_owner(m.from_user.id):
        t.append("/addadm (айди), /deladm (айди) - управление админами")
        t.append("/admlist - список админов")
        t.append("/sozd, /import - команды создателя")
    await m.answer("\n".join(t))


async def startup_initialization():
    if DEFAULT_OWNER_ID != 0:
        set_flag(DEFAULT_OWNER_ID, "is_owner", 1)
        set_flag(DEFAULT_OWNER_ID, "is_admin", 1)
        log.info(f"Автоматически выданы права владельца для ID: {DEFAULT_OWNER_ID}")


async def main():
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(NameMW())
    dp.include_router(r)
    await startup_initialization()
    asyncio.create_task(watcher())
    asyncio.create_task(sync_group_admins())
    log.info("Бот запущен на Bothost.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
