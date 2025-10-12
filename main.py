import os
import asyncio
import logging
from html import escape as html_escape
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageEntity
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

# мини-веб-сервер для проверки/пингов (Render/UptimeRobot)
from aiohttp import web

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- приоритеты для корректной вложенности тегов
TAG_PRIORITY = {
    "blockquote": -10,
    "text_link": 0, "text_mention": 0, "url": 0, "email": 0, "mention": 0,  # ссылки — внешние
    "bold": 10, "italic": 11, "underline": 12, "strikethrough": 13, "spoiler": 14,
    "code": 20, "pre": 21,  # моноширинные — самые внутренние
}
DEFAULT_PRIORITY = 15


def etype_str(entity: MessageEntity) -> str:
    return entity.type.value if hasattr(entity.type, "value") else str(entity.type)


def entity_tags(entity: MessageEntity, text: str):
    etype = etype_str(entity)
    start = entity.offset
    end = entity.offset + entity.length
    substr = text[start:end]

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
    """Собирает валидную HTML-вложенность с учётом длин и приоритетов."""
    if not text:
        return ""
    entities = entities or []
    starts = defaultdict(list)  # pos -> list[(key1, key2, open_tag)]
    ends = defaultdict(list)    # pos -> list[(key1, key2, close_tag)]

    for e in entities:
        open_tag, close_tag, etype = entity_tags(e, text)
        if not open_tag:
            continue
        start = e.offset
        end = e.offset + e.length
        length = e.length
        pr = tag_priority(etype)
        # Открывать внешние раньше (длиннее, меньший pr), закрывать внутренние раньше
        starts[start].append((-length, pr, open_tag))
        ends[end].append((length, -pr, close_tag))

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


# --- простой web-сервер для health-check (возвращает "ok" на "/")
async def _health(request):
    return web.Response(text="ok")


async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", _health)
    # Render задаёт порт в переменной PORT — обязаны слушать его
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Keep-alive web server started on port {port}")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

    bot = Bot(BOT_TOKEN, parse_mode=None)  # отправляем «сырой» HTML-код
    dp = Dispatcher()

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

    # параллельно поднимаем веб-сервер и запускаем polling
    await asyncio.gather(
        start_keepalive_server(),
        dp.start_polling(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())
