import os
import asyncio
import logging
from html import escape as html_escape
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, MessageEntity
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv
from aiohttp import web  # health-check / keep-alive

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- приоритеты для корректной вложенности (меньше = "внешнее")
TAG_PRIORITY = {
    "blockquote": -10,
    "text_link": 0, "text_mention": 0, "url": 0, "email": 0, "mention": 0,  # ссылки — внешние
    "bold": 10, "italic": 11, "underline": 12, "strikethrough": 13,
    "spoiler": 14,
    "code": 20, "pre": 21,  # моноширинные — самые внутренние
}
DEFAULT_PRIORITY = 15
MERGEABLE = {"bold", "italic", "underline", "strikethrough"}

def etype_str(e: MessageEntity) -> str:
    return e.type.value if hasattr(e.type, "value") else str(e.type)

# UTF-16 code units -> индекс Python
def utf16_units_to_py_index(s: str, unit_pos: int) -> int:
    if unit_pos <= 0:
        return 0
    u = 0
    i = 0
    n = len(s)
    while i < n and u < unit_pos:
        cp = ord(s[i])
        u += 2 if cp > 0xFFFF else 1
        i += 1
    return i

def basic_tags_for_type(etype: str, *, href: str | None = None, user_id: int | None = None):
    if etype == "bold":          return "<b>", "</b>"
    if etype == "italic":        return "<i>", "</i>"
    if etype == "underline":     return "<u>", "</u>"
    if etype == "strikethrough": return "<s>", "</s>"
    if etype == "spoiler":       return '<span class="tg-spoiler">', "</span>"
    if etype == "code":          return "<code>", "</code>"
    if etype == "pre":           return "<pre>", "</pre>"
    if etype == "text_link" and href:
        return f'<a href="{html_escape(href)}">', "</a>"
    if etype == "text_mention" and user_id:
        return f'<a href="tg://user?id={user_id}">', "</a>"
    if etype == "url" and href:
        return f'<a href="{html_escape(href)}">', "</a>"
    if etype == "email" and href:
        return f'<a href="mailto:{html_escape(href)}">', "</a>"
    if etype == "mention" and href:
        return f'<a href="https://t.me/{html_escape(href)}">', "</a>"
    if etype == "blockquote":    return "<blockquote>", "</blockquote>"
    return None, None

def tag_priority(etype: str) -> int:
    return TAG_PRIORITY.get(etype, DEFAULT_PRIORITY)

def build_raw_spans(text: str, entities: list[MessageEntity] | None):
    if not entities:
        return []
    spans = []
    for e in entities:
        etype = etype_str(e)
        start = utf16_units_to_py_index(text, e.offset)
        end   = utf16_units_to_py_index(text, e.offset + e.length)
        if start >= end or start < 0 or end > len(text):
            continue

        # подготовка href/username + очистка whitespace
        href = None
        user_id = None
        substr = text[start:end]
        if etype == "text_link" and getattr(e, "url", None):
            href = e.url
        elif etype == "text_mention" and getattr(e, "user", None):
            user_id = e.user.id
        elif etype == "url":
            href = "".join(substr.split())  # убираем переносы/пробелы
        elif etype == "email":
            href = "".join(substr.split())
        elif etype == "mention":
            username = substr.strip()
            if username.startswith("@"):
                username = username[1:]
            href = username

        # выкидываем полностью пробельные куски для mergeable-типов (убирает <b> </b>)
        if etype in MERGEABLE and substr.strip() == "":
            continue

        open_tag, close_tag = basic_tags_for_type(etype, href=href, user_id=user_id)
        if not open_tag:
            continue

        spans.append({
            "id": id(e),                   # уникальный идентификатор
            "type": etype,
            "start": start,
            "end": end,
            "open": open_tag,
            "close": close_tag,
            "priority": tag_priority(etype),
        })
    return spans

def merge_mergeable_spans(text: str, spans: list[dict]) -> list[dict]:
    by_type: dict[str, list[dict]] = {t: [] for t in MERGEABLE}
    others: list[dict] = []
    for s in spans:
        (by_type if s["type"] in MERGEABLE else others).setdefault(s["type"] if s["type"] in MERGEABLE else "others", []).append(s)
    # ↑ setdefault нужен потому, что others — не dict по типам; исправим на явное разделение:
    by_type = {t: [] for t in MERGEABLE}
    others = []
    for s in spans:
        if s["type"] in MERGEABLE:
            by_type[s["type"]].append(s)
        else:
            others.append(s)

    merged: list[dict] = []
    for t in MERGEABLE:
        arr = by_type[t]
        if not arr:
            continue
        arr.sort(key=lambda x: (x["start"], x["end"]))
        cur = arr[0]
        for nxt in arr[1:]:
            gap = text[cur["end"]:nxt["start"]]
            if gap != "" and not gap.isspace():
                merged.append(cur)
                cur = nxt
            else:
                cur["end"] = max(cur["end"], nxt["end"])  # включаем пробелы и сливаем
        merged.append(cur)
    return merged + others

def to_telegram_html(text: str, entities: list[MessageEntity] | None) -> str:
    if not text:
        return ""

    # сырые интервалы + фиксы
    spans = merge_mergeable_spans(text, build_raw_spans(text, entities))

    # индексы старт/финиш
    starts = defaultdict(list)  # pos -> [(sort_key, span_id)]
    ends   = defaultdict(list)
    by_id  = {}

    for s in spans:
        by_id[s["id"]] = s
        length = s["end"] - s["start"]
        pr = s["priority"]
        starts[s["start"]].append(((-length, pr), s["id"]))   # внешние раньше
        ends[s["end"]].append(((length, -pr), s["id"]))       # внутренние раньше

    for pos in starts: starts[pos].sort()
    for pos in ends:   ends[pos].sort()

    out = []
    open_stack: list[int] = []  # stack of span_ids
    n = len(text)

    def close_until(span_id: int):
        """Закрыть всё сверху до указанного span_id, закрыть его, затем переоткрыть снятые."""
        reopen = []
        while open_stack and open_stack[-1] != span_id:
            sid = open_stack.pop()
            out.append(by_id[sid]["close"])
            reopen.append(sid)
        if open_stack and open_stack[-1] == span_id:
            open_stack.pop()
            out.append(by_id[span_id]["close"])
        for sid in reversed(reopen):
            out.append(by_id[sid]["open"])
            open_stack.append(sid)

    for i in range(n + 1):
        # закрытия на позиции i — по глубине в текущем стеке (самые глубокие сперва)
        if i in ends:
            to_close = [sid for _, sid in ends[i]]
            while to_close:
                # берём тот, что глубже всех в стеке
                idx_sid = [(open_stack.index(sid), sid) for sid in to_close if sid in open_stack]
                if not idx_sid:
                    break
                _, deepest = max(idx_sid, key=lambda x: x[0])
                close_until(deepest)
                to_close.remove(deepest)

        # открытия на позиции i — по нашему сортировочному ключу
        if i in starts:
            for _, sid in starts[i]:
                out.append(by_id[sid]["open"])
                open_stack.append(sid)

        if i < n:
            out.append(html_escape(text[i]))

    # на всякий случай закрываем то, что осталось (не должно, но безопасно)
    while open_stack:
        out.append(by_id[open_stack.pop()]["close"])

    return "".join(out)

# --- health-check
async def _health(request):
    return web.Response(text="ok")

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", _health)
    port = int(os.getenv("PORT", "10000"))  # Render задаёт порт
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Keep-alive web server started on port {port}")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

    bot = Bot(BOT_TOKEN, parse_mode=None)  # шлём «сырой» HTML
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
