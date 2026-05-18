import os
import asyncio
import re
import socks
import logging
import ollama
import json  # Добавили для работы с файлом
import base64
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from telethon import TelegramClient
from rapidfuzz import fuzz

logging.basicConfig(level=logging.INFO)
load_dotenv()

# Настройки из .env
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Твои основные каналы
BASE_SOURCES = [
    # Федеральные
    '@rian_ru', '@rbc_news', '@tass_agency', '@interfax_russia',
    '@kommersant', '@izvestia', '@gazeta_ru',
    # Агрегаторы/быстрые
    '@mash', '@readovkanews', '@toporlive', '@moscowach',
    '@smi_rf_moskva', '@novosti_efir', '@shot_shot',
    # Спорт
    '@news_matchtv', '@championat', '@sport_express_news',
    '@russiafootball', '@ftbl',
    # Общественно-политические
    '@fontanka_spb', '@the_insider_russia', '@baza_plus',
]


# --- НОВАЯ ЛОГИКА ЗАГРУЗКИ РЕГИОНОВ ---
def load_regions():
    try:
        with open('regions.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logging.error("Файл regions.json не найден!")
        return {}


REGIONAL_SOURCES = load_regions()
SETTINGS_FILE = 'user_settings.json'

def load_settings():
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return {int(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}

def save_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False)

user_settings = load_settings()

# Настройки прокси
PROXY_HOST = '127.0.0.1'
SOCKS_PORT = 10808
HTTP_PORT = 10809

telethon_client = None
session = AiohttpSession(proxy=f"http://{PROXY_HOST}:{HTTP_PORT}")
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()


# --- Поиск и верификация (без изменений) ---

async def ai_verify_news(original_text, candidate_text):
    def clean(t):
        return re.sub(r'http\S+|@\S+', '', t).strip()[:600]

    prompt = f"""СРАВНИ ДВЕ НОВОСТИ. 
    ВНИМАНИЕ: Если в новостях указаны РАЗНЫЕ ГОРОДА или РЕГИОНЫ (например, Туапсе и Кировский район), отвечай НЕТ.
    Отвечай ДА, только если это одно и то же событие.

    Текст 1: {clean(original_text)}
    Текст 2: {clean(candidate_text)}

    Ответ (ДА или НЕТ):"""
    try:
        response = await asyncio.to_thread(ollama.chat, model='llama3',
                                           messages=[{'role': 'user', 'content': prompt}],
                                           options={'temperature': 0})
        ans = response['message']['content'].strip().upper()
        return ans.startswith("ДА") or "YES" in ans[:5]
    except Exception as e:
        return False


async def ai_verify_with_image(original_text, candidate_text, image_bytes=None):
    def clean(t):
        return re.sub(r'http\S+|@\S+', '', t).strip()[:400]

    prompt = f"""Это одна и та же новость? Отвечай только ДА или НЕТ.
Новость 1: {clean(original_text)}
Новость 2: {clean(candidate_text)}"""

    try:
        if image_bytes:
            import base64
            img_b64 = base64.b64encode(image_bytes).decode()
            response = await asyncio.to_thread(
                ollama.chat,
                model='llava',
                messages=[{
                    'role': 'user',
                    'content': prompt,
                    'images': [img_b64]
                }],
                options={'temperature': 0}
            )
        else:
            response = await asyncio.to_thread(
                ollama.chat, model='llama3',
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': 0}
            )
        ans = response['message']['content'].strip().upper()
        return ans.startswith("ДА") or "YES" in ans[:5]
    except Exception as e:
        logging.error(f"AI verify error: {e}")
        return False


GLOBAL_THRESHOLD = 65   # Для федеральных СМИ — строже
REGIONAL_THRESHOLD = 55  # Для региональных — мягче

async def search_in_channels(original_text, channels_to_search, regional_channels=None):
    found_info = []
    regional_set = set(regional_channels or [])

    words = re.sub(r'[^\w\s]', '', original_text).split()

    STOP_WORDS = {
        'в', 'на', 'из', 'по', 'за', 'от', 'до', 'при', 'с', 'к', 'о', 'и',
        'а', 'но', 'не', 'что', 'как', 'это', 'все', 'ко', 'целях', 'целью',
        'после', 'также', 'уже', 'ещё', 'всех', 'всем', 'всё'
    }
    meaningful = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 3]
    # Берём до 6 слов для точности, но не более 50 символов итого
    query_words = meaningful[:6]
    short_query = " ".join(query_words)[:50] if query_words else original_text[:40]

    for channel in channels_to_search:
        is_regional = channel in regional_set
        threshold = REGIONAL_THRESHOLD if is_regional else GLOBAL_THRESHOLD

        try:
            messages = []
            async for m in telethon_client.iter_messages(channel, search=short_query, limit=5):
                messages.append(m)
            if not messages:
                async for m in telethon_client.iter_messages(channel, limit=15):
                    messages.append(m)

            for message in messages:
                msg_text = message.text
                if not msg_text or len(msg_text) < 20:
                    continue

                score = fuzz.token_set_ratio(original_text, msg_text)
                if score >= threshold:
                    found_info.append({
                        'ch': channel,
                        'score': score,
                        'text': msg_text,
                        'link': f"https://t.me/{channel.lstrip('@')}/{message.id}",
                        'is_regional': is_regional
                    })

        except Exception as e:
            logging.error(f"Ошибка в канале {channel}: {e}")

    best_results = {}
    for r in found_info:
        ch = r['ch']
        if ch not in best_results or r['score'] > best_results[ch]['score']:
            best_results[ch] = r

    return list(best_results.values())

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    builder = ReplyKeyboardBuilder()
    builder.button(
        text="🚀 Открыть NewsChecker",
        web_app=WebAppInfo(url="https://edvart08.github.io/news-checker-app/?v=2")
    )
    builder.button(text="❓ Инструкция")
    builder.adjust(1)

    welcome_text = (
        "<b>👋 Добро пожаловать в обновленный NewsChecker!</b>\n"
        "<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        "Теперь проверять новости можно в удобном и красивом интерфейсе приложения.\n\n"
        "Нажмите кнопку <b>🚀 Открыть NewsChecker</b> ниже, чтобы начать."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=builder.as_markup(resize_keyboard=True))


