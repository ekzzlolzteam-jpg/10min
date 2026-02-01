#!/usr/bin/env python3
"""
Объединенный скрипт:
1) Быстрый поиск любого аккаунта по целевой дате (логика 1.py).
2) Бинарный поиск границ всех аккаунтов за день (логика 2v2.py).
3) Умный перебор этих аккаунтов и поиск по имени (логика 3neu.py).
"""

import json
import logging
import random
import re
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

import requests

# =========================
# ===== ОБЩИЙ КОНФИГ ======
# =========================
CONFIG = {
    # --- ЭТАП 1: поиск любого ID с нужной датой (быстрый прыжок) ---
    "start_id": 46211501,
    "start_date": "24.03.2017",
    "target_date": "08.02.2017",
    "jump_multiplier": 20000,
    "max_attempts": 300,
    "pause_seconds": 1,
    "correction_steps": {
        50: 700000,
        30: 500000,
        7: 25000,
        0: 10000,
    },
    # --- ЭТАП 2: бинарный поиск FIRST/LAST ---
    "max_requests": 350,
    "request_delay": 1,
    "request_timeout": 15,
    "users_per_day": 25000,
    "min_jump": 20000,
    "max_jump": 1_200_000,
    "max_neighbor_probes": 14,
    "final_scan_limit": 200,
    "print_details": True,
    "print_requests": False,
    # --- ЭТАП 3: умный перебор и поиск по имени ---
    "target_name": "vondeliasa",
    "requests_per_second": 15,
    "max_workers": 6,
    "batch_size": 50,
    "timeout": 10,
    "log_file": "search_complete.log",
    "log_level": logging.INFO,
    "save_progress": True,
    "progress_file": "search_progress.json",
}

BASE_URL = "https://www.kleinanzeigen.de/s-bestandsliste.html?userId="

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


# =========================
# ===== ЭТАП 1 (1.py) =====
# =========================
def date_to_days(date_str: str) -> Optional[int]:
    """Конвертирует DD.MM.YYYY в дни (обрабатывает разные форматы)."""
    if not date_str:
        return None

    date_str = str(date_str).strip().replace("/", ".").replace("-", ".")
    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y.%m.%d",
        "%d-%m-%Y",
    ]

    for fmt in formats:
        try:
            date_obj = datetime.strptime(date_str, fmt)
            return date_obj.toordinal()
        except ValueError:
            continue

    print(f"ERROR: Не могу распарсить дату '{date_str}'")
    return None


def get_date_from_page(user_id: int, session: requests.Session, pause_seconds: int) -> Optional[str]:
    """Получает дату регистрации со страницы."""
    url = f"{BASE_URL}{user_id}"
    try:
        time.sleep(pause_seconds)
        response = session.get(url, timeout=10)
        if response.status_code == 404:
            return None

        match = re.search(
            r"Aktiv seit\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})",
            response.text,
        )
        if match:
            date_str = match.group(1).replace("/", ".")
            try:
                datetime.strptime(date_str, "%d.%m.%Y")
                return date_str
            except ValueError:
                try:
                    datetime.strptime(date_str, "%d.%m.%y")
                    return date_str
                except ValueError:
                    return None
        return None
    except Exception:
        return None


def calculate_step(days_diff: int, correction_steps: Dict[int, int]) -> int:
    """Определяет шаг корректировки по таблице."""
    abs_diff = abs(days_diff)
    for threshold in sorted(correction_steps.keys(), reverse=True):
        if abs_diff > threshold:
            return correction_steps[threshold]
    return 500


