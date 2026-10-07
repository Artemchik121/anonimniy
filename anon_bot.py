# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║  АНОНИМНЫЕ ВОПРОСЫ ВКонтакте — бот + веб-админка (1 файл)    ║
╚══════════════════════════════════════════════════════════════╝

Нужен рядом файл database.py (база данных).
Установка:      pip install vk_api flask pillow
Запуск:         python anon_bot.py
Веб-панель:     http://127.0.0.1:5000  (пароль — WEB_PASSWORD ниже)
База данных:    SQLite, файл anon_bot.db создаётся сам рядом со скриптом.

Настройка сообщества VK (Управление → Работа с API / Сообщения):
  1. Сообщения сообщества — включить. Возможности ботов — включить,
     «Разрешить добавлять сообщество в беседы» — по желанию.
  2. Ключ доступа: права «сообщения» (и «управление»).
  3. Long Poll API — включить, версия 5.199 (или новее).
     Типы событий: «Входящее сообщение» и «Действие с callback-кнопкой».
"""

import hmac
import json
import logging
import re
import secrets
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

import vk_api
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:      # pip install pillow
    Image = None
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from jinja2 import DictLoader
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

from database import (execute, one, many, init_db, now, get_user, uname, set_state,
                      state_data, get_stats)

# ════════════════════════════ НАСТРОЙКИ ════════════════════════════
TOKEN = "BOT_TOKEN"      # ключ доступа сообщества
GROUP_ID = "GROUP_ID"                   # ID сообщества (только цифры, без минуса)
ADMIN_IDS = [739351270]                         # ваши VK ID — получат админ-кнопки в боте

WEB_ENABLED = True                      # веб-панель вкл/выкл
WEB_HOST = "127.0.0.1"                  # "0.0.0.0" — если нужен доступ с других устройств
WEB_PORT = 5000
WEB_PASSWORD = "SECRET"            # пароль входа в веб-панель
SECRET_KEY = "SECRET"                         # любая длинная строка; пусто = случайная при каждом старте

ASK_COOLDOWN = 20                       # секунд между вопросами одного человека
MAX_LEN = 1000                          # макс. длина вопроса / ответа
SHOW_SENDER_IN_PANEL = False            # False = панель НЕ показывает, кто автор вопроса
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("anon-bot")


# ───────────────────────────── VK: ХЕЛПЕРЫ ─────────────────────────────
vk_session = None
vk = None

START_WORDS = {"начать", "start", "/start", "привет", "меню", "menu", "старт"}
BTN_LINK, BTN_ASK = "📩 Моя ссылка", "❓ Задать вопрос"
BTN_INBOX, BTN_STATS = "📥 Входящие", "📊 Моя статистика"
BTN_SET, BTN_HELP = "⚙️ Настройки", "ℹ️ Помощь"
BTN_ADMIN = "🛠 Админ-панель"
BTN_BACK, BTN_CANCEL = "⬅️ Назад", "✖️ Отмена"
BTN_CLEAR = "🧹 Очистить блок-лист"
BTN_A_STATS, BTN_A_BC = "📈 Статистика бота", "📣 Рассылка"
BTN_A_BAN, BTN_A_UNBAN = "🔨 Бан по ID", "♻️ Разбан по ID"
BTN_A_WEB = "🌐 Веб-панель"
BTN_BC_GO = "✅ Отправить всем"
MENU_LABELS = {BTN_LINK, BTN_ASK, BTN_INBOX, BTN_STATS, BTN_SET, BTN_HELP,
               BTN_ADMIN, BTN_BACK, BTN_CANCEL, BTN_CLEAR, BTN_A_STATS,
               BTN_A_BC, BTN_A_BAN, BTN_A_UNBAN, BTN_A_WEB, BTN_BC_GO}


def my_link(uid):
    return f"https://vk.me/club{GROUP_ID}?ref=q{uid}"


def send(uid, text, kb=None):
    params = dict(user_id=uid, message=text, random_id=get_random_id())
    if kb:
        params["keyboard"] = kb
    try:
        vk.messages.send(**params)
        return True
    except vk_api.exceptions.ApiError as e:
        log.warning("Не удалось отправить %s: %s", uid, e)
        return False


def main_kb(uid):
    kb = VkKeyboard(one_time=False)
    kb.add_button(BTN_LINK, VkKeyboardColor.PRIMARY)
    kb.add_button(BTN_ASK, VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(BTN_INBOX)
    kb.add_button(BTN_STATS)
    kb.add_line()
    kb.add_button(BTN_SET)
    kb.add_button(BTN_HELP)
    if uid in ADMIN_IDS:
        kb.add_line()
        kb.add_button(BTN_ADMIN, VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def cancel_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_button(BTN_CANCEL, VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def settings_kb(user):
    kb = VkKeyboard(one_time=False)
    kb.add_button(f"🔔 Принимать вопросы: {'ВКЛ' if user['accepting'] else 'ВЫКЛ'}",
                  VkKeyboardColor.POSITIVE if user["accepting"] else VkKeyboardColor.NEGATIVE)
    kb.add_line()
    kb.add_button(BTN_CLEAR)
    kb.add_line()
    kb.add_button(BTN_BACK)
    return kb.get_keyboard()


def admin_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_button(BTN_A_STATS, VkKeyboardColor.PRIMARY)
    kb.add_button(BTN_A_BC, VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(BTN_A_BAN, VkKeyboardColor.NEGATIVE)
    kb.add_button(BTN_A_UNBAN)
    kb.add_line()
    kb.add_button(BTN_A_WEB)
    kb.add_button(BTN_BACK)
    return kb.get_keyboard()


def confirm_bc_kb():
    kb = VkKeyboard(one_time=False)
    kb.add_button(BTN_BC_GO, VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button(BTN_CANCEL, VkKeyboardColor.NEGATIVE)
    return kb.get_keyboard()


def question_kb(qid):
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("💬 Ответить", VkKeyboardColor.PRIMARY, {"a": "reply", "q": qid})
    kb.add_line()
    kb.add_callback_button("🚫 Блок автора", VkKeyboardColor.NEGATIVE, {"a": "block", "q": qid})
    kb.add_callback_button("⚠️ Жалоба", VkKeyboardColor.SECONDARY, {"a": "report", "q": qid})
    kb.add_line()
    kb.add_callback_button("🗑 Удалить", VkKeyboardColor.SECONDARY, {"a": "delete", "q": qid})
    return kb.get_keyboard()


# ───────────────────────────── ПОЛЬЗОВАТЕЛИ ─────────────────────────────
def fetch_name(uid):
    try:
        r = vk.users.get(user_ids=uid)[0]
        return r.get("first_name", ""), r.get("last_name", "")
    except Exception:
        return "Пользователь", str(uid)


def ensure_user(uid):
    u = get_user(uid)
    if u:
        return u
    fn, ln = fetch_name(uid)
    execute("INSERT OR IGNORE INTO users(id, first_name, last_name, created_at) VALUES(?,?,?,?)",
            (uid, fn, ln, now()))
    return get_user(uid)


# ───────────────────────────── КАРТОЧКА ДЛЯ ИСТОРИИ ─────────────────────────────
_FONTS = {
    "reg": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "bold": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def _font(kind, size):
    for p in _FONTS[kind]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _clean(t):
    """Убираем эмодзи и невидимые символы — шрифт их не умеет рисовать."""
    t = "".join(ch for ch in t if ord(ch) <= 0xFFFF and ch not in "\ufe0f\u200d\u200b")
    return re.sub(r"[ \t]+", " ", t).strip()


def _wrap(text, font, width):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            while font.getlength(word) > width:          # очень длинное слово
                k = max(1, int(len(word) * width / font.getlength(word)))
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:k])
                word = word[k:]
            test = (cur + " " + word).strip()
            if font.getlength(test) <= width:
                cur = test
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit(text, kind, width, max_h, start, minimum):
    size = start
    while True:
        f = _font(kind, size)
        lines = _wrap(text, f, width)
        lh = int(size * 1.28)
        if len(lines) * lh <= max_h or size <= minimum:
            maxl = max_h // lh
            if len(lines) > maxl:                         # не влезло — обрезаем с «…»
                lines = lines[:maxl]
                lines[-1] = lines[-1].rstrip(" .,") + "…"
            return f, lines, lh
        size -= 4


def _background(W, H):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bot = (66, 44, 160), (22, 18, 48)
    for y in range(H):                                    # градиентный фон
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))        # мягкие круги для глубины
    g = ImageDraw.Draw(glow)
    g.ellipse([-260, -200, 520, 580], fill=(140, 110, 255, 70))
    g.ellipse([640, 1250, 1420, 2030], fill=(255, 110, 150, 55))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def make_invite_card(name, link_text):
    W, H = 1080, 1920
    img = _background(W, H)
    d = ImageDraw.Draw(img)

    def center(text, font, y, fill):
        d.text(((W - font.getlength(text)) / 2, y), text, font=font, fill=fill)

    center("Анонимные вопросы", _font("bold", 54), 150, (255, 255, 255))
    X0, X1, Y0, Y1 = 80, 1000, 470, 1230
    d.rounded_rectangle([X0, Y0, X1, Y1], radius=56, fill=(255, 255, 255))
    cx, cy, r = W // 2, Y0 + 40 + 70, 70                      # круглый значок «?»
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(106, 76, 240))
    qf = _font("bold", 92)
    d.text((cx - qf.getlength("?") / 2, cy - 62), "?", font=qf, fill=(255, 255, 255))
    f1, l1, h1 = _fit(_clean(name), "bold", 780, 160, 70, 40)
    y = cy + r + 50
    for ln in l1:
        center(ln, f1, y, (28, 24, 51))
        y += h1
    f2, l2, h2 = _fit("Задай мне вопрос — я не узнаю, кто ты", "bold", 780, 280, 66, 40)
    y += 20
    for ln in l2:
        center(ln, f2, y, (106, 76, 240))
        y += h2
    center("Это полностью анонимно", _font("reg", 40), Y1 - 90, (111, 105, 144))

    center("Переходи по ссылке и пиши", _font("bold", 50), 1340, (255, 255, 255))
    lf = _font("reg", 36)
    lw = lf.getlength(link_text)
    d.rounded_rectangle([(W - lw) / 2 - 36, 1440, (W + lw) / 2 + 36, 1516], radius=38,
                        fill=(255, 255, 255))
    d.text(((W - lw) / 2, 1455), link_text, font=lf, fill=(106, 76, 240))
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    img.save(path, "PNG")
    return path


def make_story_card(question, answer, name, link_text):
    W, H = 1080, 1920
    img = _background(W, H)
    d = ImageDraw.Draw(img)

    PAD, X0, X1 = 56, 80, 1000
    inner = X1 - X0 - 2 * PAD
    q_font, q_lines, q_lh = _fit(_clean(question), "bold", inner, 430, 70, 38)
    a_font, a_lines, a_lh = _fit(_clean(answer), "reg", inner, 520, 62, 36)
    q_h = PAD + 52 + len(q_lines) * q_lh + PAD
    a_h = PAD + 52 + len(a_lines) * a_lh + PAD
    gap = 36
    total = q_h + gap + a_h
    y = max(260, (H - total) // 2 - 60)

    hd = _font("bold", 54)
    title = "Анонимные вопросы"
    d.text(((W - hd.getlength(title)) / 2, 130), title, font=hd, fill=(255, 255, 255))

    # карточка вопроса
    d.rounded_rectangle([X0, y, X1, y + q_h], radius=48, fill=(255, 255, 255))
    d.ellipse([X0 + PAD, y + PAD - 4, X0 + PAD + 44, y + PAD + 40], fill=(28, 24, 51))
    qm = _font("bold", 30)
    d.text((X0 + PAD + 22 - qm.getlength("?") / 2, y + PAD + 2), "?", font=qm, fill=(255, 255, 255))
    d.text((X0 + PAD + 60, y + PAD + 2), "Анонимный вопрос", font=_font("reg", 34), fill=(111, 105, 144))
    ty = y + PAD + 52
    for ln in q_lines:
        d.text((X0 + PAD, ty), ln, font=q_font, fill=(28, 24, 51))
        ty += q_lh

    # карточка ответа
    y2 = y + q_h + gap
    d.rounded_rectangle([X0, y2, X1, y2 + a_h], radius=48, fill=(106, 76, 240),
                        outline=(176, 158, 255), width=3)
    d.text((X0 + PAD, y2 + PAD), _clean(name) or "Ответ", font=_font("bold", 36), fill=(225, 217, 255))
    ty = y2 + PAD + 52
    for ln in a_lines:
        d.text((X0 + PAD, ty), ln, font=a_font, fill=(255, 255, 255))
        ty += a_lh

    # низ: приглашение и ссылка
    cta = "Задай мне вопрос анонимно"
    cf = _font("bold", 50)
    d.text(((W - cf.getlength(cta)) / 2, 1640), cta, font=cf, fill=(255, 255, 255))
    lf = _font("reg", 36)
    lw = lf.getlength(link_text)
    px0, px1 = (W - lw) / 2 - 36, (W + lw) / 2 + 36
    d.rounded_rectangle([px0, 1730, px1, 1730 + 76], radius=38, fill=(255, 255, 255, 255))
    d.text(((W - lw) / 2, 1730 + 15), link_text, font=lf, fill=(106, 76, 240))

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    img.save(path, "PNG")
    return path


def send_story_card(uid, q):
    """Делает картинку и присылает её в личку — остаётся выложить в историю."""
    try:
        owner = q["to_id"]
        link = my_link(owner)
        path = make_story_card(q["text"], q["answer"], uname(owner),
                               link.replace("https://", ""))
        try:
            ph = vk_api.VkUpload(vk_session).photo_messages(photos=path)[0]
        finally:
            os.remove(path)
        vk.messages.send(
            user_id=uid, random_id=get_random_id(),
            attachment=f"photo{ph['owner_id']}_{ph['id']}",
            message=STORY_GUIDE)
        send_link(uid, link)
    except Exception:
        log.exception("Не удалось сделать карточку для истории")
        send(uid, "😕 Не получилось сделать картинку. Попробуйте ещё раз позже.")


STORY_GUIDE = (
    "📸 Картинка готова! Как выложить её в историю (с телефона, в приложении ВК):\n\n"
    "1️⃣ Нажмите на картинку выше, чтобы открыть её.\n"
    "2️⃣ Нажмите «Поделиться» (стрелка или три точки) и выберите «В историю».\n"
    "3️⃣ В редакторе истории нажмите на значок стикеров и выберите «Ссылка».\n"
    "4️⃣ Вставьте ссылку из сообщения ниже (зажмите его → «Копировать») и поставьте стикер под картинкой.\n"
    "5️⃣ Нажмите «Опубликовать».\n\n"
    "💡 Если стикера «Ссылка» нет, вставьте адрес в статус профиля и напишите в истории «ссылка в статусе». "
    "Названия кнопок в приложении могут немного отличаться."
)


def send_link(uid, link):
    """Ссылка отдельным сообщением: кликабельна, плюс кнопка «Открыть»."""
    try:
        kb = VkKeyboard(inline=True)
        kb.add_openlink_button("🔗 Открыть ссылку", link)
        vk.messages.send(user_id=uid, random_id=get_random_id(), message=link,
                         keyboard=kb.get_keyboard())
    except Exception:
        send(uid, link)


def invite_kb():
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("📸 Сделать историю", VkKeyboardColor.PRIMARY, {"a": "invite"})
    return kb.get_keyboard()


def send_invite_card(uid):
    try:
        link = my_link(uid)
        path = make_invite_card(uname(uid), link.replace("https://", ""))
        try:
            ph = vk_api.VkUpload(vk_session).photo_messages(photos=path)[0]
        finally:
            os.remove(path)
        vk.messages.send(
            user_id=uid, random_id=get_random_id(),
            attachment=f"photo{ph['owner_id']}_{ph['id']}",
            message=STORY_GUIDE)
        send_link(uid, link)
    except Exception:
        log.exception("Не удалось сделать карточку-приглашение")
        send(uid, "😕 Не получилось сделать картинку. Попробуйте ещё раз позже.")


def story_kb(qid):
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("📸 Выложить в историю", VkKeyboardColor.PRIMARY, {"a": "story", "q": qid})
    return kb.get_keyboard()


# ───────────────────────────── ЛОГИКА БОТА ─────────────────────────────
def cmd_start(uid):
    send(uid,
         "👋 Добро пожаловать в анонимные вопросы!\n\n"
         "Ниже ваша личная ссылка. Разместите её в статусе или истории — любой, кто её откроет, "
         "сможет задать вам вопрос, а вы не узнаете, кто это был. "
         "Вы тоже можете писать другим — нажмите «❓ Задать вопрос».",
         main_kb(uid))
    send_link(uid, my_link(uid))
    if Image is not None:
        send(uid, "🔥 Хотите, чтобы вам задавали вопросы? Выложите историю — "
                  "я сделаю для неё красивую картинку со ссылкой:", invite_kb())


def cmd_link(uid):
    send(uid, "📩 Ваша ссылка для анонимных вопросов (нажмите на неё, чтобы открыть, "
              "или зажмите, чтобы скопировать):", main_kb(uid))
    send_link(uid, my_link(uid))
    if Image is not None:
        send(uid, "Могу сделать картинку для истории:", invite_kb())


def cmd_help(uid):
    send(uid,
         "ℹ️ Как это работает\n\n"
         "• Вашу ссылку открывает кто угодно — и пишет вам вопрос.\n"
         "• Вы получаете его от имени бота. Автор остаётся скрытым.\n"
         "• Нажмите «💬 Ответить» — ответ уйдёт автору в личку от бота.\n"
         "• «🚫 Блок автора» — человек больше не сможет вам писать (он об этом не узнает).\n"
         "• «⚠️ Жалоба» — отправит вопрос на проверку администратору.\n\n"
         "📸 Как выложить историю: нажмите «📩 Моя ссылка» → «Сделать историю» — бот пришлёт картинку и подробную инструкцию.\n\n"
         "Задать вопрос другому: «❓ Задать вопрос» → пришлите его ссылку на профиль или ID. "
         "Человек должен хотя бы раз запустить бота.",
         main_kb(uid))


def cmd_stats(uid):
    r = one("SELECT COUNT(*) c FROM questions WHERE to_id=? AND status!='deleted'", (uid,))["c"]
    a = one("SELECT COUNT(*) c FROM questions WHERE to_id=? AND status='answered'", (uid,))["c"]
    n = one("SELECT COUNT(*) c FROM questions WHERE to_id=? AND status='new'", (uid,))["c"]
    s = one("SELECT COUNT(*) c FROM questions WHERE from_id=?", (uid,))["c"]
    b = one("SELECT COUNT(*) c FROM blocks WHERE owner_id=?", (uid,))["c"]
    send(uid, f"📊 Ваша статистика\n\n📨 Получено вопросов: {r}\n💬 Отвечено: {a}\n"
              f"🕓 Ждут ответа: {n}\n✉️ Вы задали вопросов: {s}\n🚫 В блок-листе: {b}", main_kb(uid))


def cmd_inbox(uid):
    rows = many("SELECT * FROM questions WHERE to_id=? AND status='new' AND delivered=1 "
                "ORDER BY id DESC LIMIT 5", (uid,))
    if not rows:
        send(uid, "📭 Новых вопросов пока нет.\nПоделитесь своей ссылкой — и они появятся!\n\n" + my_link(uid),
             main_kb(uid))
        return
    total = one("SELECT COUNT(*) c FROM questions WHERE to_id=? AND status='new'", (uid,))["c"]
    send(uid, f"📥 Ждут ответа: {total}. Показываю последние {len(rows)}:", main_kb(uid))
    for q in reversed(rows):
        send(uid, f"📨 Анонимный вопрос №{q['id']}\n\n{q['text']}", question_kb(q["id"]))


def cmd_settings(uid):
    u = get_user(uid)
    send(uid, "⚙️ Настройки\n\nЕсли отключить приём, по вашей ссылке никто не сможет писать, "
              "пока вы снова не включите его.", settings_kb(u))


def resolve_target(text):
    t = text.strip()
    m = re.search(r"ref=q(\d+)", t) or re.fullmatch(r"q(\d+)", t)
    if m:
        return int(m.group(1))
    t = re.sub(r"^(https?://)?(m\.)?(vk\.com|vk\.ru)/", "", t).lstrip("@*").strip("/ ")
    m = re.fullmatch(r"(?:id)?(\d+)", t)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"[A-Za-z0-9_.]{2,40}", t):
        try:
            r = vk.utils.resolveScreenName(screen_name=t)
            if r and r.get("type") == "user":
                return int(r["object_id"])
        except Exception:
            pass
    return None


def begin_ask(uid, target, announce=True):
    """Проверяет получателя и включает режим «пишу вопрос». True — всё ок."""
    if target == uid:
        send(uid, "😅 Себе писать анонимно нет смысла. Отправьте ссылку друзьям!", main_kb(uid))
        return False
    t = get_user(target)
    if not t:
        send(uid, "🤷 Этот человек ещё не запускал бота, поэтому написать ему нельзя.\n"
                  "Отправьте ему вашу ссылку-приглашение — пусть откроет бота.", main_kb(uid))
        return False
    if t["banned"] or not t["accepting"]:
        send(uid, "🔒 Этот пользователь сейчас не принимает вопросы.", main_kb(uid))
        return False
    set_state(uid, "ask", {"to": target})
    if announce:
        send(uid, f"🕶 Вы пишете анонимный вопрос: {uname(target)}\n\n"
                  f"Получатель не узнает, кто вы. Напишите текст (до {MAX_LEN} символов):",
             cancel_kb())
    return True


def handle_ask(uid, user, text, msg):
    data = state_data(user)
    target = data.get("to")
    if not text:
        send(uid, "✍️ Принимаю только текст. Напишите вопрос словами.", cancel_kb())
        return
    if len(text) > MAX_LEN:
        send(uid, f"✂️ Слишком длинно ({len(text)}). Сократите до {MAX_LEN} символов.", cancel_kb())
        return
    wait = ASK_COOLDOWN - (now() - (user["last_ask_ts"] or 0))
    if wait > 0:
        send(uid, f"⏳ Не так быстро! Подождите ещё {wait} с.", cancel_kb())
        return
    t = get_user(target)
    if not t or t["banned"] or not t["accepting"]:
        set_state(uid)
        send(uid, "🔒 Пользователь больше не принимает вопросы.", main_kb(uid))
        return
    blocked = one("SELECT 1 FROM blocks WHERE owner_id=? AND blocked_id=?", (target, uid))
    execute("UPDATE users SET last_ask_ts=? WHERE id=?", (now(), uid))
    set_state(uid)
    if blocked:
        # Автор не должен знать о блокировке — делаем вид, что всё доставлено.
        execute("INSERT INTO questions(to_id, from_id, text, status, delivered, created_at) "
                "VALUES(?,?,?,?,?,?)", (target, uid, text, "deleted", 0, now()))
        send(uid, "✅ Вопрос отправлен анонимно!", main_kb(uid))
        return
    qid = execute("INSERT INTO questions(to_id, from_id, text, created_at) VALUES(?,?,?,?)",
                  (target, uid, text, now()))
    ok = send(target, f"📨 Анонимный вопрос №{qid}\n\n{text}", question_kb(qid))
    if ok:
        send(uid, "✅ Вопрос отправлен анонимно! Если получатель ответит — ответ придёт сюда.",
             main_kb(uid))
    else:
        execute("UPDATE questions SET delivered=0 WHERE id=?", (qid,))
        send(uid, "😕 Не получилось доставить: человек закрыл личные сообщения от сообщества.",
             main_kb(uid))


def handle_reply(uid, user, text):
    qid = state_data(user).get("q")
    q = one("SELECT * FROM questions WHERE id=? AND to_id=?", (qid, uid))
    if not q or q["status"] == "deleted":
        set_state(uid)
        send(uid, "Этот вопрос уже недоступен.", main_kb(uid))
        return
    if not text:
        send(uid, "✍️ Ответ должен быть текстом.", cancel_kb())
        return
    if len(text) > MAX_LEN:
        send(uid, f"✂️ Слишком длинно. Максимум {MAX_LEN} символов.", cancel_kb())
        return
    execute("UPDATE questions SET answer=?, status='answered', answered_at=? WHERE id=?",
            (text, now(), qid))
    set_state(uid)
    send(q["from_id"], f"💬 Ответ на ваш анонимный вопрос №{qid}\n\n"
                       f"Вы спрашивали:\n«{q['text']}»\n\n{uname(uid)} отвечает:\n{text}", story_kb(qid))
    send(uid, "✅ Ответ отправлен автору вопроса (он по-прежнему анонимен).", main_kb(uid))
    send(uid, "Хотите поделиться этим в истории? Сделаю красивую картинку 👇", story_kb(qid))


def find_state(uid, text):
    target = resolve_target(text)
    if not target:
        send(uid, "🔎 Не нашёл такого человека. Пришлите ссылку на профиль (vk.com/…), "
                  "числовой ID или ссылку-приглашение.", cancel_kb())
        return
    begin_ask(uid, target)


# ───────────────────────────── РАССЫЛКА ─────────────────────────────
def start_broadcast(text, only_admins=False):
    bid = execute("INSERT INTO broadcasts(text, created_at, status) VALUES(?,?,'running')",
                  (text, now()))
    threading.Thread(target=_run_broadcast, args=(bid, text, only_admins), daemon=True).start()
    return bid


def _run_broadcast(bid, text, only_admins):
    if only_admins:
        ids = list(ADMIN_IDS)
    else:
        ids = [r["id"] for r in many("SELECT id FROM users WHERE banned=0")]
    execute("UPDATE broadcasts SET total=? WHERE id=?", (len(ids), bid))
    sent = failed = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            res = vk.messages.send(user_ids=",".join(map(str, chunk)), message=text,
                                   random_id=get_random_id())
            if isinstance(res, list):
                for item in res:
                    if isinstance(item, dict) and item.get("error"):
                        failed += 1
                    else:
                        sent += 1
            else:
                sent += len(chunk)
        except Exception as e:
            log.warning("Рассылка: ошибка пачки: %s", e)
            failed += len(chunk)
        execute("UPDATE broadcasts SET sent=?, failed=? WHERE id=?", (sent, failed, bid))
        time.sleep(0.4)
    execute("UPDATE broadcasts SET status='done', sent=?, failed=? WHERE id=?", (sent, failed, bid))
    log.info("Рассылка #%s завершена: %s ок / %s ошибок", bid, sent, failed)


# ───────────────────────────── АДМИН В ЧАТЕ ─────────────────────────────
def admin_menu(uid, t):
    if uid not in ADMIN_IDS:
        return False
    if t == BTN_ADMIN:
        send(uid, "🛠 Админ-панель. Выберите действие:", admin_kb())
    elif t == BTN_A_STATS:
        s = get_stats()
        send(uid, "📈 Статистика бота\n\n"
                  f"👥 Пользователей: {s['users']} (сегодня +{s['new_today']})\n"
                  f"🚫 В бане: {s['banned']}\n"
                  f"📨 Вопросов: {s['questions']} (сегодня {s['today']})\n"
                  f"💬 Отвечено: {s['answered']} ({s['rate']}%)\n"
                  f"⚠️ Жалоб: {s['reported']}", admin_kb())
    elif t == BTN_A_BC:
        set_state(uid, "bc_text")
        send(uid, "📣 Пришлите текст рассылки одним сообщением. Его получат все пользователи бота.",
             cancel_kb())
    elif t in (BTN_A_BAN, BTN_A_UNBAN):
        set_state(uid, "adm_ban" if t == BTN_A_BAN else "adm_unban")
        send(uid, "Пришлите VK ID или ссылку на профиль:", cancel_kb())
    elif t == BTN_A_WEB:
        send(uid, f"🌐 Веб-панель: http://{WEB_HOST}:{WEB_PORT}\n"
                  "Рассылки, жалобы, пользователи и график — там удобнее.", admin_kb())
    elif t == BTN_BC_GO:
        u = get_user(uid)
        text = state_data(u).get("text") if u["state"] == "bc_confirm" else None
        if not text:
            send(uid, "Нет текста для рассылки.", admin_kb())
            return True
        set_state(uid)
        start_broadcast(text)
        send(uid, "🚀 Рассылка запущена. Прогресс — в веб-панели, в разделе «Рассылка».", admin_kb())
    else:
        return False
    return True


def admin_state(uid, user, text):
    st = user["state"]
    if st == "bc_text":
        if not text or len(text) > 4000:
            send(uid, "Нужен текст до 4000 символов.", cancel_kb())
            return
        set_state(uid, "bc_confirm", {"text": text})
        total = one("SELECT COUNT(*) c FROM users WHERE banned=0")["c"]
        send(uid, f"Проверьте текст:\n\n{text}\n\n— получат {total} чел. Отправляем?", confirm_bc_kb())
    elif st in ("adm_ban", "adm_unban"):
        target = resolve_target(text or "")
        if not target or not get_user(target):
            send(uid, "Такого пользователя нет в базе. Пришлите другой ID.", cancel_kb())
            return
        execute("UPDATE users SET banned=? WHERE id=?", (1 if st == "adm_ban" else 0, target))
        set_state(uid)
        send(uid, ("🔨 Забанен: " if st == "adm_ban" else "♻️ Разбанен: ") + uname(target), admin_kb())
    elif st == "bc_confirm":
        send(uid, "Нажмите «✅ Отправить всем» или «✖️ Отмена».", confirm_bc_kb())


# ───────────────────────────── РОУТИНГ СООБЩЕНИЙ ─────────────────────────────
def menu_cmd(uid, text):
    t = text.strip()
    if t.lower() in START_WORDS:
        cmd_start(uid)
    elif t == BTN_LINK:
        cmd_link(uid)
    elif t == BTN_ASK:
        set_state(uid, "find")
        send(uid, "🔎 Кому хотите написать? Пришлите ссылку на профиль (vk.com/…), числовой ID "
                  "или ссылку-приглашение.", cancel_kb())
    elif t == BTN_INBOX:
        cmd_inbox(uid)
    elif t == BTN_STATS:
        cmd_stats(uid)
    elif t == BTN_HELP:
        cmd_help(uid)
    elif t == BTN_SET:
        cmd_settings(uid)
    elif t.startswith("🔔 Принимать"):
        u = get_user(uid)
        execute("UPDATE users SET accepting=? WHERE id=?", (0 if u["accepting"] else 1, uid))
        cmd_settings(uid)
    elif t == BTN_CLEAR:
        execute("DELETE FROM blocks WHERE owner_id=?", (uid,))
        send(uid, "🧹 Блок-лист очищен.", settings_kb(get_user(uid)))
    elif t in (BTN_BACK, BTN_CANCEL):
        send(uid, "Главное меню 👇", main_kb(uid))
    else:
        return admin_menu(uid, t)
    return True


def is_menu_text(text):
    t = text.strip()
    return (t in MENU_LABELS or t.lower() in START_WORDS or t.startswith("🔔 Принимать"))


def on_message(msg):
    uid = msg.get("from_id", 0)
    if uid <= 0:
        return
    text = (msg.get("text") or "").strip()
    ref = msg.get("ref") or ""
    user = ensure_user(uid)
    if user["banned"]:
        send(uid, "🚫 Доступ к боту ограничен администратором.")
        return

    # Переход по чужой ссылке-приглашению
    m = re.fullmatch(r"q(\d+)", ref)
    if m:
        target = int(m.group(1))
        if not text or text.lower() in START_WORDS:
            begin_ask(uid, target)
            return
        if not is_menu_text(text):
            if not begin_ask(uid, target, announce=False):
                return
            user = get_user(uid)

    # Кнопки меню всегда сбрасывают текущий режим
    if is_menu_text(text):
        keep = user["state"] == "bc_confirm" and text == BTN_BC_GO
        if not keep:
            set_state(uid)
        menu_cmd(uid, text)
        return

    st = user["state"]
    if st == "ask":
        handle_ask(uid, user, text, msg)
    elif st == "reply":
        handle_reply(uid, user, text)
    elif st == "find":
        find_state(uid, text)
    elif st in ("bc_text", "bc_confirm", "adm_ban", "adm_unban") and uid in ADMIN_IDS:
        admin_state(uid, user, text)
    else:
        send(uid, "Не понял 🤔 Воспользуйтесь кнопками меню.", main_kb(uid))


def on_callback(o):
    uid = o["user_id"]
    payload = o.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    def snack(t):
        try:
            vk.messages.sendMessageEventAnswer(
                event_id=o["event_id"], user_id=uid, peer_id=o["peer_id"],
                event_data=json.dumps({"type": "show_snackbar", "text": t[:90]}, ensure_ascii=False))
        except Exception as e:
            log.warning("snackbar: %s", e)

    a, qid = payload.get("a"), payload.get("q")
    q = one("SELECT * FROM questions WHERE id=?", (qid,))
    if a == "invite":
        if Image is None:
            snack("Администратору: установите pillow")
            return
        ensure_user(uid)
        snack("Рисую картинку, секунду…")
        threading.Thread(target=send_invite_card, args=(uid,), daemon=True).start()
        return
    if a == "story":
        if Image is None:
            snack("Администратору: установите pillow")
            return
        if not q or uid not in (q["to_id"], q["from_id"]) or not q["answer"] or q["status"] == "deleted":
            snack("Вопрос недоступен")
            return
        snack("Рисую картинку, секунду…")
        threading.Thread(target=send_story_card, args=(uid, q), daemon=True).start()
        return
    if not q or q["to_id"] != uid or q["status"] == "deleted":
        snack("Вопрос недоступен")
        return
    ensure_user(uid)
    if a == "reply":
        if q["status"] == "answered":
            snack("Вы уже ответили на этот вопрос")
            return
        set_state(uid, "reply", {"q": qid})
        snack("Напишите ответ в чат")
        send(uid, f"💬 Ответ на вопрос №{qid}:\n«{q['text']}»\n\nНапишите ответ одним сообщением:",
             cancel_kb())
    elif a == "block":
        execute("INSERT OR IGNORE INTO blocks(owner_id, blocked_id) VALUES(?,?)", (uid, q["from_id"]))
        snack("Автор заблокирован. Он об этом не узнает")
    elif a == "report":
        execute("UPDATE questions SET reported=1 WHERE id=?", (qid,))
        snack("Жалоба отправлена администратору")
        for adm in ADMIN_IDS:
            send(adm, f"⚠️ Жалоба на анонимный вопрос №{qid}. Разберите её в веб-панели.")
    elif a == "delete":
        execute("UPDATE questions SET status='deleted' WHERE id=?", (qid,))
        snack("Вопрос удалён")


def run_bot():
    while True:
        try:
            longpoll = VkBotLongPoll(vk_session, GROUP_ID)
            log.info("✅ Бот запущен и слушает сообщения")
            for event in longpoll.listen():
                try:
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        obj = event.obj
                        on_message(obj.get("message") or obj)
                    elif event.type == VkBotEventType.MESSAGE_EVENT:
                        on_callback(event.obj)
                except Exception:
                    log.exception("Ошибка обработки события")
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("Long Poll упал, перезапуск через 5 секунд")
            time.sleep(5)


# ═════════════════════════════ ВЕБ-ПАНЕЛЬ ═════════════════════════════
CSS = r"""
*{box-sizing:border-box}
:root{--ink:#1c1833;--ink2:#2b2553;--paper:#f4f1fb;--line:#e4def4;--muted:#6f6990;
--violet:#6a4cf0;--soft:#ebe6ff;--mint:#16a37f;--coral:#e24b55;--amber:#d99410;--r:16px}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:15px/1.55 Onest,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--paper);color:var(--ink)}
h1,h2,h3{font-family:Unbounded,Onest,system-ui,sans-serif;font-weight:600;margin:0;letter-spacing:-.01em}
a{color:var(--violet)}
.shell{display:grid;grid-template-columns:252px 1fr;min-height:100vh}
aside{background:var(--ink);color:#d8d2f5;padding:26px 16px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:30px}
.brand{display:flex;align-items:center;gap:12px;font-family:Unbounded,Onest,sans-serif;font-weight:600;font-size:15px;color:#fff;padding:0 8px;line-height:1.2}
.brand small{display:block;font:400 12px Onest,sans-serif;color:#9d96c9;margin-top:2px}
nav{display:flex;flex-direction:column;gap:4px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-radius:12px;color:#c9c2ee;text-decoration:none;font-weight:500;transition:background .15s}
nav a:hover{background:var(--ink2);color:#fff}
nav a.on{background:var(--violet);color:#fff}
.badge{background:var(--coral);color:#fff;border-radius:99px;font-size:12px;padding:1px 8px;font-weight:600}
.side-foot{margin-top:auto;padding:0 8px;font-size:13px;color:#9d96c9}
.side-foot a{color:#fff}
main{padding:38px clamp(18px,4vw,52px) 60px;max-width:1220px;width:100%}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.head h1{font-size:27px}
.head p{margin:8px 0 0;color:var(--muted)}
.card{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:24px}
.card h3{font-size:16px;margin-bottom:4px}
.card .sub{color:var(--muted);font-size:13px;margin:0}
.grid2{display:grid;grid-template-columns:1.7fr 1fr;gap:20px;margin-bottom:20px}
.kv{margin:18px 0 0;display:grid;gap:0}
.kv div{display:flex;justify-content:space-between;align-items:baseline;padding:12px 0;border-bottom:1px solid var(--line)}
.kv div:last-child{border:0}
.kv dt{color:var(--muted)}
.kv dd{margin:0;font-family:Unbounded,Onest,sans-serif;font-weight:600;font-size:18px}
.kv dd.bad{color:var(--coral)}
.bars{display:flex;gap:8px;height:210px;margin-top:20px}
.col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;font-size:11px;color:var(--muted);min-width:0}
.col b{font-size:12px;color:var(--ink);font-weight:600;height:16px}
.track{flex:1;min-height:0;width:100%;display:flex;align-items:flex-end}
.track i{display:block;width:100%;background:var(--violet);border-radius:9px 9px 3px 3px}
.col:last-child .track i{background:var(--mint)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;cursor:pointer;background:var(--violet);color:#fff;font:600 14px Onest,sans-serif;padding:11px 20px;border-radius:12px;text-decoration:none;transition:transform .1s,filter .15s}
.btn:hover{filter:brightness(1.08)}.btn:active{transform:translateY(1px)}
.btn.ghost{background:var(--soft);color:var(--violet)}
.btn.danger{background:#fde8ea;color:var(--coral)}
.btn.ok{background:#dcf5ee;color:var(--mint)}
.btn.sm{padding:6px 12px;font-size:13px;border-radius:10px}
:focus-visible{outline:3px solid #b9a9ff;outline-offset:2px}
input[type=text],input[type=password],textarea{width:100%;font:15px Onest,sans-serif;padding:12px 14px;border:1.5px solid var(--line);border-radius:12px;background:#fff;color:var(--ink)}
textarea{min-height:170px;resize:vertical;line-height:1.5}
input:focus,textarea:focus{outline:none;border-color:var(--violet);box-shadow:0 0 0 4px var(--soft)}
label.chk{display:flex;gap:10px;align-items:center;color:var(--muted);cursor:pointer}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-weight:500;color:var(--muted);font-size:13px;padding:0 14px 12px}
td{padding:13px 14px;border-top:1px solid var(--line);vertical-align:middle}
tr.off td{opacity:.55}
.tag{display:inline-block;border-radius:99px;font-size:12px;font-weight:600;padding:2px 10px;background:var(--soft);color:var(--violet)}
.tag.ok{background:#dcf5ee;color:var(--mint)}.tag.bad{background:#fde8ea;color:var(--coral)}.tag.warn{background:#fdf1d6;color:var(--amber)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px}
.tabs a{padding:8px 16px;border-radius:99px;text-decoration:none;color:var(--muted);background:#fff;border:1px solid var(--line);font-weight:500}
.tabs a.on{background:var(--ink);border-color:var(--ink);color:#fff}
.q{display:flex;gap:16px;margin-bottom:16px}
.ava{flex:none;width:46px;height:46px;border-radius:50% 50% 50% 8px;background:var(--ink);color:#fff;display:grid;place-items:center;font:600 20px Unbounded,sans-serif}
.q.deleted .ava{background:#b8b3d1}
.qb{flex:1;min-width:0}
.meta{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;color:var(--muted);font-size:13px;margin-bottom:8px}
.meta b{color:var(--ink)}
.bub{background:#fff;border:1px solid var(--line);border-radius:4px 18px 18px 18px;padding:14px 18px;white-space:pre-wrap;word-break:break-word}
.bub.ans{background:var(--soft);border-color:#d9d0ff;border-radius:18px 4px 18px 18px;margin:8px 0 0 36px}
.q.rep .bub:not(.ans){border-color:var(--coral);box-shadow:0 0 0 3px #fde8ea}
.acts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.acts form,td form{display:inline}
.flash{padding:12px 16px;border-radius:12px;margin-bottom:20px;font-weight:500}
.flash.ok{background:#dcf5ee;color:#0c6e55}.flash.err{background:#fde8ea;color:#a8232d}
.bar{height:8px;background:var(--soft);border-radius:9px;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:var(--violet);border-radius:9px}
.empty{text-align:center;color:var(--muted);padding:44px 10px}
.pager{display:flex;gap:10px;margin-top:20px}
.row{display:grid;grid-template-columns:1.5fr 1fr;gap:20px;align-items:start}
.search{display:flex;gap:10px;max-width:420px}
.hint{color:var(--muted);font-size:13px;margin:8px 0 0}
.login{min-height:100vh;display:grid;place-items:center;background:var(--ink);padding:20px}
.login .box{width:min(400px,100%)}
.login svg{display:block;margin:0 auto 22px}
.login h1{color:#fff;text-align:center;font-size:24px;margin-bottom:6px}
.login p.t{color:#a9a2d4;text-align:center;margin:0 0 26px}
.login form{background:#fff;border-radius:20px;padding:26px;display:grid;gap:14px}
@media(max-width:900px){
 .shell{grid-template-columns:1fr}
 aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;padding:14px;gap:10px 18px}
 nav{flex-direction:row;flex-wrap:wrap}.side-foot{margin:0 0 0 auto}
 .grid2,.row{grid-template-columns:1fr}
 .bars{gap:4px}.col span{display:none}
 table{display:block;overflow-x:auto}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

LOGO = ('<svg width="{s}" height="{s}" viewBox="0 0 48 48" aria-hidden="true">'
        '<path d="M24 3C12 3 3 11.6 3 22.5c0 6 2.7 11.3 7 14.9V45l8.2-4.6c1.9.4 3.8.6 5.8.6 12 0 21-8.6 21-19.5S36 3 24 3z" fill="#6a4cf0"/>'
        '<path d="M10 20c0-2 1.6-3.5 3.6-3.5h20.8c2 0 3.6 1.5 3.6 3.5v1.2c0 3.3-2.7 6-6 6-2.5 0-4.1-1.3-5.2-3.2-.5-.8-1.3-1.3-2.8-1.3s-2.300.5-2.800 1.300c-1.100 1.900-2.700 3.200-5.200 3.200-3.300 0-6-2.700-6-6z" fill="#fff"/>'
        '<circle cx="17.200" cy="20.700" r="1.700" fill="#1c1833"/><circle cx="30.800" cy="20.700" r="1.700" fill="#1c1833"/></svg>')

TEMPLATES = {
"base.html": """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Панель{% endblock %} — Анонимные вопросы</title>
{% block refresh %}{% endblock %}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600&family=Unbounded:wght@500;600&display=swap" rel="stylesheet">
<style>{{ css|safe }}</style></head><body>
<div class="shell">
<aside>
  <div class="brand">{{ logo(34)|safe }}<div>Анонимные вопросы<small>панель управления</small></div></div>
  <nav>
    {% set items=[('dashboard','Обзор'),('users','Пользователи'),('questions','Вопросы'),('broadcast','Рассылка')] %}
    {% for ep,label in items %}
      <a href="{{ url_for(ep) }}" class="{{ 'on' if request.endpoint==ep else '' }}">{{ label }}
      {% if ep=='questions' and reports_count %}<span class="badge">{{ reports_count }}</span>{% endif %}</a>
    {% endfor %}
  </nav>
  <div class="side-foot"><a href="{{ url_for('logout') }}">Выйти</a></div>
</aside>
<main>
  {% for cat,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
  {% block body %}{% endblock %}
</main></div></body></html>""",

"login.html": """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход — Анонимные вопросы</title>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600&family=Unbounded:wght@500;600&display=swap" rel="stylesheet">
<style>{{ css|safe }}</style></head><body>
<div class="login"><div class="box">
  {{ logo(76)|safe }}
  <h1>Анонимные вопросы</h1><p class="t">Войдите, чтобы управлять ботом</p>
  <form method="post">
    <input type="hidden" name="_csrf" value="{{ csrf }}">
    {% for cat,m in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}" style="margin:0">{{ m }}</div>{% endfor %}
    <input type="password" name="password" placeholder="Пароль панели" autofocus required>
    <button class="btn" type="submit">Войти</button>
  </form>
</div></div></body></html>""",

"dashboard.html": """{% extends "base.html" %}{% block title %}Обзор{% endblock %}
{% block body %}
<div class="head"><div><h1>Обзор</h1>
<p>Сегодня {{ s.today }} {{ 'вопрос' if s.today==1 else 'вопросов' }} и {{ s.new_today }} новых пользователей</p></div>
<a class="btn" href="{{ url_for('broadcast') }}">Написать рассылку</a></div>
<div class="grid2">
  <section class="card"><h3>Вопросы за 14 дней</h3><p class="sub">Последний столбец — сегодня</p>
    <div class="bars">{% for d in s.chart %}
      <div class="col" title="{{ d.label }}: {{ d.n }}"><b>{{ d.n if d.n else '' }}</b>
      <div class="track"><i style="height:{{ d.pct }}%"></i></div><span>{{ d.label }}</span></div>
    {% endfor %}</div></section>
  <section class="card"><h3>Всего</h3>
    <dl class="kv">
      <div><dt>Пользователей</dt><dd>{{ s.users }}</dd></div>
      <div><dt>Вопросов</dt><dd>{{ s.questions }}</dd></div>
      <div><dt>Доля ответов</dt><dd>{{ s.rate }}%</dd></div>
      <div><dt>В бане</dt><dd>{{ s.banned }}</dd></div>
      <div><dt>Жалобы</dt><dd class="{{ 'bad' if s.reported else '' }}">{{ s.reported }}</dd></div>
    </dl></section>
</div>
<section class="card"><h3>Свежие вопросы</h3><p class="sub">Авторы скрыты — так задумано</p>
{% if recent %}<table style="margin-top:14px"><thead><tr><th>№</th><th>Кому</th><th>Текст</th><th>Статус</th><th></th></tr></thead><tbody>
{% for q in recent %}<tr><td>{{ q.id }}</td><td>{{ q.to_name }}</td>
<td style="max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ q.text }}</td>
<td>{% if q.status=='answered' %}<span class="tag ok">отвечен</span>{% elif q.reported %}<span class="tag bad">жалоба</span>{% else %}<span class="tag warn">ждёт</span>{% endif %}</td>
<td>{{ q.created_at|dt }}</td></tr>{% endfor %}</tbody></table>
{% else %}<div class="empty">Вопросов пока нет. Поделитесь ссылкой на бота.</div>{% endif %}</section>
{% endblock %}""",

"users.html": """{% extends "base.html" %}{% block title %}Пользователи{% endblock %}
{% block body %}
<div class="head"><div><h1>Пользователи</h1><p>Все, кто запускал бота</p></div>
<form class="search" method="get"><input type="text" name="q" value="{{ q }}" placeholder="Имя или VK ID"><button class="btn ghost">Найти</button></form></div>
<section class="card">
{% if rows %}<table><thead><tr><th>Пользователь</th><th>Получил</th><th>Отправил</th><th>Приём</th><th>С нами с</th><th></th></tr></thead><tbody>
{% for u in rows %}<tr class="{{ 'off' if u.banned else '' }}">
<td><a href="https://vk.com/id{{ u.id }}" target="_blank" rel="noopener">{{ u.first_name }} {{ u.last_name }}</a>
<div class="sub" style="color:var(--muted);font-size:12px">id{{ u.id }}</div></td>
<td>{{ u.rec }}</td><td>{{ u.sent }}</td>
<td>{% if u.banned %}<span class="tag bad">бан</span>{% elif u.accepting %}<span class="tag ok">открыт</span>{% else %}<span class="tag warn">выкл</span>{% endif %}</td>
<td>{{ u.created_at|dt }}</td>
<td style="text-align:right"><form method="post" action="{{ url_for('toggle_ban', uid=u.id) }}">
<input type="hidden" name="_csrf" value="{{ csrf }}">
{% if u.banned %}<button class="btn ok sm">Разбанить</button>{% else %}<button class="btn danger sm" onclick="return confirm('Забанить этого пользователя?')">Забанить</button>{% endif %}
</form></td></tr>{% endfor %}</tbody></table>
<div class="pager">{% if page>0 %}<a class="btn ghost sm" href="?q={{ q }}&page={{ page-1 }}">Назад</a>{% endif %}
{% if more %}<a class="btn ghost sm" href="?q={{ q }}&page={{ page+1 }}">Дальше</a>{% endif %}</div>
{% else %}<div class="empty">Никого не нашли.</div>{% endif %}</section>
{% endblock %}""",

"questions.html": """{% extends "base.html" %}{% block title %}Вопросы{% endblock %}
{% block body %}
<div class="head"><div><h1>Вопросы</h1><p>Модерация без раскрытия авторов</p></div></div>
<div class="tabs">
{% for key,label in [('all','Все'),('new','Ждут ответа'),('answered','С ответом'),('reported','Жалобы'),('deleted','Удалённые')] %}
<a href="?f={{ key }}" class="{{ 'on' if f==key else '' }}">{{ label }}</a>{% endfor %}</div>
{% for q in rows %}
<article class="q {{ q.status }} {{ 'rep' if q.reported else '' }}">
  <div class="ava">?</div>
  <div class="qb">
    <div class="meta"><b>Вопрос №{{ q.id }}</b><span>для <a href="https://vk.com/id{{ q.to_id }}" target="_blank" rel="noopener">{{ q.to_name }}</a></span>
      <span>{{ q.created_at|dt }}</span>
      {% if reveal %}<span>автор: id{{ q.from_id }}</span>{% endif %}
      {% if q.reported %}<span class="tag bad">жалоба</span>{% endif %}
      {% if not q.delivered %}<span class="tag warn">не доставлен</span>{% endif %}
      {% if q.status=='deleted' %}<span class="tag">удалён</span>{% endif %}</div>
    <div class="bub">{{ q.text }}</div>
    {% if q.answer %}<div class="bub ans">{{ q.answer }}</div>{% endif %}
    <div class="acts">
      {% if q.status!='deleted' %}<form method="post" action="{{ url_for('q_action', qid=q.id, act='delete') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn ghost sm">Удалить</button></form>{% endif %}
      {% if q.reported %}<form method="post" action="{{ url_for('q_action', qid=q.id, act='dismiss') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn ok sm">Жалоба необоснована</button></form>
      <form method="post" action="{{ url_for('q_action', qid=q.id, act='ban') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="btn danger sm" onclick="return confirm('Забанить автора? Вы не увидите, кто это.')">Забанить автора</button></form>{% endif %}
    </div>
  </div>
</article>
{% else %}<div class="card empty">В этом разделе пусто.</div>{% endfor %}
{% endblock %}""",

"broadcast.html": """{% extends "base.html" %}{% block title %}Рассылка{% endblock %}
{% block refresh %}{% if running %}<meta http-equiv="refresh" content="3">{% endif %}{% endblock %}
{% block body %}
<div class="head"><div><h1>Рассылка</h1><p>Сообщение уйдёт всем, кто не в бане</p></div></div>
<div class="row">
<section class="card"><h3>Новое сообщение</h3>
<form method="post" style="display:grid;gap:14px;margin-top:16px">
<input type="hidden" name="_csrf" value="{{ csrf }}">
<textarea name="text" maxlength="4000" placeholder="Например: «Мы добавили новые функции! Загляните в меню.»" required></textarea>
<label class="chk"><input type="checkbox" name="test" value="1"> Тест: отправить только администраторам из ADMIN_IDS</label>
<div><button class="btn" onclick="return confirm('Запустить рассылку?')">Отправить</button></div>
<p class="hint">Всего получателей: {{ total }}. Отправка идёт пачками, это займёт немного времени.</p>
</form></section>
<section class="card"><h3>История</h3>
{% if rows %}<table style="margin-top:14px"><tbody>
{% for b in rows %}<tr><td><div style="max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ b.text }}</div>
<div class="sub" style="color:var(--muted);font-size:12px">{{ b.created_at|dt }}</div></td>
<td style="min-width:130px">
<div class="bar"><i style="width:{{ (100*(b.sent+b.failed)//b.total) if b.total else 0 }}%"></i></div>
<div style="font-size:12px;color:var(--muted);margin-top:4px">
{% if b.status=='running' %}идёт: {% endif %}{{ b.sent }} ок{% if b.failed %}, {{ b.failed }} ошибок{% endif %} из {{ b.total }}</div></td></tr>{% endfor %}
</tbody></table>{% else %}<div class="empty">Рассылок ещё не было.</div>{% endif %}</section>
</div>
{% endblock %}""",
}


def create_web_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.jinja_env.loader = DictLoader(TEMPLATES)
    app.jinja_env.globals.update(css=CSS, logo=lambda s: LOGO.format(s=s))
    app.add_template_filter(lambda ts: datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—", "dt")

    def login_required(f):
        from functools import wraps

        @wraps(f)
        def wrap(*a, **kw):
            if not session.get("auth"):
                return redirect(url_for("login"))
            return f(*a, **kw)
        return wrap

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            if not hmac.compare_digest(request.form.get("_csrf", "").encode("utf-8"), session.get("csrf", "x").encode("utf-8")):
                abort(400)

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        rc = one("SELECT COUNT(*) c FROM questions WHERE reported=1 AND status!='deleted'")["c"] \
            if session.get("auth") else 0
        return dict(csrf=session["csrf"], reports_count=rc)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode("utf-8"), WEB_PASSWORD.encode("utf-8")):
                session["auth"] = True
                return redirect(url_for("dashboard"))
            time.sleep(1)
            flash("Неверный пароль", "err")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        recent = many("SELECT q.*, (SELECT first_name||' '||last_name FROM users WHERE id=q.to_id) to_name "
                      "FROM questions q WHERE status!='deleted' ORDER BY id DESC LIMIT 8")
        return render_template("dashboard.html", s=get_stats(), recent=recent)

    @app.route("/users")
    @login_required
    def users():
        q = request.args.get("q", "").strip()
        page = max(0, int(request.args.get("page", 0) or 0))
        like = f"%{q}%"
        rows = many("""SELECT u.*,
            (SELECT COUNT(*) FROM questions WHERE to_id=u.id AND status!='deleted') rec,
            (SELECT COUNT(*) FROM questions WHERE from_id=u.id) sent
            FROM users u
            WHERE (?='' OR (first_name||' '||last_name) LIKE ? OR CAST(id AS TEXT)=?)
            ORDER BY created_at DESC LIMIT 51 OFFSET ?""", (q, like, q, page * 50))
        return render_template("users.html", rows=rows[:50], more=len(rows) > 50, q=q, page=page)

    @app.post("/users/<int:uid>/toggle-ban")
    @login_required
    def toggle_ban(uid):
        u = get_user(uid)
        if u:
            execute("UPDATE users SET banned=? WHERE id=?", (0 if u["banned"] else 1, uid))
            flash(("Разбанен: " if u["banned"] else "Забанен: ") + uname(uid), "ok")
        return redirect(request.referrer or url_for("users"))

    @app.route("/questions")
    @login_required
    def questions():
        f = request.args.get("f", "all")
        where = {"all": "status!='deleted'", "new": "status='new'", "answered": "status='answered'",
                 "reported": "reported=1 AND status!='deleted'", "deleted": "status='deleted' AND delivered=1"}.get(f, "1")
        rows = many("SELECT q.*, (SELECT first_name||' '||last_name FROM users WHERE id=q.to_id) to_name "
                    f"FROM questions q WHERE {where} ORDER BY id DESC LIMIT 100")
        return render_template("questions.html", rows=rows, f=f, reveal=SHOW_SENDER_IN_PANEL)

    @app.post("/questions/<int:qid>/<act>")
    @login_required
    def q_action(qid, act):
        q = one("SELECT * FROM questions WHERE id=?", (qid,))
        if not q:
            abort(404)
        if act == "delete":
            execute("UPDATE questions SET status='deleted' WHERE id=?", (qid,))
            flash(f"Вопрос №{qid} удалён", "ok")
        elif act == "dismiss":
            execute("UPDATE questions SET reported=0 WHERE id=?", (qid,))
            flash("Жалоба снята", "ok")
        elif act == "ban":
            execute("UPDATE users SET banned=1 WHERE id=?", (q["from_id"],))
            execute("UPDATE questions SET reported=0, status='deleted' WHERE id=?", (qid,))
            flash("Автор забанен, вопрос удалён. Личность осталась скрытой.", "ok")
        else:
            abort(404)
        return redirect(request.referrer or url_for("questions"))

    @app.route("/broadcast", methods=["GET", "POST"])
    @login_required
    def broadcast():
        if request.method == "POST":
            text = request.form.get("text", "").strip()
            if not text or len(text) > 4000:
                flash("Нужен текст до 4000 символов", "err")
            else:
                start_broadcast(text, only_admins=bool(request.form.get("test")))
                flash("Рассылка запущена", "ok")
            return redirect(url_for("broadcast"))
        rows = many("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 15")
        total = one("SELECT COUNT(*) c FROM users WHERE banned=0")["c"]
        running = any(r["status"] == "running" for r in rows)
        return render_template("broadcast.html", rows=rows, total=total, running=running)

    return app


def run_web():
    app = create_web_app()
    log.info("🌐 Веб-панель: http://%s:%s", WEB_HOST, WEB_PORT)
    import logging as _l
    _l.getLogger("werkzeug").setLevel(_l.WARNING)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False, threaded=True)


# ═════════════════════════════ ЗАПУСК ═════════════════════════════
if __name__ == "__main__":
    init_db()
    if WEB_ENABLED:
        if WEB_PASSWORD == "ИЗМЕНИ_МЕНЯ":
            log.warning("⚠️  Смените WEB_PASSWORD в настройках!")
        threading.Thread(target=run_web, daemon=True).start()

    if TOKEN.startswith("ВСТАВЬ") or GROUP_ID == 123456789:
        log.error("Укажите TOKEN и GROUP_ID в начале файла. Веб-панель при этом работает, бот — нет.")
        if WEB_ENABLED:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        sys.exit(1)

    vk_session = vk_api.VkApi(token=TOKEN, api_version="5.199")
    vk = vk_session.get_api()
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Остановлено")
