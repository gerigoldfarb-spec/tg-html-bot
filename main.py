import os
import asyncio
import logging
from html import escape as html_escape
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageEntity
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv
from aiohttp import web  # мини-веб для health-check / keep-alive

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- приоритеты для корректной вложенности (внешние меньше)
TAG_PRIORITY = {
    "blockquote": -10,
    "text_link": 0, "text_mention": 0, "url": 0, "email": 0, "mention": 0,  # ссылки — внешние
    "bold": 10, "italic": 11, "underline": 12, "strikethrough": 13, "spoiler": 14,
    "code": 20, "pre": 21,  # моноширинные — наиболее внутренние
}
DEFAULT_PRIORITY = 15

def etype_str(e: MessageEntity) -> str:
    return e.type.value if hasattr(e.type, "value") else str(e.type)

# --- КЛЮЧЕВОЕ: перевод позиции из UTF-16 code units в индекс Python-строки
def utf16_units_to_py_index(s: str, unit_pos: int) -> int:
    """
    Возвращает индекс в Python-строке, соответствующий позиции unit_pos в UTF-16 code units.
    Эмодзи/символы вне BMP занимают 2 юнита, обычные — 1.
    """
    if unit_pos <= 0:
        return 0
    u = 0
    i = 0
    n = len(s)
    while i < n and u < unit_pos:
        cp = ord(s[i])
        u += 2 if cp > 0xFFFF else 1
        i += 1
    return i  # может быть == len(s), что нормально для концов выделений

def entity_tags(entity: MessageEntity, text: str, start_py: int, end_py: int):
    """Подбираем открывающий/закрывающий тег и (при необходимости) собираем href из подстроки."""
    etype = etype_str(entity)
    substr = text[start_py:end_py]

    if etype == "bold":            return "<b>", "</b>", etype
    if etype == "italic":          return "<i>", "</i>", etype
    if etype == "underline":       return "<u>", "</u>", etype
    if etype == "strikethrough":   return "<s>", "</s>", etype
    if etype == "spoiler":         return '<span class="tg-spoiler">', "</span>", etype
    if etype == "code":            return "<code>", "</code>", etype
    if etype == "pre":
        lang = getattr(entity, "language", None)
        if lang:                   return f'<pre><code class="language-{html_escape(lang)}">', "</code></pre>", etype
        return "<pre>", "</pre>", etype
    if etype == "text_link" and getattr(entity, "url", None):
        return f'<a href="{html_escape(entity.url)}">', "</a>", etype
    if etype == "text_mention" and getattr(entity, "user", None):
        return f'<a href="tg://user?id={entity.user.id}">', "</a>", etype
    if etype == "url":             return f'<a href="{html_escape(substr)}">', "</a>", etype
    if etype == "email":           return f'<a href="mailto:{html_escape(substr)}">', "</a>", etype
    if etype == "mention":
        username = substr[1:] if substr.startswith("@") else substr
        return f'<a href="https://t.me/{html_escape(username)}">', "</a>", etype
    if etype == "blockquote":      return "<blockquote>", "</blockquote>", etype
    return None, None, etype

def tag_priority(etype: str) -> int:
    return TAG_PRIORITY.get(etype, DEFAULT_PRIORITY)

def to_telegram_html(text: str, entities: list[MessageEntity] | None) -> str:
    """
    Собирает валидную HTML-вложенность.
    ВАЖНО: конвертирует offsets из UTF-16 units в индексы Python.
    """
    if not text:
        return ""

    entities = entities or []
    starts = defaultdict(list)  # pos_py -> list[(key1, key2, open_tag)]
    ends   = defaultdict(list)  # pos_py -> list[(key1, key2, close_tag)]

    for e in entities:
        # Telegram даёт offset/length в UTF-16 units
        start_py = utf16_units_to_py_index(text, e.offset)
        end_py   = utf16_units_to_py_index(text, e.offset + e.length)

        open_tag, close_tag, etype = entity_tags(e, text, start_py, end_py)
        if not open_tag:
            continue

        length_py = end_py - start_py
        pr = tag_priority(etype)

        # Открывать внешние раньше (длиннее / меньший pr), закрывать внутренние раньше
        starts[start_py].append((-length_py, pr, open_tag))
        ends[end_py].append((length_py, -pr, close_tag))

    # сортировки
    for pos in starts: starts[pos].sort()
    for pos in ends:   ends[pos].sort()

    out = []
    n = len(text)
    for i in range(n + 1):
        if i in ends:
            for _, _, tag in ends[i]:
                out.append(tag)
        if i in starts:
            for _, _, tag in starts[i]:
                out.append(tag)
        if i < n:
            out.append(html_escape(text[i]))
    return "".join(out)

# --- health-check endpoint (для рендер/пингов)
async def _health(request):  # GET /
    return web.Response(text="ok")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", _health)
    port = int(os.getenv("PORT", "10000"))  # Render кладёт порт в переменную PORT
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Keep-alive web server started on port {port}")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

    bot = Bot(BOT_TOKEN, parse_mode=None)  # отправляем «сырой» HTML-код
    dp  = Dispatcher()

    @dp.message(CommandStart())
    async def start_cmd(m: Message):
        await m.answer("Пришли сообщение/пересланный пост — верну строку в Telegram-HTML.")

    @dp.message(Command("help"))
    async def help_cmd(m: Message):
        await m.answer("Отправь текст с форматированием — отвечу строкой с HTML-тегами.")

    @dp.message(F.text)
    async def handle_text(m: Message):
        html_str = to_telegram_html(m.text, m.entities)
        await m.answer(html_str or html_escape(m.text or ""), disable_web_page_preview=True)

    @dp.message(F.caption)
    async def handle_caption(m: Message):
        html_str = to_telegram_html(m.caption, m.caption_entities)
        await m.answer(html_str or html_escape(m.caption or ""), disable_web_page_preview=True)

    await asyncio.gather(
        start_keepalive_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