# 1. НОВЫЙ: Хендлер для Инструкции (должен быть ВЫШЕ общего handle_news)
@dp.message(lambda message: message.text and message.text == "❓ Инструкция")
async def cmd_instruction(message: types.Message):
    inst_text = (
        "<b>📖 Инструкция:</b>\n\n"
        "1️⃣ <b>Регион:</b> Нажмите «Выбрать регион», чтобы бот знал, где искать местные новости.\n"
        "2️⃣ <b>Проверка:</b> Пришлите текст новости. Бот найдет похожие посты в СМИ.\n"
        "3️⃣ <b>Результат:</b>\n"
        "🟢 80-100% — новость подтверждена.\n"
        "🟡 50-70% — есть похожие события, но детали могут отличаться.\n"
        "🔴 0-40% — подтверждений не найдено."
    )
    await message.answer(inst_text, parse_mode="HTML")


@dp.message(lambda message: message.text and message.text == "📍 Выбрать регион")
async def show_regions(message: types.Message):
    builder = ReplyKeyboardBuilder()
    for reg in REGIONAL_SOURCES.keys():
        builder.button(text=f"Регион: {reg.capitalize()}")
    builder.adjust(2)
    await message.answer("Выберите ваш регион из списка:", reply_markup=builder.as_markup(resize_keyboard=True))


@dp.message(lambda message: message.text and (message.text.startswith("Регион: ") or message.text.startswith("/region")))
async def set_region_smart(message: types.Message):
    if message.text.startswith("/region"):
        args = message.text.split(maxsplit=1)
        region_raw = args[1] if len(args) > 1 else ""
    else:
        region_raw = message.text.replace("Регион: ", "")

    region = region_raw.strip().lower()

    if region in REGIONAL_SOURCES:
        user_settings[message.from_user.id] = region
        save_settings(user_settings)
        builder = ReplyKeyboardBuilder()
        builder.button(text="📍 Выбрать регион")
        builder.button(text="❓ Инструкция")
        await message.answer(f"✅ Настройка завершена!\nТекущий регион: <b>{region.capitalize()}</b>",
                             parse_mode="HTML", reply_markup=builder.as_markup(resize_keyboard=True))
    else:
        await message.answer(f"❌ Регион '{region_raw}' не найден.")


