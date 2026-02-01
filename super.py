#!/usr/bin/env python3
"""
Телеграм-бот для приема скринов профиля Kleinanzeigen и OCR-анализа.
Извлекает дату регистрации (Aktiv seit) и другие поля, затем ищет ближайшую
дату регистрации в базе user_id+date (txt).
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import requests
from PIL import Image
import pytesseract


# === ПРИНУДИТЕЛЬНАЯ УСТАНОВКА ПУТИ TESSERACT ===
pytesseract.pytesseract.tesseract_cmd = r"D:\sof1\tesseract.exe"

# === CONFIG (редактируй здесь) ===
TELEGRAM_BOT_TOKEN = "8501651034:AAH2eoMiqnH05kNJlLf291ru_0aMRLRwmJk"
TELEGRAM_CHAT_IDS = [
    "6478058170",
    "5583690035",
]
POLL_SECONDS = 5  # увеличен с 2
DB_FILE = "known_accounts.txt"  # строки вида: user_id;dd.mm.yyyy
TESSERACT_CMD = r"D:\sof1\tesseract.exe"  # путь к tesseract.exe
REQUEST_TIMEOUT = 60  # увеличен с 20

BADGE_KEYWORDS = [
    "TOP Zufriedenheit",
    "OK Zufriedenheit",
    "Besonders freundlich",
    "Besonders zuverlässig",
    "Sehr freundlich",
    "Sehr zuverlässig",
    "Freundlich",
    "Zuverlässig",
    "Naja",
]

IGNORE_NAME_KEYWORDS = [
    "Privater Nutzer",
    "Aktiv seit",
    "Anzeigen online",
    "gesamt",
    "Verkaufsübersicht",
    "Antwortet",
    "Follower",
    "Folge ich",
    "Sicher bezahlen",
]


@dataclass
class ParsedProfile:
    name: Optional[str]
    aktiv_seit: Optional[str]
    online_ads: Optional[int]
    total_ads: Optional[int]
    followers: Optional[int]
    badges: list[str]


def _parse_date(date_str: str) -> Optional[str]:
    date_str = date_str.strip().replace("/", ".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return None


def _to_ordinal(date_str: str) -> int:
    return datetime.strptime(date_str, "%d.%m.%Y").toordinal()


def load_known_accounts(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if ";" not in line:
            continue
        user_id, date_str = [part.strip() for part in line.split(";", 1)]
        normalized = _parse_date(date_str)
        if user_id and normalized:
            rows.append((user_id, normalized))
    return rows


def add_known_account(path: Path, user_id: str, date_str: str) -> None:
    normalized = _parse_date(date_str)
    if not normalized:
        return
    entry = f"{user_id};{normalized}"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if entry in existing:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")


def find_nearest_account(
    target_date: str, entries: Iterable[tuple[str, str]]
) -> Optional[tuple[str, str]]:
    target_ord = _to_ordinal(target_date)
    best: Optional[tuple[str, str]] = None
    best_delta: Optional[int] = None
    for user_id, date_str in entries:
        delta = abs(_to_ordinal(date_str) - target_ord)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = (user_id, date_str)
    return best


def ocr_image(image_bytes: bytes) -> str:
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    image = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(image, lang="deu+eng")


def extract_profile(text: str) -> ParsedProfile:
    aktiv_match = re.search(
        r"Aktiv\s*seit\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})",
        text,
        re.IGNORECASE,
    )
    aktiv_seit = _parse_date(aktiv_match.group(1)) if aktiv_match else None

    online_total_match = re.search(
        r"(\d+)\s+Anzeigen\s+online\s*/\s*(\d+)\s+gesamt",
        text,
        re.IGNORECASE,
    )
    online_ads = None
    total_ads = None
    if online_total_match:
        online_ads = int(online_total_match.group(1))
        total_ads = int(online_total_match.group(2))
    else:
        online_only = re.search(
            r"(\d+)\s+Anzeigen\s+online",
            text,
            re.IGNORECASE,
        )
        if online_only:
            online_ads = int(online_only.group(1))

    followers_match = re.search(r"(\d+)\s+Follower", text, re.IGNORECASE)
    followers = int(followers_match.group(1)) if followers_match else None

    badges = [badge for badge in BADGE_KEYWORDS if badge.lower() in text.lower()]

    name = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(keyword.lower() in line.lower() for keyword in IGNORE_NAME_KEYWORDS):
            continue
        if len(line) < 2:
            continue
        name = line
        break

    return ParsedProfile(
        name=name,
        aktiv_seit=aktiv_seit,
        online_ads=online_ads,
        total_ads=total_ads,
        followers=followers,
        badges=badges,
    )


def send_message(chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка отправки сообщения: {e}")


def download_file(file_id: str) -> bytes:
    meta_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    try:
        meta = requests.get(meta_url, params={"file_id": file_id}, timeout=REQUEST_TIMEOUT)
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        response = requests.get(file_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка загрузки файла: {e}")
        raise


def format_summary(parsed: ParsedProfile) -> str:
    lines = [
        "Распознанные данные:",
        f"Имя: {parsed.name or 'не найдено'}",
        f"Дата регистрации: {parsed.aktiv_seit or 'не найдена'}",
        f"Объявления онлайн: {parsed.online_ads if parsed.online_ads is not None else 'не найдено'}",
        f"Всего объявлений: {parsed.total_ads if parsed.total_ads is not None else 'не найдено'}",
        f"Подписчики: {parsed.followers if parsed.followers is not None else 'не найдено'}",
        f"Плашки: {', '.join(parsed.badges) if parsed.badges else 'не найдены'}",
    ]
    return "\n".join(lines)


def handle_photo(chat_id: str, file_id: str) -> None:
    try:
        image_bytes = download_file(file_id)
        ocr_text = ocr_image(image_bytes)
        parsed = extract_profile(ocr_text)
        summary = format_summary(parsed)

        send_message(chat_id, summary)

        if not parsed.aktiv_seit:
            send_message(chat_id, "Не удалось определить дату регистрации на скрине.")
            return

        db_path = Path(DB_FILE)
        entries = load_known_accounts(db_path)
        if not entries:
            send_message(
                chat_id,
                "База пустая. Добавь запись вида user_id;dd.mm.yyyy в known_accounts.txt.",
            )
            return

        nearest = find_nearest_account(parsed.aktiv_seit, entries)
        if not nearest:
            send_message(chat_id, "Не удалось найти ближайший аккаунт в базе.")
            return

        user_id, date_str = nearest
        send_message(
            chat_id,
            (
                "Ближайшая запись в базе:\n"
                f"user_id: {user_id}\n"
                f"Дата: {date_str}\n"
                "Эти данные будут стартовой точкой для дальнейшего поиска."
            ),
        )
    except Exception as e:
        print(f"⚠️ Ошибка обработки фото: {e}")
        send_message(chat_id, f"Ошибка обработки фото: {e}")


def handle_command(chat_id: str, text: str) -> None:
    if text.startswith("/add "):
        parts = text.split()
        if len(parts) < 3:
            send_message(chat_id, "Формат: /add <user_id> <dd.mm.yyyy>")
            return
        user_id = parts[1]
        date_str = parts[2]
        try:
            add_known_account(Path(DB_FILE), user_id, date_str)
            send_message(chat_id, f"Добавлено в базу: {user_id};{date_str}")
        except Exception as e:
            send_message(chat_id, f"Ошибка добавления: {e}")


def get_updates(offset: Optional[int]) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 60}  # увеличен long-polling таймаут
    if offset is not None:
        params["offset"] = offset
    
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ReadTimeout:
        print("⚠️ Таймаут при получении обновлений. Продолжаю работу...")
        return {"result": []}
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка соединения: {e}")
        return {"result": []}


def main() -> None:
    allowed = set(TELEGRAM_CHAT_IDS)
    offset = None
    error_count = 0
    
    print("🤖 Бот запущен и ожидает скриншоты...")
    print(f"📁 База данных: {DB_FILE}")
    print(f"🔧 Tesseract путь: {TESSERACT_CMD}")
    print("📸 Отправляйте скриншоты профилей Kleinanzeigen в Telegram")

    while True:
        try:
            updates = get_updates(offset)
            
            if "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue
                    
                    chat_id = str(message["chat"]["id"])
                    if chat_id not in allowed:
                        continue
                    
                    if "text" in message:
                        handle_command(chat_id, message["text"])
                    
                    if "photo" in message:
                        file_id = message["photo"][-1]["file_id"]
                        print(f"📸 Получено фото от {chat_id}, обработка...")
                        handle_photo(chat_id, file_id)
            
            error_count = 0  # сбрасываем счетчик ошибок
            time.sleep(POLL_SECONDS)
            
        except KeyboardInterrupt:
            print("\n👋 Остановка бота...")
            break
        except Exception as e:
            error_count += 1
            print(f"⚠️ Ошибка в основном цикле ({error_count}): {e}")
            
            if error_count > 10:
                print("🔄 Слишком много ошибок, перезапуск через 30 секунд...")
                time.sleep(30)
                error_count = 0
            else:
                time.sleep(10)  # ждем перед следующей попыткой


if __name__ == "__main__":
    main()