def find_any_id_for_date() -> Optional[int]:
    """Поиск любого ID с нужной датой (логика 1.py)."""
    session = requests.Session()
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    start_id = CONFIG["start_id"]
    start_date = CONFIG["start_date"]
    target_date = CONFIG["target_date"]

    print("=== ЭТАП 1: ПОИСК ПОЛЬЗОВАТЕЛЯ ===")
    print(f"Ищем дату: {target_date}")
    print(f"Старт: ID {start_id} от {start_date}")
    print(
        f"Настройки: множитель={CONFIG['jump_multiplier']}, пауза={CONFIG['pause_seconds']}с"
    )
    print(f"Корректировки: {CONFIG['correction_steps']}")
    print("=" * 40)

    print(f"[1] ID {start_id} -> ", end="")
    start_date_found = get_date_from_page(
        start_id, session, CONFIG["pause_seconds"]
    )
    if not start_date_found:
        print("ОШИБКА: Не видим дату стартового пользователя!")
        return None
    print(f"{start_date_found}")

    if start_date_found != start_date:
        print(f"ВНИМАНИЕ: Указали {start_date}, а на сайте {start_date_found}")
        print("Продолжаем поиск с датой с сайта...")
        actual_start_date = start_date_found
    else:
        actual_start_date = start_date

    start_days = date_to_days(actual_start_date)
    target_days = date_to_days(target_date)

    if not start_days or not target_days:
        print(f"Ошибка формата даты! start_days={start_days}, target_days={target_days}")
        return None

    days_diff = start_days - target_days
    initial_jump = days_diff * CONFIG["jump_multiplier"]
    current_id = start_id - initial_jump

    print(f"Разница: {days_diff} дней")
    print(f"Прыжок: {initial_jump} ID -> проверяем ID {current_id}")
    print("-" * 40)

    for attempt in range(2, CONFIG["max_attempts"] + 1):
        if current_id <= 0:
            print("ID стал отрицательным!")
            current_id = 1
            continue

        print(f"[{attempt}] ID {current_id} -> ", end="", flush=True)
        date_found = get_date_from_page(current_id, session, CONFIG["pause_seconds"])

        if date_found is None:
            print("Нет пользователя")
            current_id += 1 if days_diff > 0 else -1
            continue

        print(f"{date_found}")
        current_days = date_to_days(date_found)
        if not current_days:
            current_id += 100
            continue

        if current_days == target_days:
            print("\n" + "=" * 40)
            print(f"НАЙДЕНО! ID: {current_id}")
            print(f"Дата: {date_found}")
            print(f"Ссылка: {BASE_URL}{current_id}")
            print("=" * 40)
            return current_id

        if current_days > target_days:
            direction = -1
            diff = current_days - target_days
        else:
            direction = 1
            diff = target_days - current_days

        step = calculate_step(diff, CONFIG["correction_steps"]) * direction
        print(f"    Корректировка: {step} ID (разница {diff} дней)")
        current_id += step

    print(f"\nНе найдено за {CONFIG['max_attempts']} попыток")
    print(f"Последний ID: {current_id}")
    return None


# =========================
# ===== ЭТАП 2 (2v2.py) ===
# =========================
AKTIV_RE = re.compile(
    r"Aktiv\s*seit\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})",
    re.IGNORECASE,
)