# Хендлер данных из WebApp (tg.sendData вызывает именно это)
@dp.message(lambda message: message.web_app_data is not None)
async def handle_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        raw_text = data.get("text", "").strip()
    except Exception:
        raw_text = message.web_app_data.data.strip()

    if len(raw_text) < 25:
        return await message.answer("⚠️ <b>Текст слишком короткий</b>", parse_mode="HTML")

    user_id = message.from_user.id
    user_region = user_settings.get(user_id)
    regional_channels = REGIONAL_SOURCES.get(user_region, []) if user_region else []
    current_sources = BASE_SOURCES.copy() + regional_channels

    status = await message.answer("⏳ <b>Ищу упоминания в СМИ...</b>", parse_mode="HTML")

    try:
        results = await search_in_channels(raw_text, current_sources, regional_channels=regional_channels)

        if not results:
            return await status.edit_text(
                "❌ <b>Подтверждений не найдено</b>\nВ базе СМИ нет похожих новостей.",
                parse_mode="HTML"
            )

        await status.edit_text("🧠 <b>Нейросеть проверяет смысл...</b>", parse_mode="HTML")

        async def verify_wrapper(r):
            if r['score'] >= 90:
                return r
            try:
                verified = await ai_verify_with_image(raw_text, r['text'])
                return r if verified else None
            except Exception:
                return r

        checked = await asyncio.gather(*(verify_wrapper(r) for r in results))
        final = [r for r in checked if r is not None]

        if not final:
            return await status.edit_text(
                "⚠️ <b>Новости найдены, но смысл отличается</b>\nПохоже, это другое событие.",
                parse_mode="HTML"
            )

        final.sort(key=lambda x: x['score'], reverse=True)
        top_score = min(int(final[0]['score']), 100)

        # Кодируем результаты для передачи в мини-апп
        sources_compact = [
            {"c": r["ch"], "s": min(int(r["score"]), 100), "l": r["link"]}
            for r in final[:8]
        ]
        payload = base64.b64encode(json.dumps(
            {"sources": sources_compact, "top": top_score}, ensure_ascii=False
        ).encode()).decode()

        webapp_url = f"https://edvart08.github.io/news-checker-app/?v=2&r={payload}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"📊 Открыть результаты ({len(final)} источника)",
                web_app=WebAppInfo(url=webapp_url)
            )
        ]])

        bar = "🟩" * (top_score // 10) + "⬜" * (10 - top_score // 10)
        await status.edit_text(
            f"✅ <b>Найдено {len(final)} подтверждений!</b>\n"
            f"<b>Достоверность: {top_score}%</b>  [{bar}]\n\n"
            f"Нажмите кнопку ниже, чтобы открыть источники в приложении.",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:
        logging.error(f"Ошибка в handle_web_app_data: {e}")
        await status.edit_text("❌ <b>Произошла ошибка при анализе</b>", parse_mode="HTML")


# 2. ИЗМЕНЕННЫЙ: Основной хендлер теперь игнорирует системные кнопки
@dp.message(lambda message:
    (message.text or message.caption) and
    not (message.text or "").startswith(("📍", "❓", "Регион:", "/"))
)
async def handle_news(message: types.Message):
    raw_text = (message.text or message.caption or "")[:1000]

    if len(raw_text) < 25:
        return await message.answer("⚠️ <b>Текст слишком короткий</b>", parse_mode="HTML")

    # Фото
    image_bytes = None
    if message.photo:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()

    user_id = message.from_user.id
    user_region = user_settings.get(user_id)
    regional_channels = REGIONAL_SOURCES.get(user_region, []) if user_region else []
    # Исправлено: больше нет дублирования
    current_sources = BASE_SOURCES.copy() + regional_channels

    status = await message.answer("⏳ <b>Ищу упоминания в СМИ...</b>", parse_mode="HTML")

    try:
        results = await search_in_channels(raw_text, current_sources, regional_channels=regional_channels)

        if not results:
            return await status.edit_text(
                "❌ <b>Подтверждений не найдено</b>\nВ базе СМИ нет похожих новостей.",
                parse_mode="HTML"
            )

        await status.edit_text("🧠 <b>Нейросеть проверяет смысл...</b>", parse_mode="HTML")

        # Единственный verify_wrapper с fallback если AI недоступна
        async def verify_wrapper(r):
            if r['score'] >= 90:
                return r
            try:
                verified = await ai_verify_with_image(raw_text, r['text'], image_bytes)
                return r if verified else None
            except Exception:
                # Если AI недоступна — доверяем fuzzy-порогу
                return r

        checked = await asyncio.gather(*(verify_wrapper(r) for r in results))
        final = [r for r in checked if r is not None]

        if not final:
            return await status.edit_text(
                "⚠️ <b>Новости найдены, но смысл отличается</b>\nПохоже, это другое событие.",
                parse_mode="HTML"
            )

        final.sort(key=lambda x: x['score'], reverse=True)
        top_score = min(int(final[0]['score']), 100)
        bar = "🟩" * (top_score // 10) + "⬜" * (10 - top_score // 10)

        sources_links = ""
        for r in final:
            emoji = "🟢" if r['score'] >= 80 else "🟡"
            sources_links += f"{emoji} <a href='{r['link']}'>{r['ch']}</a> — <b>{int(r['score'])}%</b>\n"

        await status.edit_text(
            f"📑 <b>ОТЧЕТ О ВЕРИФИКАЦИИ</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n\n"
            f"<b>Достоверность: {top_score}%</b>\n"
            f"[{bar}]\n\n"
            f"✅ <b>Найдено подтверждений: {len(final)}</b>\n"
            f"{sources_links}\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"👉 <i>Нажмите на источник, чтобы прочитать оригинал.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        logging.error(f"Ошибка в handle_news: {e}")
        await status.edit_text("❌ <b>Произошла ошибка при анализе</b>", parse_mode="HTML")


async def main():
    global telethon_client
    proxy = (socks.SOCKS5, PROXY_HOST, SOCKS_PORT)
    telethon_client = TelegramClient('user_session', API_ID, API_HASH, proxy=proxy)
    await telethon_client.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())