def norm_date(s: str) -> Optional[str]:
    s = s.replace("/", ".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return None


def ord_date(s: str) -> int:
    return datetime.strptime(s, "%d.%m.%Y").toordinal()


class Finder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept-Language": "de-DE,de;q=0.9",
                "DNT": "1",
            }
        )
        self.req = 0
        self.cache: Dict[int, Optional[str]] = {}

    def log(self, msg: str):
        if CONFIG["print_details"]:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def budget_ok(self) -> bool:
        return self.req < CONFIG["max_requests"]

    def fetch_date(self, uid: int) -> Optional[str]:
        if uid < 1 or not self.budget_ok():
            return None
        if uid in self.cache:
            return self.cache[uid]

        time.sleep(CONFIG["request_delay"])
        try:
            r = self.session.get(f"{BASE_URL}{uid}", timeout=CONFIG["request_timeout"])
            self.req += 1
            if CONFIG["print_requests"]:
                self.log(f"REQ {self.req}/{CONFIG['max_requests']}: {uid} -> {r.status_code}")

            if r.status_code != 200:
                self.cache[uid] = None
                return None

            m = AKTIV_RE.search(r.text)
            if not m:
                self.cache[uid] = None
                return None

            d = norm_date(m.group(1))
            self.cache[uid] = d
            return d
        except Exception:
            self.req += 1
            return None

    def nearest_ok(self, uid: int, lo: int, hi: int) -> Tuple[Optional[int], Optional[str]]:
        d = self.fetch_date(uid)
        if d and lo <= uid <= hi:
            return uid, d

        step = 1
        for _ in range(CONFIG["max_neighbor_probes"]):
            for c in (uid - step, uid + step):
                if c < lo or c > hi:
                    continue
                dd = self.fetch_date(c)
                if dd:
                    return c, dd
            step *= 2
        return None, None

    def jump(self, uid: int, days: int, direction: int) -> int:
        step = max(
            CONFIG["min_jump"],
            min(days * CONFIG["users_per_day"], CONFIG["max_jump"]),
        )
        return uid + direction * step

    def find_first(self, known: int, target_date: str) -> Optional[int]:
        tord = ord_date(target_date)
        hi = known

        days = 1
        while self.budget_ok():
            cand = self.jump(hi, days, -1)
            cid, cd = self.nearest_ok(cand, 1, hi - 1)
            if cid and ord_date(cd) < tord:
                lo = cid
                break
            days *= 2
        else:
            return None

        left, right = lo, hi
        while left < right - 1 and self.budget_ok():
            mid = (left + right) // 2
            mid_id, mid_d = self.nearest_ok(mid, left + 1, right - 1)

            if not mid_id:
                right -= 1
                continue

            if ord_date(mid_d) < tord:
                left = mid_id
            else:
                right = mid_id

            self.log(f"FIRST step: {left} .. {right}")

        cur = right
        for _ in range(CONFIG["final_scan_limit"]):
            pid, pd = self.nearest_ok(cur - 1, 1, cur - 1)
            if not pid or ord_date(pd) < tord:
                return cur
            cur = pid

        return cur

    def find_last(self, first: int, target_date: str) -> Optional[int]:
        tord = ord_date(target_date)
        lo = first

        days = 1
        while self.budget_ok():
            cand = self.jump(lo, days, +1)
            cid, cd = self.nearest_ok(cand, lo + 1, cand + CONFIG["max_jump"])
            if cid and ord_date(cd) > tord:
                hi = cid
                break
            days *= 2
        else:
            return None

        left, right = lo, hi
        while left < right - 1 and self.budget_ok():
            mid = (left + right) // 2
            mid_id, mid_d = self.nearest_ok(mid, left + 1, right - 1)

            if not mid_id:
                left += 1
                continue

            if ord_date(mid_d) <= tord:
                left = mid_id
            else:
                right = mid_id

            self.log(f"LAST step: {left} .. {right}")

        cur = left
        for _ in range(CONFIG["final_scan_limit"]):
            nid, nd = self.nearest_ok(cur + 1, cur + 1, cur + 1 + CONFIG["max_jump"])
            if not nid or ord_date(nd) > tord:
                return cur
            cur = nid

        return cur


def find_day_boundaries(known_id: int) -> Optional[Tuple[int, int]]:
    print("=" * 70)
    print("🚀 ЭТАП 2 — поиск границ регистрации (stable)")
    print("=" * 70)
    print(f"Дата   : {CONFIG['target_date']}")
    print(f"Known ID: {known_id}")
    print("=" * 70)

    finder = Finder()
    first = finder.find_first(known_id, CONFIG["target_date"])
    if not first:
        print("❌ FIRST_ID не найден")
        return None

    last = finder.find_last(first, CONFIG["target_date"])
    if not last:
        print("❌ LAST_ID не найден")
        return None

    print("\n" + "=" * 70)
    print("✅ РЕЗУЛЬТАТ")
    print("=" * 70)
    print(f"KNOWN_ID : {known_id}")
    print(f"FIRST_ID : {first}")
    print(f"LAST_ID  : {last}")
    print(f"COUNT    : {last - first + 1:,}")
    print(f"REQUESTS : {finder.req}/{CONFIG['max_requests']}")
    print("=" * 70)
    return first, last


# =========================
# ===== ЭТАП 3 (3neu.py) ==
# =========================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


class RateLimiter:
    """Контролер скорости запросов."""

    def __init__(self, requests_per_second: int):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0
        self.lock = threading.Lock()

    def wait(self):
        """Ждет необходимое время перед следующим запросом."""
        with self.lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time

            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                time.sleep(sleep_time)

            self.last_request_time = time.time()


class SearchLogger:
    """Кастомный логгер с красивым выводом."""

    def __init__(self):
        file_handler = logging.FileHandler(CONFIG["log_file"], encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(message)s"))

        self.logger = logging.getLogger("KleinanzeigenSearch")
        self.logger.setLevel(CONFIG["log_level"])
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        self.colors = {
            "INFO": "\033[94m",
            "SUCCESS": "\033[92m",
            "WARNING": "\033[93m",
            "ERROR": "\033[91m",
            "DEBUG": "\033[90m",
            "FOUND": "\033[95m",
            "FOUND_BANNER": "\033[1;35m",
            "RESET": "\033[0m",
        }

    def log(self, level: str, message: str, user_id: int = None, details: dict = None):
        """Логирование с красивым форматированием."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        if user_id:
            base_msg = f"[{timestamp}] [{level}] ID {user_id}: {message}"
        else:
            base_msg = f"[{timestamp}] [{level}]: {message}"

        if details:
            details_str = " | ".join(
                [f"{k}={v}" for k, v in details.items() if v is not None]
            )
            if details_str:
                base_msg += f" | {details_str}"

        if level == "FOUND":
            self.logger.warning(base_msg)
        elif level == "SUCCESS":
            self.logger.info(base_msg)
        else:
            getattr(self.logger, level.lower())(base_msg)

        color = self.colors.get(level, self.colors["RESET"])
        console_msg = f"{color}{base_msg}{self.colors['RESET']}"

        if level in ["PROGRESS", "FOUND", "ERROR"]:
            print(console_msg)
        else:
            if level in ["WARNING", "SUCCESS"] or "ERROR" in level:
                print(console_msg)


@dataclass
class SearchConfig:
    start_id: int
    end_id: int
    target_name: str
    target_date: str


class ReliableUserSearch:
    def __init__(self, search_config: SearchConfig):
        self.start_id = search_config.start_id
        self.end_id = search_config.end_id
        self.target_name = search_config.target_name
        self.target_date = search_config.target_date
        self.total_users = self.end_id - self.start_id + 1

        self.logger = SearchLogger()
        self.rate_limiter = RateLimiter(CONFIG["requests_per_second"])

        self.found_users = []
        self.checked_ids = set()
        self.request_count = 0
        self.success_count = 0
        self.error_count = 0
        self.missing_ids = set()
        self.start_time = time.time()

        self.lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.error_stats = defaultdict(int)
        self.error_codes = defaultdict(int)

        self.should_stop = False
        signal.signal(signal.SIGINT, self.signal_handler)

        if CONFIG["save_progress"]:
            self.load_progress()

        self.show_header()
        self.print_expected_stats()

    def signal_handler(self, signum, frame):
        """Обработчик Ctrl+C."""
        self.logger.log("WARNING", "Получен сигнал остановки, завершаем...")
        self.should_stop = True

    def show_header(self):
        """Красивый заголовок."""
        print("\n" + "=" * 80)
        print("🔍 KLEINANZEIGEN ПОИСК ПОЛЬЗОВАТЕЛЯ - НАДЕЖНАЯ ВЕРСИЯ")
        print("=" * 80)
        print(f"🎯 Цель: '{self.target_name}'")
        print(f"📊 Диапазон: {self.start_id:,} - {self.end_id:,}")
        print(f"📈 Всего пользователей: {self.total_users:,}")
        print(f"⚡ Скорость: {CONFIG['requests_per_second']} запросов/сек")
        print(f"🧵 Потоков: {CONFIG['max_workers']}")
        print("=" * 80)
        print(f"📝 Полный лог: {CONFIG['log_file']}")
        print(f"💾 Автосохранение: {CONFIG['progress_file']}")
        print("=" * 80 + "\n")

    def print_expected_stats(self):
        """Расчет ожидаемой производительности."""
        total_requests = self.total_users
        estimated_time = total_requests / CONFIG["requests_per_second"]

        self.logger.log("INFO", "РАСЧЕТ ПРОИЗВОДИТЕЛЬНОСТИ:")
        self.logger.log("INFO", f"  • Всего запросов: {total_requests:,}")
        self.logger.log(
            "INFO", f"  • Целевая скорость: {CONFIG['requests_per_second']} запр/сек"
        )

        if estimated_time < 60:
            self.logger.log("SUCCESS", f"  • Расчетное время: {estimated_time:.1f} секунд")
        elif estimated_time < 3600:
            minutes = estimated_time / 60
            self.logger.log("SUCCESS", f"  • Расчетное время: {minutes:.1f} минут")
        else:
            hours = estimated_time / 3600
            self.logger.log("SUCCESS", f"  • Расчетное время: {hours:.1f} часов")

        print()

    def clean_name(self, name: str) -> str:
        """Очистка имени от лишних пробелов."""
        if not name:
            return ""
        return re.sub(r"\s+", " ", name.strip())

    def extract_name_from_html(self, html: str):
        """Извлечение имени пользователя из HTML."""
        try:
            pattern = (
                r'<h2\s+class="userprofile--name">\s*<span\s+class="sr-only">'
                r"Profil\s+von</span>\s*(.*?)\s*</h2>"
            )
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)

            if match:
                name = match.group(1).strip()
                name = re.sub(r"&[a-z]+;", " ", name)
                return self.clean_name(name)

            pattern2 = r'"userprofile--name"[^>]*>.*?Profil von.*?>\s*(.*?)\s*<'
            match = re.search(pattern2, html, re.DOTALL | re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                return self.clean_name(name)

            return None
        except Exception as e:
            self.logger.log("DEBUG", f"Ошибка парсинга имени: {str(e)[:50]}")
            return None

    def is_exact_match(self, found_name: str) -> bool:
        """Проверка точного совпадения имени."""
        if not found_name:
            return False
        clean_found = self.clean_name(found_name).lower()
        clean_target = self.clean_name(self.target_name).lower()
        return clean_found == clean_target

    def fetch_user_page(self, user_id: int):
        """Получение страницы пользователя с контролем скорости."""
        self.rate_limiter.wait()
        url = f"{BASE_URL}{user_id}"

        try:
            response = self.session.get(url, timeout=CONFIG["timeout"])

            with self.lock:
                self.request_count += 1
                self.success_count += 1

            return {
                "success": True,
                "status_code": response.status_code,
                "html": response.text if response.status_code == 200 else None,
                "error": None,
            }
        except requests.exceptions.Timeout:
            with self.lock:
                self.request_count += 1
                self.error_count += 1
                self.error_stats["timeout"] += 1

            return {
                "success": False,
                "status_code": None,
                "html": None,
                "error": "timeout",
            }
        except requests.exceptions.ConnectionError:
            with self.lock:
                self.request_count += 1
                self.error_count += 1
                self.error_stats["connection"] += 1

            return {
                "success": False,
                "status_code": None,
                "html": None,
                "error": "connection",
            }
        except Exception as e:
            with self.lock:
                self.request_count += 1
                self.error_count += 1
                self.error_stats["other"] += 1

            return {
                "success": False,
                "status_code": None,
                "html": None,
                "error": f"other: {str(e)[:50]}",
            }

    def process_user(self, user_id: int):
        """Обработка одного пользователя."""
        if self.should_stop:
            return None

        result = self.fetch_user_page(user_id)

        if result["success"]:
            if result["status_code"] == 200:
                name = self.extract_name_from_html(result["html"])

                if name:
                    if self.is_exact_match(name):
                        user_data = {
                            "id": user_id,
                            "name": name,
                            "url": f"{BASE_URL}{user_id}",
                            "timestamp": datetime.now().isoformat(),
                        }

                        with self.lock:
                            self.found_users.append(user_data)

                        print("\n" + "⭐" * 70)
                        print("⭐" * 25 + " НАЙДЕН ЦЕЛЕВОЙ ПОЛЬЗОВАТЕЛЬ! " + "⭐" * 25)
                        print("⭐" * 70)
                        print(f"⭐ ID: {user_id}")
                        print(f"⭐ ИМЯ: {name}")
                        print(f"⭐ ССЫЛКА: {BASE_URL}{user_id}")
                        print(f"⭐ ВРЕМЯ: {datetime.now().strftime('%H:%M:%S')}")
                        print("⭐" * 70)
                        print("⭐ Скрипт продолжит работу для поиска других совпадений")
                        print("⭐" * 70 + "\n")

                        self.logger.log(
                            "FOUND",
                            "НАЙДЕН ПОЛЬЗОВАТЕЛЬ!",
                            user_id,
                            {"name": name, "status": "FOUND"},
                        )
                        return user_data

                    self.logger.log("INFO", "Проверен", user_id, {"name": name})
                else:
                    self.logger.log("DEBUG", "Имя не найдено в HTML", user_id)
            else:
                with self.lock:
                    self.error_codes[result["status_code"]] += 1

                self.logger.log(
                    "ERROR",
                    "HTTP ошибка",
                    user_id,
                    {"status": result["status_code"]},
                )
        else:
            self.logger.log("ERROR", "Ошибка запроса", user_id, {"error": result["error"]})

        return None

    def print_progress(self):
        """Красивый вывод прогресса."""
        with self.lock:
            checked = len(self.checked_ids)
            requests_count = self.request_count
            errors = self.error_count
            found = len(self.found_users)

        elapsed = time.time() - self.start_time
        progress_pct = (checked / self.total_users * 100) if self.total_users > 0 else 0

        speed = requests_count / elapsed if elapsed > 0 else 0

        if speed > 0 and checked < self.total_users:
            remaining = self.total_users - checked
            eta_seconds = remaining / speed

            if eta_seconds < 60:
                eta_str = f"{eta_seconds:.0f}с"
            elif eta_seconds < 3600:
                eta_str = f"{eta_seconds/60:.0f}м"
            else:
                eta_str = f"{eta_seconds/3600:.1f}ч"
        else:
            eta_str = "---"

        progress_line = (
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"[PROGRESS] Проверено: {checked:,}/{self.total_users:,} ({progress_pct:.1f}%) | "
            f"Скорость: {speed:.1f} запр/сек | "
            f"Осталось: {eta_str} | "
            f"Найдено: {found} | "
            f"Ошибок: {errors:,}"
        )

        if progress_pct < 30:
            color = "\033[91m"
        elif progress_pct < 70:
            color = "\033[93m"
        else:
            color = "\033[92m"

        print(f"{color}{progress_line}\033[0m", end="\r")

    def search_batch(self, batch_ids):
        """Поиск в пакете ID."""
        with ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as executor:
            future_to_id = {
                executor.submit(self.process_user, user_id): user_id for user_id in batch_ids
            }

            for future in as_completed(future_to_id):
                user_id = future_to_id[future]

                try:
                    future.result(timeout=CONFIG["timeout"] + 5)
                except Exception as e:
                    self.logger.log(
                        "ERROR", "Ошибка обработки", user_id, {"error": str(e)[:50]}
                    )

                with self.lock:
                    self.checked_ids.add(user_id)

                if len(self.checked_ids) % 5 == 0:
                    self.print_progress()

    def run_search(self):
        """Основной метод поиска."""
        self.logger.log("INFO", "🚀 ЗАПУСК ПОИСКА...")
        self.logger.log("INFO", "Нажмите Ctrl+C для безопасной остановки")
        print()

        self.print_progress()

        all_ids = range(self.start_id, self.end_id + 1)
        total_batches = (self.total_users + CONFIG["batch_size"] - 1) // CONFIG["batch_size"]

        for batch_num in range(total_batches):
            if self.should_stop:
                self.logger.log("WARNING", "Поиск остановлен пользователем")
                break

            start_idx = batch_num * CONFIG["batch_size"]
            end_idx = min((batch_num + 1) * CONFIG["batch_size"], self.total_users)
            batch_ids = list(all_ids[start_idx:end_idx])

            batch_start = time.time()
            self.search_batch(batch_ids)
            batch_time = time.time() - batch_start

            if batch_time > 0 and len(batch_ids) > 0:
                batch_speed = len(batch_ids) / batch_time
                self.logger.log(
                    "DEBUG",
                    f"Пакет {batch_num+1}/{total_batches} завершен",
                    None,
                    {"время": f"{batch_time:.1f}с", "скорость": f"{batch_speed:.1f} запр/сек"},
                )

            if CONFIG["save_progress"] and batch_num % 5 == 0:
                self.save_progress()

            if batch_num < total_batches - 1 and not self.should_stop:
                time.sleep(0.3)

        self.print_progress()
        print("\n")

        self.check_missing_ids()
        return self.found_users

    def check_missing_ids(self):
        """Проверка пропущенных ID."""
        expected_ids = set(range(self.start_id, self.end_id + 1))
        missing_ids = expected_ids - self.checked_ids

        if missing_ids:
            self.missing_ids = missing_ids
            self.logger.log("WARNING", f"Обнаружены пропущенные ID: {len(missing_ids)}")

            first_missing = sorted(missing_ids)[:10]
            for mid in first_missing:
                self.logger.log("DEBUG", "Пропущен ID", mid)

            if len(missing_ids) > 10:
                self.logger.log("DEBUG", f"... и еще {len(missing_ids) - 10} ID")

    def save_progress(self):
        """Сохранение прогресса."""
        if not CONFIG["save_progress"]:
            return

        with self.lock:
            data = {
                "search_config": {
                    "start_id": self.start_id,
                    "end_id": self.end_id,
                    "target_name": self.target_name,
                    "target_date": self.target_date,
                },
                "progress": {
                    "checked_ids": sorted(list(self.checked_ids)),
                    "last_check_time": datetime.now().isoformat(),
                    "total_checked": len(self.checked_ids),
                },
                "results": {
                    "found_users": self.found_users,
                    "total_found": len(self.found_users),
                },
                "statistics": {
                    "requests": self.request_count,
                    "success": self.success_count,
                    "errors": self.error_count,
                    "error_stats": dict(self.error_stats),
                    "error_codes": dict(self.error_codes),
                },
                "missing_ids": sorted(list(self.missing_ids)),
            }

        try:
            with open(CONFIG["progress_file"], "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.logger.log("SUCCESS", "Прогресс сохранен")
        except Exception as e:
            self.logger.log("ERROR", "Ошибка сохранения прогресса", None, {"error": str(e)})

    def load_progress(self):
        """Загрузка прогресса."""
        try:
            with open(CONFIG["progress_file"], "r", encoding="utf-8") as f:
                data = json.load(f)

            config = data.get("search_config", {})
            if (
                config.get("start_id") == self.start_id
                and config.get("end_id") == self.end_id
                and config.get("target_name") == self.target_name
            ):
                progress = data.get("progress", {})
                self.checked_ids = set(progress.get("checked_ids", []))

                results = data.get("results", {})
                self.found_users = results.get("found_users", [])

                stats = data.get("statistics", {})
                self.request_count = stats.get("requests", 0)
                self.success_count = stats.get("success", 0)
                self.error_count = stats.get("errors", 0)

                missing = data.get("missing_ids", [])
                self.missing_ids = set(missing)

                self.logger.log(
                    "SUCCESS",
                    f"Загружен прогресс: {len(self.checked_ids):,} проверенных",
                )
                self.logger.log(
                    "SUCCESS", f"Уже найдено: {len(self.found_users)} пользователей"
                )

                if self.missing_ids:
                    self.logger.log("WARNING", f"Есть пропущенные ID: {len(self.missing_ids)}")

        except FileNotFoundError:
            self.logger.log("INFO", "Прогресс не найден, начинаем с начала")
        except Exception as e:
            self.logger.log("ERROR", "Ошибка загрузки прогресса", None, {"error": str(e)})

    def print_final_report(self):
        """Красивый финальный отчет с выделением найденных пользователей."""
        print("\n" + "=" * 80)
        print("📊 ФИНАЛЬНЫЙ ОТЧЕТ ПОИСКА")
        print("=" * 80)

        elapsed = time.time() - self.start_time

        with self.lock:
            checked = len(self.checked_ids)
            requests_count = self.request_count
            errors = self.error_count
            found = len(self.found_users)
            missing = len(self.missing_ids)

        if elapsed < 60:
            time_str = f"{elapsed:.1f} секунд"
        elif elapsed < 3600:
            time_str = f"{elapsed/60:.1f} минут"
        else:
            time_str = f"{elapsed/3600:.1f} часов"

        print(f"\n⏱️  ОБЩЕЕ ВРЕМЯ: {time_str}")
        print(f"📊 ПРОВЕРЕНО: {checked:,}/{self.total_users:,} пользователей")

        if elapsed > 0:
            avg_speed = requests_count / elapsed
            print(f"⚡ СРЕДНЯЯ СКОРОСТЬ: {avg_speed:.1f} запросов/сек")

        print(f"✅ УСПЕШНЫХ ЗАПРОСОВ: {self.success_count:,}")
        print(f"❌ ОШИБОК: {errors:,}")

        if self.error_stats:
            print("\n🔧 СТАТИСТИКА ОШИБОК:")
            for error_type, count in sorted(self.error_stats.items()):
                if count > 0:
                    percentage = (count / errors * 100) if errors > 0 else 0
                    print(f"   • {error_type}: {count:,} ({percentage:.1f}%)")

        if self.error_codes:
            print("\n📡 HTTP СТАТУСЫ:")
            for code, count in sorted(self.error_codes.items()):
                print(f"   • {code}: {count:,}")

        if missing > 0:
            print(f"\n⚠️  ПРОПУЩЕНО ID: {missing:,}")
            if missing <= 20:
                print(f"   ID: {sorted(self.missing_ids)}")

        print("\n" + "-" * 40)
        print("🎯 РЕЗУЛЬТАТЫ ПОИСКА:")

        if found > 0:
            print("\n" + "🎯" * 40)
            print(f"🎯 НАЙДЕНО ЦЕЛЕВЫХ ПОЛЬЗОВАТЕЛЕЙ: {found}")
            print("🎯" * 40)

            for user in self.found_users:
                print(f"\n   ⭐ ID: {user['id']}")
                print(f"   ⭐ Имя: {user['name']}")
                print(f"   ⭐ Ссылка: {user['url']}")
                print(f"   ⭐ Время находки: {user['timestamp'][11:19]}")
                print(f"   {'─' * 40}")
        else:
            print(f"\n❌ Пользователь '{self.target_name}' не найден")
            print(f"   в диапазоне {self.start_id:,} - {self.end_id:,}")

        print("\n" + "=" * 80)
        print(f"📁 Лог сохранен: {CONFIG['log_file']}")
        if CONFIG["save_progress"]:
            print(f"💾 Прогресс сохранен: {CONFIG['progress_file']}")
        print("=" * 80)


def run_full_pipeline():
    print("\n" + "✨" * 40)
    print("✨      KLEINANZEIGEN ОБЪЕДИНЕННЫЙ ПОИСК      ✨")
    print("✨" * 40 + "\n")

    if not CONFIG["target_date"]:
        print("❌ Ошибка: target_date не может быть пустой")
        return

    if not CONFIG["target_name"] or CONFIG["target_name"].strip() == "":
        print("❌ Ошибка: target_name не может быть пустым")
        return

    known_id = find_any_id_for_date()
    if not known_id:
        print("❌ Не удалось найти ID по целевой дате (этап 1)")
        return

    boundaries = find_day_boundaries(known_id)
    if not boundaries:
        print("❌ Не удалось найти границы дня (этап 2)")
        return

    start_id, end_id = boundaries
    if start_id > end_id:
        print("❌ Ошибка: START_ID должен быть меньше END_ID")
        return

    search_config = SearchConfig(
        start_id=start_id,
        end_id=end_id,
        target_name=CONFIG["target_name"],
        target_date=CONFIG["target_date"],
    )

    try:
        search = ReliableUserSearch(search_config)
        search.run_search()
        if CONFIG["save_progress"]:
            search.save_progress()
        search.print_final_report()

        if search.missing_ids:
            print("\n⚠️  Хотите проверить пропущенные ID?")
            print("   Запустите: python check_missing.py")
    except KeyboardInterrupt:
        print("\n\n🛑 ПОИСК ПРЕРВАН ПОЛЬЗОВАТЕЛЕМ")
        if "search" in locals():
            if CONFIG["save_progress"]:
                search.save_progress()
            search.print_final_report()
    except Exception as e:
        print(f"\n💥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    run_full_pipeline()
