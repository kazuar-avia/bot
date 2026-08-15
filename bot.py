import discord
import aiohttp
import asyncio
import json
import os
import logging
import re
import random
import io
import time
import shutil
import base64
import subprocess
import sys
from pathlib import Path
from itertools import cycle
from datetime import datetime, timezone, timedelta
from aiohttp import web
from discord.ext import tasks

# ---------- НАЛАШТУВАННЯ ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else 0
NEWSKY_API_KEY = os.getenv("NEWSKY_API_KEY")
NEWSKY_API_KEY_ADD = os.getenv("NEWSKY_API_KEY_ADD")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
NEWSKY_SID = os.getenv("NEWSKY_SID")
GITHUB_REPO = "kazuar-avia/kazuar-avia.github.io"
GITHUB_FILE_PATH = "newsky-airports.txt"

# Формуємо список ключів (першим завжди йде головний!)
NEWSKY_API_KEYS = []
if NEWSKY_API_KEY: 
    NEWSKY_API_KEYS.append(NEWSKY_API_KEY)
if NEWSKY_API_KEY_ADD: 
    NEWSKY_API_KEYS.append(NEWSKY_API_KEY_ADD)
if not NEWSKY_API_KEYS: 
    NEWSKY_API_KEYS.append("")

ADMIN_IDS = [
    598767470140063744,
]

START_TIME = datetime.now(timezone.utc)

STATE_FILE = Path("/app/data/sent.json")
HIDDEN_FILE = Path("/app/data/hidden.json")
STATUS_FILE = Path("/app/data/statuses.json")
WEEKLY_STATS_FILE = Path("/app/data/weekly_stats.json")
IGNORED_FILE = Path("/app/data/ignored.json")
CHARTERS_FILE = Path("/app/data/charters.json")
CHECK_INTERVAL = 10
BASE_URL = "https://newsky.app/api/airline-api"
AIRPORTS_DB_URL = "https://raw.githubusercontent.com/mwgg/Airports/master/airports.json"

logging.basicConfig(level=logging.INFO)
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

AIRPORTS_DB = {}
HIDDEN_USERS = {}
BANNED_WOW_MESSAGES = set()
MONITORING_STARTED = False
LAST_TRAFFIC_TIME = 0.0
TAXIING_FLIGHTS = set()
last_sent_message = None

# ---------- ДОПОМІЖНІ ФУНКЦІЇ ----------
def format_flight_for_db(f):
    correctAircraftName = None
    if f.get("aircraft") and f["aircraft"].get("airframe") and f["aircraft"]["airframe"].get("name"):
        correctAircraftName = f["aircraft"]["airframe"]["name"]
    elif f.get("aircraft") and f["aircraft"].get("name"):
        correctAircraftName = f["aircraft"]["name"]

    cleanFlight = {
        "_id": f.get("_id"),
        "aircraft": {
            "_id": f.get("aircraft", {}).get("_id") if f.get("aircraft") else None,
            "name": correctAircraftName,
            "customName": f.get("aircraft", {}).get("name") if f.get("aircraft") else None,
            "icao": f.get("aircraft", {}).get("airframe", {}).get("icao") if f.get("aircraft") else None
        },
        "type": f.get("type"),
        "flightNumber": f.get("flightNumber"),
        "free": (not f.get("charter") and not f.get("schedule")),
        "schedule": bool(f.get("schedule")),
        "charter": bool(f.get("charter")),
        "payload": {
            "pax": f.get("payload", {}).get("pax"),
            "cargo": f.get("payload", {}).get("cargo"),
            "weights": {
                "cargo": f.get("payload", {}).get("weights", {}).get("cargo")
            }
        },
        "depTime": f.get("depTime"),
        "duration": f.get("duration"),
        "arrTime": f.get("arrTime"),
        "depTimeAct": f.get("depTimeAct"),
        "delay": f.get("delay"),
        "takeoffTimeAct": f.get("takeoffTimeAct"),
        "durationAct": f.get("durationAct"),
        "arrTimeAct": f.get("arrTimeAct"),
        "open": f.get("open"),
        "close": f.get("close"),
        "rating": f.get("rating"),
        "simulator": f.get("simulator"),
        "network": {"name": f["network"]["name"]} if f.get("network") and isinstance(f["network"], dict) else None,
        "emergency": f.get("emergency"),
        "result": {
            "totals": {
                "distance": f.get("result", {}).get("totals", {}).get("distance"),
                "fuel": f.get("result", {}).get("totals", {}).get("fuel"),
                "expenses": f.get("result", {}).get("totals", {}).get("expenses"),
                "penalties": f.get("result", {}).get("totals", {}).get("penalties"),
                "revenue": f.get("result", {}).get("totals", {}).get("revenue"),
                "balance": f.get("result", {}).get("totals", {}).get("balance"),
                "prices": f.get("result", {}).get("totals", {}).get("prices"),
                "payload": {
                    "pax": f.get("result", {}).get("totals", {}).get("payload", {}).get("pax"),
                    "cargo": f.get("result", {}).get("totals", {}).get("payload", {}).get("cargo"),
                    "paxByClass": f.get("result", {}).get("totals", {}).get("payload", {}).get("paxByClass")
                }
            },
            "expenses": f.get("result", {}).get("expenses"),
            "revenue": f.get("result", {}).get("revenue"),
            "violations": []
        },
        "createdAt": f.get("createdAt"),
        "updatedAt": f.get("updatedAt"),
        "dep": f.get("dep"),
        "arr": f.get("arr"),
        "actArr": f.get("actArr")
    }

    if f.get("result") and f["result"].get("violations"):
        for v in f["result"]["violations"]:
            cleanV = {
                "title": v.get("title"),
                "penalty": v.get("penalty")
            }
            if v.get("entry") and v["entry"].get("payload"):
                p = v["entry"]["payload"]
                cleanV["entry"] = {
                    "type": v["entry"].get("type", "landing"),
                    "payload": {
                        "system": {"fps": p["system"]["fps"]} if p.get("system") and "fps" in p["system"] else None
                    }
                }
                if p.get("touchDown"):
                    cleanV["entry"]["payload"]["touchDown"] = {
                        "gForce": p["touchDown"].get("gForce"),
                        "rate": p["touchDown"].get("rate")
                    }
                
                # 🔥 НОВЕ: ЗБЕРІГАЄМО ПОГОДУ (ВІТЕР) ПІД ЧАС ПОСАДКИ 🔥
                if p.get("weather"):
                    w = p["weather"]
                    cleanV["entry"]["payload"]["weather"] = {
                        "windDir": w.get("windDir"),
                        "windSpd": w.get("windSpd"),
                        "windX": w.get("windX")
                    }

            cleanFlight["result"]["violations"].append(cleanV)
            
    # 🔥 НОВЕ: РЕЗЕРВНЕ ЗБЕРЕЖЕННЯ ПОСАДКИ (якщо не було порушень) 🔥
    if f.get("landing"):
        cleanFlight["landing"] = {
            "rate": f["landing"].get("rate") or f["landing"].get("touchDownRate"),
            "gForce": f["landing"].get("gForce")
        }
        if f["landing"].get("weather"):
            w = f["landing"]["weather"]
            cleanFlight["landing"]["weather"] = {
                "windDir": w.get("windDir"),
                "windSpd": w.get("windSpd"),
                "windX": w.get("windX")
            }
            
    return cleanFlight

async def get_updated_liveries_content(session, target_ac_id, actual_arr_icao):
    if not NEWSKY_SID or not GITHUB_TOKEN: return None
    
    # 1. Запит до Newsky (список всіх бортів компанії)
    ns_headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
        "Content-Type": "application/json"
    }
    payload = {"skip": 0, "count": 100, "needle": "", "sort": "airframe.icao", "order": 1}
    airline_id = "695810be3dc76275ba8befa9"
    
    ns_aircraft = {}
    try:
        async with session.post(f"https://newsky.app/api/airline/{airline_id}/aircraft/list", headers=ns_headers, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                for ac in data.get("results", []):
                    ns_aircraft[str(ac.get("_id"))] = {
                        "locationIcao": ac.get("locationIcao"),
                        "name": ac.get("name")
                    }
    except Exception as e: 
        print(f"Error fetching aircraft list: {e}")
        pass
    
    # 2. Читання livery-matching.json через Blob API (Миттєво, 100% без кешу)
    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Cache-Control": "no-cache"
    }
    livery_data = None
    
    try:
        # Шукаємо SHA файлу в папці COMPANY (з антикеш-таймером)
        dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/COMPANY?t={int(time.time())}"
        file_sha = None
        
        async with session.get(dir_url, headers=gh_headers) as dir_resp:
            if dir_resp.status == 200:
                for item in await dir_resp.json():
                    if item.get("name") == "livery-matching.json":
                        file_sha = item.get("sha")
                        break
                        
        # Якщо знайшли SHA, викачуємо "м'ясо" файлу напряму
        if file_sha:
            blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
            async with session.get(blob_url, headers=gh_headers) as blob_resp:
                if blob_resp.status == 200:
                    blob_data = await blob_resp.json()
                    livery_data = json.loads(base64.b64decode(blob_data['content']).decode('utf-8'))
    except Exception as e:
        print(f"Помилка читання livery: {e}")
        return None
        
    if not livery_data: return None
    
    # 3. М'ясорубка: оновлюємо локації
    for ac in livery_data.get("liveries", []):
        ac_id = str(ac.get("_id"))
        
        # А) Оновлюємо загальну локацію та назву з Newsky (якщо борт є у списку)
        if ac_id in ns_aircraft:
            ac["locationIcao"] = ns_aircraft[ac_id].get("locationIcao")
            if ns_aircraft[ac_id].get("name"):
                ac["name"] = ns_aircraft[ac_id].get("name")
            
        # Б) Оновлюємо lastflightlocationICAO ТІЛЬКИ для літака, що щойно сів (замінюючи логіку Newsky)
        if ac_id == str(target_ac_id) and actual_arr_icao:
            ac["lastflightlocationICAO"] = actual_arr_icao
            
    # 4. Оновлюємо час генерації
    livery_data["generatedAtUtc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    # Повертаємо готовий текст для запису
    return json.dumps(livery_data, ensure_ascii=False, indent=2)
    
async def save_flight_to_github(clean_flight, pilot_id, pilot_name, pilot_avatar, week_tag):
    if not GITHUB_TOKEN:
        print("⚠️ Немає токену GitHub, рейс не збережено в БД.")
        return
        
    week_filename = f"{week_tag}.json"
    file_path = f"FLIGHTS/{week_filename}"
    
    # 🔥 Додано антикеш заголовок
    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Cache-Control": "no-cache"
    }
    
    async with GITHUB_DB_LOCK:
        async with aiohttp.ClientSession() as session:
            # 1. ШУКАЄМО SHA ФАЙЛУ (додано ?t=... для обходу кешу GitHub)
            file_sha = None
            dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
            async with session.get(dir_url, headers=gh_headers) as dir_resp:
                if dir_resp.status == 200:
                    dir_data = await dir_resp.json()
                    if isinstance(dir_data, list):
                        for item in dir_data:
                            if item.get("name") == week_filename:
                                file_sha = item.get("sha")
                                break
            
            # 2. КАЧАЄМО ВМІСТ НАПРЯМУ З BLOB API (Миттєво, без кешу!)
            github_file_content = []
            if file_sha:
                blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                async with session.get(blob_url, headers=gh_headers) as blob_resp:
                    if blob_resp.status == 200:
                        try:
                            blob_data = await blob_resp.json()
                            text_content = base64.b64decode(blob_data['content']).decode('utf-8')
                            github_file_content = json.loads(text_content)
                        except Exception as e:
                            print(f"Помилка читання Blob: {e}")
                            github_file_content = []
            
            # 3. СОРТУВАННЯ І ДОДАВАННЯ РЕЙСУ ПО ПІЛОТАХ
            existing_pilot = next((p for p in github_file_content if p.get("pilot_id") == pilot_id), None)
            
            profile_changed = False # 🔥 Флаг для запуску масового оновлення
            
            if existing_pilot:
                # Перевіряємо, чи змінилася аватарка або ім'я порівняно з базою
                if existing_pilot.get("avatar") != pilot_avatar or existing_pilot.get("fullname") != pilot_name:
                    print(f"⚠️ Виявлено зміну профілю для {pilot_name}. Запускаю фонове оновлення бази...")
                    existing_pilot["fullname"] = pilot_name
                    existing_pilot["avatar"] = pilot_avatar
                    profile_changed = True # Вмикаємо тригер!

                # Перевірка, щоб випадково не додати той самий рейс двічі
                if not any(f.get("_id") == clean_flight["_id"] for f in existing_pilot["flights"]):
                    existing_pilot["flights"].append(clean_flight)
            else:
                # Якщо пілот летить вперше на цьому тижні - створюємо його блок
                new_pilot_block = {
                    "pilot_id": pilot_id,
                    "fullname": pilot_name,
                    "avatar": pilot_avatar,
                    "flights": [clean_flight]
                }
                github_file_content.append(new_pilot_block)
            
            # 4. ВІДПРАВКА НА GITHUB (ОДНИМ КОМІТОМ РАЗОМ ІЗ LIVERY-MATCHING)
            new_content_str = json.dumps(github_file_content, ensure_ascii=False, indent=4)
            
            # Збираємо файли для відправки
            files_to_push = {
                file_path: new_content_str
            }
            
            # Дістаємо ID літака та фактичний аеропорт прибуття з чистих даних рейсу
            ac_id = clean_flight.get("aircraft", {}).get("_id")
            act_arr = clean_flight.get("actArr")
            arr = clean_flight.get("arr")
            
            actual_arr_icao = None
            if isinstance(act_arr, dict) and act_arr.get("icao"):
                actual_arr_icao = act_arr.get("icao")
            elif isinstance(arr, dict) and arr.get("icao"):
                actual_arr_icao = arr.get("icao")
                
            # Генеруємо оновлений livery-matching.json і додаємо до списку файлів на відправку
            if ac_id:
                livery_content = await get_updated_liveries_content(session, ac_id, actual_arr_icao)
                if livery_content:
                    files_to_push["COMPANY/livery-matching.json"] = livery_content
                    
            commit_msg = f"🤖 Auto db update: flight {clean_flight.get('flightNumber')} & Fleet Loc"
            
            # Відправляємо єдиним пушем через вашу готову функцію push_to_github_batch
            success = await push_to_github_batch(session, files_to_push, commit_msg)
            
            if not success:
                print(f"❌ Помилка пакетного запису рейсу та локацій на GitHub.")
            else:
                print(f"✅ Рейс та локації флоту успішно збережено одним комітом!")
                if profile_changed:
                    client.loop.create_task(update_pilot_history_on_github(pilot_id, pilot_name, pilot_avatar))


async def update_pilot_history_on_github(pilot_id, new_name, new_avatar):
    if not GITHUB_TOKEN: return
    
    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Cache-Control": "no-cache"
    }
    
    print(f"🔄 Починаю масове оновлення профілю для {new_name} на GitHub...")
    
    # Використовуємо замок, щоб не заважати запису нових рейсів
    async with GITHUB_DB_LOCK:
        async with aiohttp.ClientSession() as session:
            # 1. Отримуємо список всіх файлів у папці FLIGHTS
            dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
            async with session.get(dir_url, headers=gh_headers) as dir_resp:
                if dir_resp.status != 200: 
                    print("❌ Помилка доступу до папки FLIGHTS")
                    return
                dir_data = await dir_resp.json()
                
            files_to_push = {}
            
            # 2. Проходимося по кожному файлу
            for item in dir_data:
                file_name = item.get("name", "")
                
                if not re.match(r"^\d{4}-W\d{2}\.json$", file_name): 
                    continue
                
                file_path = item["path"]
                file_sha = item["sha"]
                
                # Завантажуємо вміст файлу через Blob API (обхід кешу!)
                blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                async with session.get(blob_url, headers=gh_headers) as blob_resp:
                    if blob_resp.status != 200: continue
                    try:
                        blob_data = await blob_resp.json()
                        text_content = base64.b64decode(blob_data['content']).decode('utf-8')
                        file_content = json.loads(text_content)
                    except Exception as e: 
                        print(f"Помилка читання Blob у фоні: {e}")
                        continue
                        
                changed = False
                
                # 3. Шукаємо нашого пілота в цьому файлі
                for p in file_content:
                    if p.get("pilot_id") == pilot_id:
                        if p.get("fullname") != new_name or p.get("avatar") != new_avatar:
                            p["fullname"] = new_name
                            p["avatar"] = new_avatar
                            changed = True
                        break 
                        
                # 4. Якщо були зміни - додаємо у кошик для масового пушу
                if changed:
                    new_content_str = json.dumps(file_content, ensure_ascii=False, indent=4)
                    files_to_push[file_path] = new_content_str
                    
                # Невелика пауза між читаннями, щоб не дусити GitHub GET-запитами
                await asyncio.sleep(0.1) 

            # 5. Відправляємо всі змінені файли одним комітом!
            if files_to_push:
                commit_msg = f"🤖 Auto update profile info for {new_name}"
                success = await push_to_github_batch(session, files_to_push, commit_msg)
                
                if success:
                    try:
                        owner = await client.fetch_user(ADMIN_IDS[0])
                        await owner.send(f"🔄 **Системне оновлення!** Пілот `{new_name}` змінив нік або аватарку.\nУспішно оновлено файлів історії на GitHub (одним комітом): **{len(files_to_push)}** 🗂️.")
                    except Exception as e:
                        print(f"Не вдалося відправити звіт адміну: {e}")
                else:
                    print(f"❌ Помилка під час масового пушу оновлень профілю для {new_name}")
            else:
                print(f"✅ Оновлення профілю завершено: не знайдено файлів, які б потребували змін для {new_name}.")
    
def load_charters_state():
    if not CHARTERS_FILE.exists(): return {}
    try: return json.loads(CHARTERS_FILE.read_text(encoding="utf-8"))
    except: return {}

def save_charters_state(state):
    try:
        if len(state) > 100: state = dict(list(state.items())[-50:])
        CHARTERS_FILE.write_text(json.dumps(state), encoding="utf-8")
    except: pass

def load_state():
    if not STATE_FILE.exists(): return {}
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except: return {}

def save_state(state):
    try:
        if len(state) > 100: state = dict(list(state.items())[-50:])
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except: pass

def load_ignored():
    if not IGNORED_FILE.exists(): return []
    try: return json.loads(IGNORED_FILE.read_text(encoding="utf-8"))
    except: return []

def save_ignored(ignored_list):
    try: IGNORED_FILE.write_text(json.dumps(ignored_list), encoding="utf-8")
    except: pass

def load_hidden_users():
    if not HIDDEN_FILE.exists(): return {}
    try: return json.loads(HIDDEN_FILE.read_text(encoding="utf-8"))
    except: return {}

def save_hidden_users(data):
    try: HIDDEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    except: pass

HIDDEN_USERS = load_hidden_users()

# 🔥 БЛОК ФУНКЦІЙ ДЛЯ СТАТИСТИКИ 🔥
def load_weekly_stats():
    if not WEEKLY_STATS_FILE.exists(): return {}
    try: return json.loads(WEEKLY_STATS_FILE.read_text(encoding="utf-8"))
    except: return {}

def save_weekly_stats(stats):
    try: WEEKLY_STATS_FILE.write_text(json.dumps(stats, indent=4), encoding="utf-8")
    except: pass

def get_iso_week(dt_str=None):
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

def get_week_dates_string(week_tag):
    try:
        y, w = map(int, week_tag.split("-W"))
        first_day = datetime.strptime(f'{y}-W{w}-1', "%G-W%V-%u")
        last_day = datetime.strptime(f'{y}-W{w}-7', "%G-W%V-%u")
        return f"{first_day.strftime('%d.%m.%Y')} - {last_day.strftime('%d.%m.%Y')}"
    except: return week_tag

def init_week_stats():
    return {
        "flights": 0, "earnings": 0, "pax": 0, "cargo": 0,
        "rating_sum": 0.0, "fpm_sum": 0, "g_sum": 0.0,
        "pilots": {}, "airports": {}, "aircrafts": {},
        "records": {
            "butter": {"fpm": -99999, "g": 0.0, "pilot": "None"},
            "hardest": {"fpm": 0, "g": 0.0, "pilot": "None"},
            "longest": [],
            "shortest": []
        }
    }

def update_weekly_stats(f, week_tag):
    stats = load_weekly_stats()
    if week_tag not in stats:
        stats[week_tag] = init_week_stats()
        
    s = stats[week_tag]
    
    t = f.get("result", {}).get("totals", {})
    balance = int(t.get("balance", 0))
    
    raw_pax = t.get("payload", {}).get("pax", 0)
    if raw_pax == 0 and f.get("type") != "cargo":
        raw_pax = int(f.get("payload", {}).get("pax", 0))
        
    cargo_kg = int(f.get("payload", {}).get("weights", {}).get("cargo", 0))
    rating = float(f.get("rating", 0.0))
    ftime = int(t.get("time", 0))
    
    pilot = f.get("pilot", {}).get("fullname", "Unknown Pilot")
    dep = f.get("dep", {}).get("icao", "???")
    arr = f.get("arr", {}).get("icao", "???")
    
    ac_data = f.get("aircraft", {})
    ac_icao = "Unknown"
    if isinstance(ac_data, dict):
        ac_icao = ac_data.get("icao") or ac_data.get("airframe", {}).get("icao") or "Unknown"
    check_g, check_fpm = 0.0, 0
    has_crash_title = False

    if "result" in f and "violations" in f["result"]:
        for v in f["result"]["violations"]:
            if "Crashed on landing" in v.get("title", ""):
                has_crash_title = True
                
            entry = v.get("entry", {}).get("payload", {}).get("touchDown", {})
            if entry and check_g == 0.0:
                check_g = float(entry.get("gForce", 0))
                check_fpm = int(entry.get("rate", 0))

    if check_g == 0 and "landing" in f:
        check_g = float(f.get("landing", {}).get("gForce", 0))
        check_fpm = int(f.get("landing", {}).get("rate", 0) or f.get("landing", {}).get("touchDownRate", 0))
        
    fpm_val = -abs(check_fpm) if check_fpm != 0 else 0
    
    is_hard_crash = abs(check_g) > 3.0 or abs(check_fpm) > 2000 or has_crash_title
    
    s["flights"] += 1
    s["earnings"] += balance
    s["pax"] += raw_pax
    s["cargo"] += cargo_kg
    s["rating_sum"] += rating
    s["fpm_sum"] += fpm_val
    s["g_sum"] += check_g
    
    s["pilots"][pilot] = s["pilots"].get(pilot, 0) + 1
    s["airports"][dep] = s["airports"].get(dep, 0) + 1
    s["airports"][arr] = s["airports"].get(arr, 0) + 1
    
    if "aircrafts" not in s: s["aircrafts"] = {}
    if ac_icao != "Unknown":
        s["aircrafts"][ac_icao] = s["aircrafts"].get(ac_icao, 0) + 1
    
    is_accelerated = False
    accel_scalar = t.get("prices", {}).get("costScalars", {}).get("timeAcceleration", 1.0)
    if accel_scalar < 1.0:
        is_accelerated = True
    elif "result" in f and "violations" in f["result"]:
        for v in f["result"]["violations"]:
            if "Time acceleration" in v.get("title", ""):
                is_accelerated = True
                break

    # 1. Hardest Landing - приймаємо ВСЕ, навіть краші!
    if fpm_val < s["records"]["hardest"]["fpm"]:
        s["records"]["hardest"] = {"fpm": fpm_val, "g": check_g, "pilot": pilot}

    # 2. Butter, Longest, Shortest - ТІЛЬКИ для безпечних (не крашів)
    if not is_hard_crash:
        if fpm_val < 0 and fpm_val > s["records"]["butter"]["fpm"]:
            s["records"]["butter"] = {"fpm": fpm_val, "g": check_g, "pilot": pilot}
            
        if ftime > 0 and not is_accelerated:
            flight_rec = {"time": ftime, "pilot": pilot, "dep": dep, "arr": arr}
            
            s["records"]["longest"].append(flight_rec)
            s["records"]["longest"] = sorted(s["records"]["longest"], key=lambda x: x["time"], reverse=True)[:3]
            
            s["records"]["shortest"].append(flight_rec)
            s["records"]["shortest"] = sorted(s["records"]["shortest"], key=lambda x: x["time"])[:3]
            
    save_weekly_stats(stats)

async def check_and_publish_weekly_stats(channel, state, ongoing_ids):
    stats = load_weekly_stats()
    if not stats: return
    
    current_week = get_iso_week()
    weeks_to_delete = []
    
    for week_tag, s in stats.items():
        if week_tag >= current_week:
            continue
            
        active_flight_exists = False
        for fid, fstate in state.items():
            if isinstance(fstate, dict) and not fstate.get("completed") and fstate.get("week") == week_tag:
                if fid in ongoing_ids:
                    active_flight_exists = True
                    break
                else:
                    print(f"👻 Ігноруємо завислий рейс (привид): {fid}")
                    
        if not active_flight_exists:
            try:
                pinned_msgs = await channel.pins()
                for p_msg in pinned_msgs:
                    if p_msg.author == client.user and p_msg.embeds:
                        if p_msg.embeds[0].title and "Weekly Summary" in p_msg.embeds[0].title:
                            await p_msg.unpin()
            except Exception as e:
                print(f"Error unpinning old real report: {e}")

            new_msg = await publish_weekly_embed(channel, week_tag, s)
            
            if new_msg:
                try:
                    await new_msg.pin()
                except Exception as e:
                    print(f"Error pinning message: {e}")

            try:
                owner = await client.fetch_user(ADMIN_IDS[0])
                dates_str = get_week_dates_string(week_tag)
                file_bin = io.BytesIO(json.dumps({week_tag: s}, indent=4).encode('utf-8'))
                
                await owner.send(
                    content=f"📁 **Архів тижня: {week_tag}** ({dates_str})", 
                    file=discord.File(file_bin, filename=f"weekly_stats_{week_tag}.json")
                )
            except Exception as e:
                print(f"Error sending stats DM to admin: {e}")

            weeks_to_delete.append(week_tag)
            
    if weeks_to_delete:
        for w in weeks_to_delete:
            del stats[w]
        save_weekly_stats(stats)

async def publish_weekly_embed(channel, week_tag, s):
	# --- НАШ БЕЗПЕЧНИЙ ЛОКАЛЬНИЙ СЛОВНИК ДЛЯ СТАТИСТИКИ ---
    manual_map = {'UK': 'UA', 'VH': 'HK'}
    def get_report_flag(icao):
        if not icao or icao == "???": return "🏳️"
        country = AIRPORTS_DB.get(icao.upper(), {}).get("country", "XX")
        if country == "XX" and len(icao) >= 2:
            country = manual_map.get(icao[:2].upper(), "XX")
        return get_flag(country)
    # -----------------------------------------------------
    dates_str = get_week_dates_string(week_tag)
    fl = s["flights"]
    if fl == 0: return None
    
    avg_rating = round(s["rating_sum"] / fl, 1)
    avg_fpm = int(s["fpm_sum"] / fl)
    avg_g = round(s["g_sum"] / fl, 2)
    
    medals = ["🥇", "🥈", "🥉"]
    
    # --- ТОП 3 ПІЛОТИ ---
    sorted_pilots = sorted(s.get("pilots", {}).items(), key=lambda x: x[1], reverse=True)[:3]
    pilots_str = ""
    if not sorted_pilots:
        pilots_str = "╰ None\n"
    else:
        for i, (p_name, p_flights) in enumerate(sorted_pilots):
            pilots_str += f"╰ {medals[i]} **{p_name}** ({p_flights} flights)\n"
            
    # --- ТОП 3 АЕРОПОРТИ ---
    sorted_apts = sorted(s.get("airports", {}).items(), key=lambda x: x[1], reverse=True)[:3]
    apts_str = ""
    if not sorted_apts:
        apts_str = "╰ None\n"
    else:
        for i, (apt_icao, apt_ops) in enumerate(sorted_apts):
            apt_flag = get_report_flag(apt_icao)
            apts_str += f"╰ {medals[i]} {apt_flag} **{apt_icao}** ({apt_ops} ops)\n"
            
    # --- ТОП 3 ЛІТАКИ ---
    sorted_acs = sorted(s.get("aircrafts", {}).items(), key=lambda x: x[1], reverse=True)[:3]
    acs_str = ""
    if not sorted_acs:
        acs_str = "╰ None\n"
    else:
        for i, (ac_icao, ac_flights) in enumerate(sorted_acs):
            acs_str += f"╰ {medals[i]} **{ac_icao}** ({ac_flights} flights)\n"
            
    rec = s["records"]
    
    def format_duration(minutes):
        return f"{int(minutes // 60):02d} hrs {int(minutes % 60):02d} mins"

    earn_val = s['earnings']
    sign = "+" if earn_val >= 0 else "-" 
    
    # --- БЛОК ДЛЯ ТОП-3 НАЙДОВШИХ ---
    longest_str = ""
    if not rec['longest']:
        longest_str = "╰ None\n"
    else:
        for i, r in enumerate(rec['longest']):
            l_dep = r.get('dep', '???')
            l_arr = r.get('arr', '???')
            l_route = f" ({get_report_flag(l_dep)} {l_dep} ➔ {get_report_flag(l_arr)} {l_arr})" if l_dep != "???" else ""
            longest_str += f"╰ {medals[i] if i < 3 else '🔹'} {format_duration(r['time'])} — **{r.get('pilot', 'Unknown')}**{l_route}\n"

    # --- БЛОК ДЛЯ ТОП-3 НАЙКОРОТШИХ ---
    shortest_str = ""
    if not rec['shortest']:
        shortest_str = "╰ None\n"
    else:
        for i, r in enumerate(rec['shortest']):
            s_dep = r.get('dep', '???')
            s_arr = r.get('arr', '???')
            s_route = f" ({get_report_flag(s_dep)} {s_dep} ➔ {get_report_flag(s_arr)} {s_arr})" if s_dep != "???" else ""
            shortest_str += f"╰ {medals[i] if i < 3 else '🔹'} {format_duration(r['time'])} — **{r.get('pilot', 'Unknown')}**{s_route}\n"
    
    desc = (
        f"### 📈 General Statistics\n"
        f"🛫 **Flights Completed:** {fl}\n"
        f"💰 **Airline Earnings:** {sign}{abs(earn_val):,} $\n"
        f"👫 **Passengers Carried:** {s['pax']:,} Pax\n"
        f"📦 **Cargo Carried:** {s['cargo']:,} kg\n\n"
        
        f"### 🏆 Weekly Records\n"
        f"👨‍✈️ **Most Active Pilots:**\n"
        f"{pilots_str}\n"
        
        f"🧈 **Butter Landing:**\n"
        f"╰ {rec['butter']['pilot']} ({rec['butter']['fpm']} fpm, {rec['butter']['g']} G)\n\n"
        
        f"🛬 **Hardest Landing:**\n"
        f"╰ {rec['hardest']['pilot']} ({rec['hardest']['fpm']} fpm, {rec['hardest']['g']} G)\n\n"
        
        f"🐢 **Longest Flights:**\n"
        f"{longest_str}\n"
        
        f"🚀 **Shortest Flights:**\n"
        f"{shortest_str}\n"
        
        f"### ⭐ Company Averages\n"
        f"📊 **Average Rating:** {avg_rating}\n"
        f"📉 **Average FPM:** {avg_fpm} fpm | {avg_g} G\n\n"
        
        f"📍 **Most Popular Airports:**\n"
        f"{apts_str}\n"
        
        f"✈️ **Most Popular Aircraft:**\n"
        f"{acs_str.rstrip()}"
    )
    
    embed = discord.Embed(
        title=f"📊 Weekly Summary for {dates_str}",
        description=desc,
        color=0x3498db
    )
    
    try: 
        msg = await channel.send(embed=embed)
        return msg 
    except Exception as e: 
        print(f"Error sending weekly embed: {e}")
        return None
# 🔥 КІНЕЦЬ БЛОКУ СТАТИСТИКИ 🔥

# --- 🎭 СТАНДАРТНІ СТАТУСИ ---
DEFAULT_STATUSES = [
    {"type": "play", "name": "🕹️Tracking with Newsky.app"}
]

def load_statuses():
    if not STATUS_FILE.exists():
        return list(DEFAULT_STATUSES)
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        if not data: return list(DEFAULT_STATUSES)
        return data
    except:
        return list(DEFAULT_STATUSES)

def save_statuses():
    try:
        STATUS_FILE.write_text(json.dumps(status_list, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Failed to save statuses: {e}")

status_list = load_statuses()
status_cycle = cycle(status_list)

def clean_text(text):
    if not text: return ""
    text = re.sub(r"\(.*?\)", "", text)
    removals = ["International", "Regional", "Airport", "Aerodrome", "Air Base", "Intl"]
    for word in removals:
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        text = pattern.sub("", text)
    return text.strip().strip(",").strip()

# --- 🌍 ЗАВАНТАЖЕННЯ БАЗИ ---
async def update_airports_db():
    global AIRPORTS_DB
    print("🌍 Downloading airports database...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(AIRPORTS_DB_URL) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    AIRPORTS_DB = {}
                    for k, v in data.items():
                        AIRPORTS_DB[k.upper()] = {
                            "country": v.get("country", "XX"),
                            "city": v.get("city", ""),
                            "name": v.get("name", "")
                        }
                    print(f"✅ Airports DB loaded! ({len(AIRPORTS_DB)} airports)")
                else:
                    print(f"⚠️ Failed to load airports DB: Status {resp.status}")
        except Exception as e:
            print(f"⚠️ Error loading DB: {e}")

def get_flag(country_code):
    if not country_code or country_code == "XX": return "🏳️"
    try:
        return "".join([chr(ord(c) + 127397) for c in country_code.upper()])
    except:
        return "🏳️"

# --- 🧠 РОЗУМНЕ ФОРМУВАННЯ НАЗВИ ---
def format_airport_string(icao, api_name):
    icao = icao.upper()
    db_data = AIRPORTS_DB.get(icao)
    
    if db_data:
        city = db_data.get("city", "") or ""
        name = db_data.get("name", "") or ""
        country = db_data.get("country", "XX")
        
        # ВИПРАВЛЕННЯ НАЗВ МІСТ
        CITY_FIXES = {
            "Kiev": "Kyiv",
            "Dnipropetrovsk": "Dnipro",
            "Kirovograd": "Kropyvnytskyi",
            "Nikolayev": "Mykolaiv",
            "Odessa": "Odesa",
            "Vinnitsa": "Vinnytsia",
            "Zaporizhia": "Zaporizhzhia",
            "Larnarca": "Larnaca",
            "Frankfurt-am-Main": "Frankfurt am Main",
            "Sharm el-Sheikh": "Sharm El Sheikh",
            "Ajaccio/Napoléon Bonaparte": "Ajaccio",
            "Ajaccio/Napoleon Bonaparte": "Ajaccio"
        }
        
        for old, new in CITY_FIXES.items():
            if city.lower() == old.lower(): 
                city = new
            name = name.replace(old, new)
        
        clean_name = clean_text(name)
        display_text = ""
        
        if city and clean_name:
            if city.lower() in clean_name.lower():
                display_text = clean_name
            else:
                display_text = f"{city} {clean_name}"
        elif clean_name:
            display_text = clean_name
        elif city:
            display_text = city
        else:
            display_text = clean_text(api_name)

        return f"{get_flag(country)} **{icao}** ({display_text})"
    
    flag = "🏳️"
    if len(icao) >= 2:
        prefix = icao[:2]
        manual_map = {'UK': 'UA', 'VH': 'HK'}
        code = manual_map.get(prefix, "XX")
        if code != "XX": flag = get_flag(code)

    return f"{flag} **{icao}** ({clean_text(api_name)})"

def get_timing(delay):
    try:
        d = float(delay)
        if d > 15: return f"🔴 **Delay** (+{int(d)} min)"
        if d < -15: return f"🟡 **Early** ({int(d)} min)"
        return "🟢 **On time**"
    except: return "⏱️ **N/A**"

def format_time(minutes):
    if not minutes: return "00:00"
    return f"{int(minutes // 60):02d}:{int(minutes % 60):02d}"

def get_rating_square(rating):
    try:
        r = float(rating)
        if r >= 8.0: return "🟩"
        if r >= 6.0: return "🟨" 
        if r >= 4.0: return "🟧"
        return "🟥"
    except: return "⬜"

# --- FPM + G-Force + Wind Search ---
def get_landing_data(f, details_type):
    import math
    
    if details_type == "test":
        fpm = -random.randint(50, 400)
        g = round(random.uniform(0.9, 1.8), 2)
        return f"📉 **{fpm} fpm**, **{g} G**\n<:wind:1482073151071326229> **220° | 8 kt** (Crosswind: 3 kt | Tailwind: 5 kt)"

    fpm, g_force, found = 0, 0.0, False
    weather = {}
    hdg = 0.0

    if "result" in f and "violations" in f["result"]:
        for v in f["result"]["violations"]:
            payload = v.get("entry", {}).get("payload", {})
            td = payload.get("touchDown", {})
            if td:
                fpm = int(td.get("rate", 0))
                g_force = float(td.get("gForce", 0))
                weather = payload.get("weather", {})
                hdg = float(payload.get("location", {}).get("hdg", 0)) if "location" in payload else 0.0
                found = True
                break

    if not found and "landing" in f and f["landing"]:
        payload = f["landing"]
        fpm = int(payload.get("rate", 0) or payload.get("touchDownRate", 0))
        g_force = float(payload.get("gForce", 0))
        weather = payload.get("weather", {})
        hdg = float(payload.get("location", {}).get("hdg", 0)) if "location" in payload else 0.0
        found = True

    if not found:
        val = f.get("lastState", {}).get("speed", {}).get("touchDownRate")
        if val: 
            fpm = int(val)
            hdg = float(f.get("lastState", {}).get("location", {}).get("hdg", 0))
            found = True

    if found and fpm != 0:
        fpm_val = -abs(fpm)
        g_str = f", **{g_force} G**" if g_force > 0 else ""
        
        wind_str = ""
        if weather and "windDir" in weather:
            w_dir = float(weather.get("windDir", 0))
            w_spd = float(weather.get("windSpd", 0))
            
            if round(w_dir) == 0 and w_spd > 0: 
                w_dir = 360.0
                
            comp = []
            
            angle_diff = math.radians(w_dir - hdg)
            headwind_raw = w_spd * math.cos(angle_diff)
            crosswind_raw = abs(w_spd * math.sin(angle_diff))
            
            if crosswind_raw >= 1.0:
                comp.append(f"Crosswind: {int(round(crosswind_raw))} kt")
                
            if headwind_raw <= -1.0:
                comp.append(f"Tailwind: {int(round(abs(headwind_raw)))} kt")
                
            extra_str = f" ({' | '.join(comp)})" if comp else ""
            wind_str = f"\n<:wind:1482073151071326229> **{int(round(w_dir))}° | {int(round(w_spd))} kt**{extra_str}"
            
        return f"📉 **{fpm_val} fpm**{g_str}{wind_str}"
    
    return "📉 **N/A**"

API_LOCK = None
GITHUB_DB_LOCK = asyncio.Lock()
KEY_USAGE_HISTORY = {key: [] for key in NEWSKY_API_KEYS}

async def fetch_api(session, path, method="GET", body=None):
    global KEY_USAGE_HISTORY, API_LOCK
    
    if API_LOCK is None:
        API_LOCK = asyncio.Lock()
    
    # 1. ШУКАЄМО ВІЛЬНИЙ КЛЮЧ
    async with API_LOCK:
        now = time.time()
        
        # Очищаємо історію від старих запитів для ВСІХ ключів (> 10 сек)
        for key in NEWSKY_API_KEYS:
            KEY_USAGE_HISTORY[key] = [t for t in KEY_USAGE_HISTORY[key] if now - t < 10.0]
        
        selected_key = None
        
        # Проходимося по ключах ПО ЧЕРЗІ (спочатку головний, потім резервний)
        for key in NEWSKY_API_KEYS:
            if len(KEY_USAGE_HISTORY[key]) < 5:
                selected_key = key
                break # Знайшли ключ, у якого менше 5 запитів! Зупиняємо пошук.
                
        # Якщо ВСІ ключі зайняті (у всіх по 5 запитів)
        if selected_key is None:
            # Знаходимо ключ, який звільниться найшвидше (у кого найстаріший запит)
            best_key_to_wait = min(NEWSKY_API_KEYS, key=lambda k: KEY_USAGE_HISTORY[k][0])
            oldest_request = KEY_USAGE_HISTORY[best_key_to_wait][0]
            wait_time = 10.0 - (now - oldest_request)
            
            if wait_time > 0:
                print(f"🚦 API Limit: Обидва ключі зайняті! Чекаємо {wait_time:.2f} сек... (Запит: {path})")
                await asyncio.sleep(wait_time)
                now = time.time()
                
            selected_key = best_key_to_wait
            
        # Записуємо використання вибраного ключа
        KEY_USAGE_HISTORY[selected_key].append(now)

    # 2. РОБИМО ЗАПИТ З ВИБРАНИМ КЛЮЧЕМ
    headers = {"Authorization": f"Bearer {selected_key}"}
    
    try:
        async with session.request(method, f"{BASE_URL}{path}", headers=headers, json=body, timeout=10) as r:
            if r.status == 200:
                return await r.json()
            elif r.status == 429:
                print(f"⚠️ Зловили 429 Too Many Requests на {path}! Сервер просить пригальмувати.")
                return None
            else:
                return None
    except Exception as e:
        print(f"⚠️ API Error ({path}): {e}")
        return None

# ---------- MESSAGE GENERATOR ----------
async def send_flight_message(channel, status, f, details_type="ongoing", reply_to_id=None):
    fid = f.get("_id") or f.get("id") or "test_id"
    if status == "Completed" or status == "Cancelled":
        flight_url = f"https://newsky.app/flight/{fid}"
    else:
        flight_url = f"https://newsky.app/map/{fid}"

	# ---  ВИЗНАЧЕННЯ ТИПУ РЕЙСУ (СМАЙЛИК) ---
    if f.get("schedule"):
        type_emoji = "<:sch:1496947924154847242>"
    elif f.get("charter"):
        type_emoji = "<:charter:1499317989483221063>"
    else:
        type_emoji = "<:free:1496947871604539392>"

    # --- 🌐 ВИЗНАЧЕННЯ МЕРЕЖІ (VATSIM/IVAO/OFFLINE) ---
    net_data = f.get("network")
    # Якщо network немає або ім'я null, то OFFLINE
    net = (net_data.get("name") if isinstance(net_data, dict) else str(net_data)) or "OFFLINE"

    cs = f.get("flightNumber") or f.get("callsign") or "N/A"
    airline = f.get("airline", {}).get("icao", "")
    full_cs = f"{airline} {cs}" if airline else cs
    
    dep_str = format_airport_string(f.get("dep", {}).get("icao", ""), f.get("dep", {}).get("name", ""))
    arr_str = format_airport_string(f.get("arr", {}).get("icao", ""), f.get("arr", {}).get("name", ""))
    
    # --- 🔀 ЛОГІКА DIVERSION (ЗМІНА АЕРОПОРТУ) ---
    planned_arr_icao = f.get("arr", {}).get("icao", "").upper()
    act_arr = f.get("actArr", {})
    
    if isinstance(act_arr, dict):
        act_arr_icao = act_arr.get("icao", "").upper()
        # Якщо фактичний ICAO є, і він відрізняється від запланованого
        if act_arr_icao and act_arr_icao != planned_arr_icao and act_arr_icao != "???":
            act_arr_str = format_airport_string(act_arr_icao, act_arr.get("name", ""))
            # Закреслюємо старий аеропорт і додаємо фактичний
            arr_str = f"~~{arr_str}~~ \u2003🔀\u2003 {act_arr_str}"
    # ---------------------------------------------
    
    ac = f.get("aircraft", {}).get("airframe", {}).get("name", "A/C")
    pilot = f.get("pilot", {}).get("fullname", "Pilot")
    
    raw_pax = 0
    if details_type == "result":
        raw_pax = f.get("result", {}).get("totals", {}).get("payload", {}).get("pax", 0)
    
    if raw_pax == 0 and f.get("type") != "cargo":
        raw_pax = f.get("payload", {}).get("pax", 0)
    
    flight_type = f.get("type", "pax")
    
    # --- ВИПРАВЛЕННЯ ВАГИ ВАНТАЖУ ---
    cargo_kg = int(f.get("payload", {}).get("weights", {}).get("cargo", 0))

    if flight_type == "cargo":
        payload_str = f"📦 **{cargo_kg}** kg"
    else:
        payload_str = f"👫 **{raw_pax}** Pax  |  📦 **{cargo_kg}** kg"

    embed = None
    arrow = " \u2003➡️\u2003 "

    if status == "Departed":
        delay = f.get("delay", 0)
        
        # --- РОЗРАХУНОК TAXI TIME ---
        taxi_str = ""
        try:
            t_gate_str = f.get("depTimeAct")
            t_air_str = f.get("takeoffTimeAct")
            
            if t_gate_str and t_air_str:
                t_gate = datetime.fromisoformat(t_gate_str.replace("Z", "+00:00"))
                t_air = datetime.fromisoformat(t_air_str.replace("Z", "+00:00"))
                diff = t_air - t_gate
                taxi_min = int(diff.total_seconds() // 60)
                taxi_str = f"🚕 **Taxi:** {taxi_min} min\n\n"
        except Exception as e:
            print(f"Taxi Calc Error: {e}")

        desc = (
            f"{dep_str}{arrow}{arr_str}\n\n"
            f"✈️ **{ac}**\n\n"
            f"{get_timing(delay)}\n" 
            f"{taxi_str}"            
            f"👨‍✈️ **{pilot}**\n\n"
            f"🌐 **{net.upper()}**\n\n"
            f"{payload_str}"
        )
        embed = discord.Embed(title=f"{type_emoji} 🛫 {full_cs} departed", url=flight_url, description=desc, color=0x3498db)

    elif status == "Completed":
        t = f.get("result", {}).get("totals", {})
        dist = t.get("distance", 0)
        ftime = t.get("time", 0)
        
        raw_balance = int(t.get("balance", 0))
        formatted_balance = f"{raw_balance:,}".replace(",", ".")
        rating = f.get("rating", 0.0)
        delay = f.get("delay", 0)
        
        check_g = 0.0
        check_fpm = 0
        has_crash_title = False # 🔴 НОВА ЗМІННА: Шукаємо краш у тексті
        
        if "result" in f and "violations" in f["result"]:
            for v in f["result"]["violations"]:
                # Перевіряємо наявність базової фрази крашу
                if "Crashed on landing" in v.get("title", ""):
                    has_crash_title = True
                    
                entry = v.get("entry", {}).get("payload", {}).get("touchDown", {})
                if entry and check_g == 0.0:
                    check_g = float(entry.get("gForce", 0))
                    check_fpm = int(entry.get("rate", 0))
                    
        if check_g == 0 and "landing" in f:
            check_g = float(f["landing"].get("gForce", 0))
            check_fpm = int(f["landing"].get("rate", 0) or f["landing"].get("touchDownRate", 0))

        title_text = f"{type_emoji} 😎 {full_cs} completed"
        color_code = 0x2ecc71
        rating_str = f"{get_rating_square(rating)} **{rating}**"

        # 🔥 Перевірка на краш (3G, 2000fpm АБО наявність фрази Crashed on landing) 🔥
        is_hard_crash = abs(check_g) > 3.0 or abs(check_fpm) > 2000 or has_crash_title
        
        time_info_str = f"{get_timing(delay)}\n\n"

        if is_hard_crash: 
            title_text = f"{type_emoji} 💥 {full_cs} CRASHED"
            color_code = 0x992d22 
            rating_str = "💀 **CRASH**"
            time_info_str = "" 
        
        elif f.get("emergency") is True:
            title_text = f"{type_emoji} ⚠️ {full_cs} EMERGENCY"
            color_code = 0xe67e22 
            rating_str = "🟥 **EMEG**"
            
        landing_info = get_landing_data(f, details_type)

        desc = (
            f"{dep_str}{arrow}{arr_str}\n\n"
            f"✈️ **{ac}**\n\n"
            f"{time_info_str}" 
            f"👨‍✈️ **{pilot}**\n\n"
            f"🌐 **{net.upper()}**\n\n"
            f"{landing_info}\n\n" 
            f"{payload_str}\n\n"
            f"📏 **{dist}** nm  |  ⏱️ **{format_time(ftime)}**\n\n"
            f"💰 **{formatted_balance} $**\n\n"
            f"{rating_str}"
        )
        embed = discord.Embed(title=title_text, url=flight_url, description=desc, color=color_code)

    elif status == "Cancelled":
        flight_duration = 0
        if f.get("durationAct"):
            flight_duration = f.get("durationAct")
        elif f.get("takeoffTimeAct") and f.get("lastState", {}).get("timestamp"):
            try:
                takeoff = datetime.fromisoformat(f.get("takeoffTimeAct").replace("Z", "+00:00"))
                last_ping = datetime.fromtimestamp(f["lastState"]["timestamp"] / 1000, tz=timezone.utc)
                flight_duration = int((last_ping - takeoff).total_seconds() // 60)
            except: pass

        desc = (
            f"{dep_str}{arrow}{arr_str}\n\n"
            f"✈️ **{ac}**\n\n"
            f"📍 **Status:** Flight Cancelled / Connection Lost\n"
            f"⏱️ **Flight time:** ~{flight_duration} min\n\n"
            f"👨‍✈️ **{pilot}**\n\n"
            f"🌐 **{net.upper()}**\n\n"
            f"{payload_str}"
        )
        embed = discord.Embed(title=f"⚫ {full_cs} flight cancelled", url=flight_url, description=desc, color=0x2b2d31)

    if embed:
        try:
            # --- СТВОРЮЄМО КНОПКИ ТІЛЬКИ ДЛЯ ЗАКРИТИХ РЕЙСІВ ---
            view = discord.ui.View(timeout=None)
            if status == "Completed":
                # 1. Кнопка статистики
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="🌍 Global Stats", custom_id=f"gstats_{fid}"))
                
                # 2. Кнопка кабінету пілота
                pilot_id = f.get("pilot", {}).get("_id", "unknown")
                if pilot_id != "unknown":
                    profile_url = f"https://kazuar.in.ua/pilot-cabinet.html#profile/{pilot_id}"
                    view.add_item(discord.ui.Button(label="Особистий кабінет пілота", style=discord.ButtonStyle.link, url=profile_url))
                
            # Розумна відправка (з кнопкою і реплаєм, якщо треба)
            kwargs = {"embed": embed}
            if status == "Completed": 
                kwargs["view"] = view
            if reply_to_id: 
                kwargs["reference"] = discord.MessageReference(message_id=reply_to_id, channel_id=CHANNEL_ID, fail_if_not_exists=False)

            sent_msg = await channel.send(**kwargs)
                
            # 🔥 МАГІЯ ГРОШЕЙ: Реакції залежно від суми прибутку 🔥
            if status == "Completed":
                t = f.get("result", {}).get("totals", {})
                balance = int(t.get("balance", 0))
                
                money_emojis = [
                    "<a:money:1499329373168209961>",
					"<a:fmp:1528514273628917770>"
                ]
                
                if balance > 100000:
                    jackpot_emojis = [
                        "<a:jackpot:1502622039868182760>"
                    ]
                    for emoji in jackpot_emojis:
                        try:
                            await sent_msg.add_reaction(emoji)
                        except Exception as e:
                            print(f"Jackpot reaction error: {e}")
                            
                elif balance > 50000:
                    # Якщо від 30к до 120к: вибираємо один рандомний грошовий
                    try:
                        await sent_msg.add_reaction(random.choice(money_emojis))
                    except Exception as e:
                        print(f"Money reaction error: {e}")
                        
            return sent_msg.id
        except Exception as e:
            print(f"Send error: {e}")
            return None

# --- РОЗУМНА СИСТЕМА СТАТУСІВ З ГРАФІКОМ (КИЇВСЬКИЙ ЧАС) ---
async def status_loop():
    await client.wait_until_ready()
    
    while not client.is_closed():
        # Отримуємо поточний час (UTC +3 години для літнього Києва)
        kyiv_time = datetime.now(timezone.utc) + timedelta(hours=3)
        hour = kyiv_time.hour
        
        # 1. ВИЗНАЧАЄМО МЕРЕЖЕВИЙ СТАТУС (КОЛІР) ЗА ГРАФІКОМ
        discord_status = discord.Status.online
        
        if 8 <= hour < 12:
            discord_status = discord.Status.online      # 08:00 - 12:00: В мережі (Зелений)
        elif 12 <= hour < 14:
            discord_status = discord.Status.idle        # 12:00 - 14:00: Відійшов (Жовтий місяць)
        elif 14 <= hour < 20:
            discord_status = discord.Status.online      # 14:00 - 20:00: В мережі (Зелений)
        else:
            discord_status = discord.Status.dnd         # 20:00 - 08:00: Не турбувати (Червоний)

        # 2. ВИЗНАЧАЄМО ТЕКСТОВИЙ СТАТУС
        current_status = next(status_cycle)
        activity_type = discord.ActivityType.playing
        
        if current_status["type"] == "watch":
            activity_type = discord.ActivityType.watching
        elif current_status["type"] == "listen":
            activity_type = discord.ActivityType.listening
            
        activity = discord.Activity(type=activity_type, name=current_status["name"])
        
        # 3. ВІДПРАВЛЯЄМО В DISCORD ОДНОЧАСНО
        try:
            await client.change_presence(status=discord_status, activity=activity)
        except Exception as e:
            print(f"Помилка оновлення статусу: {e}")
            
        # 4. РАХУЄМО ЧАС ДО НАСТУПНОЇ ГОДИНИ (напр. до 15:00:00)
        # Оновлюємо змінну часу, щоб врахувати мілісекунди, витрачені на відправку запиту
        now = datetime.now(timezone.utc) + timedelta(hours=3)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        
        # Визначаємо точну кількість секунд до початку наступної години
        sleep_seconds = (next_hour - now).total_seconds()
        
        # Бот засинає рівно до початку наступної години
        await asyncio.sleep(sleep_seconds)

# --- 🔍 ФУНКЦІЯ: Універсальний пошук повідомлень (DRY принцип) ---
async def find_discord_message(target_id, command_message):
    found_message = None
    main_channel = client.get_channel(CHANNEL_ID)
    
    if main_channel:
        try:
            found_message = await main_channel.fetch_message(target_id)
        except:
            pass
    
    if not found_message:
        await command_message.channel.send("🔍 **Searching for message...**")
        for guild in client.guilds:
            for channel in guild.text_channels:
                if channel.id == CHANNEL_ID: continue
                try:
                    found_message = await channel.fetch_message(target_id)
                    if found_message: break
                except:
                    continue
            if found_message: break
            
    return found_message
# -----------------------------------------------------------------

# --- 🌍 РАДАР КНОПОК СТАТИСТИКИ (GLOBAL STATS) ---
@client.event
async def on_interaction(interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        
        # Перевіряємо єдину кнопку
        if custom_id.startswith("gstats_"):
            flight_id = custom_id.split("_")[1]
            is_uk = str(interaction.locale) == 'uk'
            
            await interaction.response.defer(ephemeral=True)
            
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{flight_id}")
                
            if not det or "flight" not in det:
                err_msg_text = "❌ **Помилка:** Дані про рейс не знайдено в базі." if is_uk else "❌ **Error:** Flight data not found in the database."
                err_msg = await interaction.followup.send(err_msg_text, ephemeral=True)
                async def delete_err_msg():
                    await asyncio.sleep(120)
                    try: await err_msg.delete()
                    except: pass
                client.loop.create_task(delete_err_msg())
                return
                
            f = det["flight"]
            t = f.get("result", {}).get("totals", {})
            exp = f.get("result", {}).get("expenses", {})
            
            # ==========================================
            # --- 1. КАРТКА ПАЛИВА ---
            # ==========================================
            fuel_kg = int(t.get("fuel", 0))
            fuel_price = int(exp.get("fuel", 0) or t.get("fuelPrice", 0))
            dist = float(t.get("distance", 0))
            
            cost_per_ton = round((fuel_price / fuel_kg) * 1000) if fuel_kg > 0 else 0
            burn_per_nm = round(fuel_kg / dist, 1) if dist > 0 else 0
            
            if is_uk:
                fuel_desc = (
                    f"🛢️ **Спалено палива:** {fuel_kg:,} кг\n".replace(",", " ") +
                    f"💵 **Вартість палива:** {fuel_price:,} $\n".replace(",", " ") +
                    f"📊 **Ціна за 1 тонну:** {cost_per_ton:,} $\n".replace(",", " ") +
                    f"📏 **Витрата на милю:** {burn_per_nm} кг/nm"
                )
                embed_fuel = discord.Embed(title="⛽ Детальна статистика палива", description=fuel_desc, color=0xf1c40f)
            else:
                fuel_desc = (
                    f"🛢️ **Fuel Burned:** {fuel_kg:,} kg\n".replace(",", " ") +
                    f"💵 **Fuel Cost:** {fuel_price:,} $\n".replace(",", " ") +
                    f"📊 **Price per Tonne:** {cost_per_ton:,} $\n".replace(",", " ") +
                    f"📏 **Burn per NM:** {burn_per_nm} kg/nm"
                )
                embed_fuel = discord.Embed(title="⛽ Detailed Fuel Stats", description=fuel_desc, color=0xf1c40f)

            # ==========================================
            # --- 2. КАРТКА ПАСАЖИРІВ / КАРГО ---
            # ==========================================
            flight_type = f.get("type", "pax")
            
            if flight_type == "cargo":
                cargo_cap = int(f.get("payload", {}).get("cargoCapacity", 1))
                cargo_actual = int(t.get("payload", {}).get("cargo", 0))
                if cargo_actual == 0: cargo_actual = int(f.get("payload", {}).get("cargo", 0))
                
                load_factor = round((cargo_actual / cargo_cap) * 100, 1) if cargo_cap > 0 else 0.0
                cargo_kg = int(f.get("payload", {}).get("weights", {}).get("cargo", 0))
                
                cargo_rev = int(f.get("result", {}).get("revenue", {}).get("cargo", 0))
                unit_price = int(t.get("prices", {}).get("cargoUnitPrice", 0))
                total_expenses = int(f.get("result", {}).get("totals", {}).get("expenses", 0))
                balance = int(t.get("balance", 0))
                
                if is_uk:
                    payload_desc = (
                        f"⚖️ **Завантаженість об'єму:** {cargo_actual} / {cargo_cap} одиниць ({load_factor}%)\n"
                        f"📦 **Загальна маса вантажу:** {cargo_kg:,} кг\n\n".replace(",", " ") +
                        f"📉 **Економіка рейсу:**\n"
                        f"├ 💵 Дохід з вантажу: {cargo_rev:,} $ *(Тариф: {unit_price} $ / од.)*\n".replace(",", " ") +
                        f"├ 🧾 Витрати: {total_expenses:,} $\n".replace(",", " ") +
                        f"└ 💰 Чистий прибуток: {balance:,} $".replace(",", " ")
                    )
                    embed_payload = discord.Embed(title="📦 СТАТИСТИКА КОМЕРЦІЙНОГО ВАНТАЖУ", description=payload_desc, color=0xe67e22)
                else:
                    payload_desc = (
                        f"⚖️ **Volume Load:** {cargo_actual} / {cargo_cap} units ({load_factor}%)\n"
                        f"📦 **Total Cargo Weight:** {cargo_kg:,} kg\n\n".replace(",", " ") +
                        f"📉 **Flight Economics:**\n"
                        f"├ 💵 Cargo Revenue: {cargo_rev:,} $ *(Rate: {unit_price} $ / unit)*\n".replace(",", " ") +
                        f"├ 🧾 Expenses: {total_expenses:,} $\n".replace(",", " ") +
                        f"└ 💰 Net Profit: {balance:,} $".replace(",", " ")
                    )
                    embed_payload = discord.Embed(title="📦 COMMERCIAL CARGO STATS", description=payload_desc, color=0xe67e22)
            else:
                pax_cap = int(f.get("payload", {}).get("paxCapacity", 1))
                pax_actual = int(t.get("payload", {}).get("pax", 0))
                if pax_actual == 0: pax_actual = int(f.get("payload", {}).get("pax", 0))
                
                load_factor = round((pax_actual / pax_cap) * 100, 1) if pax_cap > 0 else 0.0
                
                classes_data = t.get("payload", {}).get("paxByClass", {})
                base_price = int(t.get("prices", {}).get("ticketPrice", 0))
                
                str_fare = "Тариф" if is_uk else "Fare"
                str_first = "Перший" if is_uk else "First"
                str_bus = "Бізнес" if is_uk else "Business"
                str_eco = "Економ" if is_uk else "Economy"
                str_pax = "пас." if is_uk else "pax"
                
                # Збираємо всі наявні класи в окремий список
                available_classes = []
                if classes_data.get("F", 0) > 0:
                    available_classes.append(f"🥂**{str_first}:** {classes_data['F']} {str_pax} ({str_fare}: {base_price * 4} $)")
                if classes_data.get("C", 0) > 0:
                    available_classes.append(f"💼**{str_bus}:** {classes_data['C']} {str_pax} ({str_fare}: {base_price * 2} $)")
                if classes_data.get("Y", 0) > 0:
                    available_classes.append(f"🎟️**{str_eco}:** {classes_data['Y']} {str_pax} ({str_fare}: {base_price} $)")

                # Формуємо красивий текст із правильними лініями
                classes_str = ""
                if not available_classes:
                    classes_str = "└ 🎟️ Немає даних про класи\n" if is_uk else "└ 🎟️ No class data available\n"
                else:
                    for i, cls_text in enumerate(available_classes):
                        # Якщо це останній елемент у списку — закриваємо гілку
                        if i == len(available_classes) - 1:
                            classes_str += f"└ {cls_text}\n"
                        # Усім іншим ставимо проміжну гілку
                        else:
                            classes_str += f"├ {cls_text}\n"
                    
                revenue_tickets = int(f.get("result", {}).get("revenue", {}).get("tickets", 0))
                total_expenses = int(f.get("result", {}).get("totals", {}).get("expenses", 0))
                balance = int(t.get("balance", 0))
                
                rev_per_pax = round(revenue_tickets / pax_actual) if pax_actual > 0 else 0
                exp_per_pax = round(total_expenses / pax_actual) if pax_actual > 0 else 0
                prof_per_pax = round(balance / pax_actual) if pax_actual > 0 else 0
                
                cargo_rev = int(f.get("result", {}).get("revenue", {}).get("cargo", 0))
                
                cargo_cap = int(f.get("payload", {}).get("cargoCapacity", 0))
                cargo_actual_pax_flight = int(t.get("payload", {}).get("cargo", 0))
                if cargo_actual_pax_flight == 0: cargo_actual_pax_flight = int(f.get("payload", {}).get("cargo", 0))
                cargo_load_factor = round((cargo_actual_pax_flight / cargo_cap) * 100, 1) if cargo_cap > 0 else 0.0
                cargo_unit_price = int(t.get("prices", {}).get("cargoUnitPrice", 0))
                
                if is_uk:
                    cargo_ext_str = f"╰ Завантаженість об'єму: {cargo_actual_pax_flight} / {cargo_cap} одиниць ({cargo_load_factor}%), (Тариф: {cargo_unit_price} $ / од.)" if cargo_cap > 0 and cargo_actual_pax_flight > 0 else ""
                else:
                    cargo_ext_str = f"╰ Volume Load: {cargo_actual_pax_flight} / {cargo_cap} units ({cargo_load_factor}%), (Rate: {cargo_unit_price} $ / unit)" if cargo_cap > 0 and cargo_actual_pax_flight > 0 else ""
                
                if is_uk:
                    payload_desc = (
                        f"👥 **Завантаженість:** {pax_actual} / {pax_cap} ({load_factor}%)\n\n"
                        f"💺 **Пасажири по класах:**\n{classes_str}\n"
                        f"📉 **Економіка на 1 пасажира:**\n"
                        f"├ 💵 Дохід: {rev_per_pax} $ | 🧾 Витрати: {exp_per_pax} $\n"
                        f"└ 💰 Чистий прибуток: {prof_per_pax} $\n\n"
                        f"📦 **Комерційний вантаж:** {cargo_rev:,} $\n".replace(",", " ") +
                        cargo_ext_str
                    )
                    embed_payload = discord.Embed(title="🎫 СТАТИСТИКА ПАСАЖИРІВ ТА КОМЕРЦІЇ", description=payload_desc, color=0x3498db)
                else:
                    payload_desc = (
                        f"👥 **Load Factor:** {pax_actual} / {pax_cap} ({load_factor}%)\n\n"
                        f"💺 **Passengers by Class:**\n{classes_str}\n"
                        f"📉 **Economics per Passenger:**\n"
                        f"├ 💵 Revenue: {rev_per_pax} $ | 🧾 Expenses: {exp_per_pax} $\n"
                        f"└ 💰 Net Profit: {prof_per_pax} $\n\n"
                        f"📦 **Commercial Cargo:** {cargo_rev:,} $\n".replace(",", " ") +
                        cargo_ext_str
                    )
                    embed_payload = discord.Embed(title="🎫 PASSENGER & COMMERCIAL STATS", description=payload_desc, color=0x3498db)

            # ==========================================
            # --- 3. КАРТКА АДВАНСЕД СТАТИСТИКИ ---
            # ==========================================
            sim = str(f.get("simulator", "Unknown")).lower()
            fps = "Невідомо" if is_uk else "Unknown"
            
            violations = f.get("result", {}).get("violations", [])
            for v in violations:
                entry = v.get("entry", {})
                payload = entry.get("payload", {})
                # Шукаємо FPS строго у події типу "landing"
                if entry.get("type") == "landing" and "system" in payload and "fps" in payload["system"]:
                    fps = payload["system"]["fps"]
                    break
                    
            # Резервний пошук (якщо раптом немає у violations, але є в прямому об'єкті landing)
            if fps in ["Невідомо", "Unknown"] and "landing" in f:
                if "system" in f["landing"] and "fps" in f["landing"]["system"]:
                    fps = f["landing"]["system"]["fps"]
            
            ac_cost = int(exp.get("aircraft", 0))
            fuel_cost = int(exp.get("fuel", 0))
            hand_cost = int(exp.get("handling", 0))
            land_cost = int(exp.get("landing", 0))
            total_exp = int(t.get("expenses", 0))
            
            scalars = t.get("prices", {}).get("costScalars", {})
            dep_sc = float(scalars.get("dep", 1.0))
            arr_sc = float(scalars.get("arr", 1.0))
            gnd_sc = float(scalars.get("ground", 1.0))
            fuel_sc = float(scalars.get("fuel", 1.0))
            time_accel = float(scalars.get("timeAcceleration", 1.0))
            
            def fmt_sc(val, uk):
                if val >= 1.0: return "Без знижки" if uk else "No discount"
                diff = int(round((1.0 - val) * 100))
                return f"-{diff}% до зборів" if uk else f"-{diff}% to fees"
            def fmt_sc_g(val, uk):
                if val >= 1.0: return "Без знижки" if uk else "No discount"
                diff = int(round((1.0 - val) * 100))
                return f"-{diff}%"
            
            penalties_str = ""
            for v in violations:
                points = float(v.get("penalty", {}).get("points", 0))
                cash = int(v.get("penalty", {}).get("cash", 0))
                if points < 0 or cash < 0:
                    title = v.get("title", "Unknown violation").replace("<br/>", " ")
                    rating_penalty = points / 100.0
                    if is_uk:
                        penalties_str += f"├ **{title}**\n└ ➖ Штраф: `{rating_penalty:.2f} рейтингу` | `-${abs(cash)}`\n"
                    else:
                        penalties_str += f"├ **{title}**\n└ ➖ Penalty: `{rating_penalty:.2f} rating` | `-${abs(cash)}`\n"
                        
            if not penalties_str:
                penalties_str = "└ ✅ Штрафів немає! Ідеальний політ." if is_uk else "└ ✅ No penalties! Perfect flight."
                
            if is_uk:
                adv_desc = (
                    f"🖥️ **Технічна інформація:**\n"
                    f"├ 🕹️ Симулятор: {sim}\n"
                    f"└ 📈 FPS на посадці: {fps}\n\n"
                    f"💸 **Деталізація витрат:**\n"
                    f"├ ✈️ Літак: {ac_cost:,} $\n".replace(",", " ") +
                    f"├ 🛢️ Паливо: {fuel_cost:,} $\n".replace(",", " ") +
                    f"├ 🧳 Хендлінг: {hand_cost:,} $\n".replace(",", " ") +
                    f"├ 🛬 Посадковий збір: {land_cost:,} $\n".replace(",", " ") +
                    f"└ 🧾 Всього витрат: {total_exp:,} $\n\n".replace(",", " ") +
                    f"🏷️ **Економічні коефіцієнти:**\n"
                    f"├ 🛫 Виліт: {dep_sc} ({fmt_sc(dep_sc, True)})\n"
                    f"├ 🛬 Приліт: {arr_sc} ({fmt_sc(arr_sc, True)})\n"
                    f"├ 🚜 Наземне обслуговування: {gnd_sc} ({fmt_sc_g(gnd_sc, True)})\n"
                    f"├ ⛽ Паливо: {fuel_sc} ({fmt_sc_g(fuel_sc, True)})\n"
                    f"└ ⏱️ Прискорення часу (timeAcceleration): {time_accel}\n\n"
                    f"🚨 **Штрафи та Порушення:**\n"
                    f"{penalties_str}"
                )
                embed_adv = discord.Embed(title="📊 Розширена Статистика Рейсу", description=adv_desc, color=0x95a5a6)
            else:
                adv_desc = (
                    f"🖥️ **Technical Info:**\n"
                    f"├ 🕹️ Simulator: {sim}\n"
                    f"└ 📈 Landing FPS: {fps}\n\n"
                    f"💸 **Detailed Expenses:**\n"
                    f"├ ✈️ Aircraft: {ac_cost:,} $\n".replace(",", " ") +
                    f"├ 🛢️ Fuel: {fuel_cost:,} $\n".replace(",", " ") +
                    f"├ 🧳 Handling: {hand_cost:,} $\n".replace(",", " ") +
                    f"├ 🛬 Landing: {land_cost:,} $\n".replace(",", " ") +
                    f"└ 🧾 Total Expenses: {total_exp:,} $\n\n".replace(",", " ") +
                    f"🏷️ **Economic Multipliers:**\n"
                    f"├ 🛫 Departure: {dep_sc} ({fmt_sc(dep_sc, False)})\n"
                    f"├ 🛬 Arrival: {arr_sc} ({fmt_sc(arr_sc, False)})\n"
                    f"├ 🚜 Ground Handling: {gnd_sc} ({fmt_sc_g(gnd_sc, False)})\n"
                    f"├ ⛽ Fuel: {fuel_sc} ({fmt_sc_g(fuel_sc, False)})\n"
                    f"└ ⏱️ timeAcceleration: {time_accel}\n\n"
                    f"🚨 **Penalties & Violations:**\n"
                    f"{penalties_str}"
                )
                embed_adv = discord.Embed(title="📊 Advanced Flight Stats", description=adv_desc, color=0x95a5a6)

            # ==========================================
            # --- 4. ВІДПРАВЛЕННЯ СТАТИСТИКИ (3 ЕМБЕДИ РАЗОМ) ---
            # ==========================================
            # Discord дозволяє передати масив з embed-ів, тоді вони будуть як окремі візуальні картки
            sent_stats_msg = await interaction.followup.send(embeds=[embed_fuel, embed_payload, embed_adv], ephemeral=True)
            
            async def delete_stats_msg():
                await asyncio.sleep(120)
                try: await sent_stats_msg.delete()
                except: pass
            client.loop.create_task(delete_stats_msg())

                        # 5. НАДСИЛАЄМО ЛОГ У КАНАЛ
            if interaction.user.id not in ADMIN_IDS:
                try:
                    log_channel = client.get_channel(1474817440516018186)
                    flight_cs = f.get("flightNumber") or f.get("callsign") or flight_id
                    msg_url = interaction.message.jump_url
                    user_locale = str(interaction.locale)
                    
                    log_desc = (
                        f"👤 **Користувач:** **{interaction.user.display_name}** (`{interaction.user.name}`) *(lang: {user_locale.upper()})*\n"
                        f"🆔 **ID акаунту:** `{interaction.user.id}`\n"
                        f"✈️ **Рейс:** {flight_cs}\n"
                        f"🔗 **Повідомлення:** [Перейти до звіту]({msg_url})\n"
                        f"🔍 **Дія:** Переглянув статистику."
                    )
                    log_embed = discord.Embed(title="🔔 Лог натискання кнопки", description=log_desc, color=0x3498db)
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    print(f"Помилка відправки логу в канал: {e}")
					
# -----------------------------------------------------------------

@client.event
async def on_message(message):
    global last_sent_message
    
    if message.author == client.user: return

	# --- 🥷 ФІЛЬТР: МИТТЄВЕ ВИДАЛЕННЯ ПОВІДОМЛЕНЬ ВІД ПРИХОВАНИХ ЮЗЕРІВ ---
    # Переводимо ID каналу в текст, бо JSON зберігає ключі як текст
    ch_id_str = str(message.channel.id) 
    if ch_id_str in HIDDEN_USERS and message.author.id in HIDDEN_USERS[ch_id_str]:
        try:
            await message.delete()
        except:
            pass # Якщо в бота немає прав на видалення — ігноруємо
        return # Зупиняємо подальшу обробку
    # ------------------------------------------------------------------------

    # --- 🕵️ ПЕРЕХОПЛЕННЯ ПП ---
    if isinstance(message.channel, discord.DMChannel):
        if message.author.id in ADMIN_IDS:
            pass
        else:
            try:
                owner = await client.fetch_user(ADMIN_IDS[0])
                # Додав ID, щоб тобі було зручно копіювати для команди !pmsg
                await owner.send(f"💬 **Нове повідомлення в ПП від {message.author.display_name} (`{message.author.name}`)**\n🆔 ID: `{message.author.id}`\n\n{message.content}")
                
                # Якщо користувач надіслав файли/картинки, бот перешле їх тобі
                if message.attachments:
                    files_to_forward = [await att.to_file() for att in message.attachments]
                    await owner.send(files=files_to_forward)
            except Exception as e:
                print(f"DM Intercept Error: {e}")

    is_admin = False
    if message.author.id in ADMIN_IDS:
        is_admin = True
    else:
        # 1. Якщо пишуть прямо на сервері
        if message.guild and message.author.guild_permissions.administrator:
            is_admin = True
        # 2. Якщо пишуть у ПП, перевіряємо їхні права на головному сервері
        elif not message.guild:
            main_channel = client.get_channel(CHANNEL_ID)
            if main_channel and main_channel.guild:
                try:
                    # Шукаємо цього користувача на сервері
                    member = main_channel.guild.get_member(message.author.id)
                    if not member: # Якщо його немає в кеші, робимо запит
                        member = await main_channel.guild.fetch_member(message.author.id)
                        
                    if member and member.guild_permissions.administrator:
                        is_admin = True
                except:
                    pass
    
    # --- 📥 КОМАНДА: !cache (СКАЧАТИ ВСІ ФАЙЛИ ПАМ'ЯТІ) ---
    if message.content == "!cache":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        folder_path = Path("/app/data")
        if not folder_path.exists():
            return await message.channel.send("⚠️ **Дані ще не створені.**")
            
        files_to_send = []
        # Проходимося по всіх файлах у папці /app/data
        for file in folder_path.iterdir():
            if file.is_file() and file.name.endswith(".json") and file.stat().st_size > 0:
                files_to_send.append(discord.File(file))
                
        if not files_to_send:
            return await message.channel.send("⚠️ **Файли кешу порожні або відсутні.**")
            
        await message.channel.send(
            content="📂 **Всі файли пам'яті бота:**", 
            files=files_to_send[:10] # Відправляємо всі знайдені файли (до 10 шт.)
        )
        return

#  -------------------------------------------------------------

# --- 🌍 КОМАНДА: !syncall (РОЗУМНИЙ ЕКСПОРТ З ПІДТВЕРДЖЕННЯМ ДЛЯ ВСІХ ТИЖНІВ) ---
    if message.content.startswith("!syncall"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        status_msg = await message.channel.send("⏳ **Запуск розумної глибокої синхронізації...** Викачую всі рейси до дна.")
        
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Викачуємо всі рейси (з анти-лімітом)
                flights_list = []
                skip_count = 0
                batch_size = 100
                retry_count = 0
                
                while True:
                    if retry_count == 0:
                        await status_msg.edit(content=f"⏳ Завантажую з Newsky (зміщення: {skip_count})...")
                        
                    body = {"count": batch_size, "skip": skip_count, "start": "2026-01-01T00:00:00Z"}
                    recent = await fetch_api(session, "/flights/recent", method="POST", body=body)
                    
                    # Якщо Newsky заблокував запит (429 ліміт), чекаємо 5 сек і пробуємо знову
                    if recent is None:
                        retry_count += 1
                        if retry_count > 5:
                            await status_msg.edit(content=f"❌ **Зупинка:** API Newsky не відповідає після 5 спроб. Вже викачано {len(flights_list)} рейсів.")
                            break
                        await asyncio.sleep(5)
                        continue
                        
                    retry_count = 0 # Успіх! Скидаємо лічильник
                    
                    if "results" not in recent or not recent["results"]: 
                        break # Справжнє дно списку
                        
                    flights_list.extend(recent["results"])
                    if len(recent["results"]) < batch_size: 
                        break # Менше 100 рейсів — це остання сторінка
                        
                    skip_count += batch_size
                    
                if not flights_list:
                    return await status_msg.edit(content="✅ **Рейсів не знайдено.**")

                await status_msg.edit(content=f"⏳ **Знайдено {len(flights_list)} рейсів. Читаю базу GitHub...**")
                
                # 2. Читаємо список файлів з GitHub
                gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Cache-Control": "no-cache"}
                dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
                
                github_files = {} # week_filename -> sha
                async with session.get(dir_url, headers=gh_headers) as dir_resp:
                    if dir_resp.status == 200:
                        for item in await dir_resp.json():
                            if item.get("name", "").endswith(".json"):
                                github_files[item["name"]] = item["sha"]

                # 3. Групуємо рейси та шукаємо відсутні
                ignored_list = load_ignored()
                missing_flights = []
                flights_list.sort(key=lambda x: x.get("updatedAt", ""))
                
                # Кеш для вже завантажених файлів тижнів
                loaded_weeks = {}
                
                valid_count = 0
                for raw_f in flights_list:
                    if raw_f.get("deleted") or not raw_f.get("close"): continue
                    fid = str(raw_f.get("_id") or raw_f.get("id"))
                    if fid in ignored_list: continue
                    
                    arrival_time = raw_f.get("arrTimeAct") or raw_f.get("close")
                    week_tag = get_iso_week(arrival_time)
                    week_filename = f"{week_tag}.json"
                    
                    # Завантажуємо файл тижня з GitHub, якщо ще не завантажили
                    if week_filename not in loaded_weeks:
                        loaded_weeks[week_filename] = []
                        if week_filename in github_files:
                            blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{github_files[week_filename]}"
                            async with session.get(blob_url, headers=gh_headers) as blob_resp:
                                if blob_resp.status == 200:
                                    blob_data = await blob_resp.json()
                                    loaded_weeks[week_filename] = json.loads(base64.b64decode(blob_data['content']).decode('utf-8'))
                    
                    # Перевіряємо, чи є рейс у цьому файлі
                    already_exists = any(
                        str(f.get("_id")) == fid or str(f.get("id")) == fid 
                        for p in loaded_weeks[week_filename] for f in p.get("flights", [])
                    )
                    
                    if not already_exists:
                        det = await fetch_api(session, f"/flight/{fid}")
                        if det and "flight" in det:
                            missing_flights.append((week_tag, det["flight"]))
                            
                    valid_count += 1
                    if valid_count % 50 == 0:
                        await status_msg.edit(content=f"⏳ **Звіряю з GitHub... Оброблено {valid_count}/{len(flights_list)} рейсів. Знайдено пропущених: {len(missing_flights)}**")

                if not missing_flights:
                    return await status_msg.edit(content="✅ **Усі рейси за всі тижні вже на GitHub!** Відсутніх рейсів немає.")

                # 4. Показуємо список ВСІХ рейсів (з розумним розбиттям на повідомлення)
                header = f"📋 **Маю додати ось такі рейси ({len(missing_flights)} шт):**\n"
                chunks = []
                current_chunk = header
                
                for w_tag, mf in missing_flights:
                    fid = mf.get("_id") or mf.get("id")
                    cs = mf.get("flightNumber") or mf.get("callsign") or "Unknown"
                    line = f"✈️ [{cs}](https://newsky.app/flight/{fid}) (Тиждень {w_tag})\n"
                    
                    # Ліміт Discord - 2000 символів, ми беремо із запасом 1900
                    if len(current_chunk) + len(line) > 1900:
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += line
                
                current_chunk += "\n✍️ Напиши `yes` щоб підтвердити, або `no` щоб скасувати (маєш 180 секунд)."
                chunks.append(current_chunk)

                await status_msg.edit(content=chunks[0], suppress=True)
                
                # Якщо шматків багато, відправляємо додаткові повідомлення
                extra_msgs = []
                for chunk in chunks[1:]:
                    msg = await message.channel.send(content=chunk, suppress_embeds=True)
                    extra_msgs.append(msg)

                def check(m):
                    return m.author == message.author and m.channel == message.channel and m.content.lower() in ['yes', 'no']

                try:
                    reply = await client.wait_for('message', check=check, timeout=180.0)
                    if reply.content.lower() == 'no':
                        return await status_msg.edit(content="❌ **Синхронізацію скасовано.**")
                except asyncio.TimeoutError:
                    return await status_msg.edit(content="⏳ **Час вийшов (180 сек).** Синхронізацію скасовано.")
                finally:
                    # Очищаємо додаткові повідомлення, щоб не засмічувати чат
                    for msg in extra_msgs:
                        try: await msg.delete()
                        except: pass

                # 5. КОРИСТУВАЧ ПІДТВЕРДИВ (yes) -> М'ясорубка і Єдиний Batch Push
                await status_msg.edit(content="⏳ **Підтверджено! Обробляю рейси та записую на GitHub єдиним комітом...**")
                
                async with GITHUB_DB_LOCK:
                    files_to_push = {}
                    
                    for w_tag, f in missing_flights:
                        week_filename = f"{w_tag}.json"
                        file_path = f"FLIGHTS/{week_filename}"
                        fid = str(f.get("_id") or f.get("id"))
                        
                        t = f.get("result", {}).get("totals", {})
                        if t.get("distance", 0) == 0 and t.get("time", 0) == 0: continue
                        
                        clean_flight = format_flight_for_db(f)
                        pilot_data = f.get("pilot", {})
                        pilot_id = pilot_data.get("_id", "unknown")
                        pilot_name = pilot_data.get("fullname", "Unknown Pilot")
                        pilot_avatar = pilot_data.get("avatar", "default")
                        
                        file_content = loaded_weeks[week_filename]
                        
                        existing_pilot = next((p for p in file_content if p.get("pilot_id") == pilot_id), None)
                        if existing_pilot:
                            existing_pilot["flights"].append(clean_flight)
                        else:
                            file_content.append({
                                "pilot_id": pilot_id, "fullname": pilot_name, "avatar": pilot_avatar, "flights": [clean_flight]
                            })
                            
                        # Готуємо файл до масового пушу
                        files_to_push[file_path] = json.dumps(file_content, ensure_ascii=False, indent=4)
                        
                    # Відправляємо єдиним комітом через push_to_github_batch
                    success = await push_to_github_batch(session, files_to_push, f"🤖 Auto-sync: added {len(missing_flights)} missing flights across multiple weeks")
                    
                    if success:
                        await status_msg.edit(content=f"✅ **Успіх!** Додано **{len(missing_flights)}** пропущених рейсів у {len(files_to_push)} файлів на GitHub (одним комітом).")
                    else:
                        await status_msg.edit(content=f"❌ **Помилка пакетного запису на GitHub.**")

        except Exception as e:
            await status_msg.edit(content=f"❌ **Сталася помилка:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🛠️ КОМАНДА: !patchnames (ДОДАТИ ТІЛЬКИ customName В СТАРІ ФАЙЛИ) ---
    if message.content == "!patchnames":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        # Список файлів, які треба опрацювати
        target_weeks = [f"2026-W{i:02d}.json" for i in range(2, 26)]
        status_msg = await message.channel.send(f"⏳ **Починаю патчинг файлів: {', '.join(target_weeks)}...**\n*(Додаю ТІЛЬКИ customName, ваші ручні зміни не чіпаю)*")
        
        gh_headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Cache-Control": "no-cache"
        }
        
        try:
            async with GITHUB_DB_LOCK:
                async with aiohttp.ClientSession() as session:
                    for week_file in target_weeks:
                        file_path = f"FLIGHTS/{week_file}"
                        await status_msg.edit(content=f"⏳ **Обробка файлу `{week_file}`...** (Завантаження бази з GitHub)")
                        
                        # 1. Отримуємо SHA файлу
                        file_sha = None
                        dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
                        async with session.get(dir_url, headers=gh_headers) as dir_resp:
                            if dir_resp.status == 200:
                                dir_data = await dir_resp.json()
                                if isinstance(dir_data, list):
                                    for item in dir_data:
                                        if item.get("name") == week_file:
                                            file_sha = item.get("sha")
                                            break
                                            
                        if not file_sha:
                            continue # Якщо файлу немає, просто пропускаємо
                            
                        # 2. Завантажуємо вміст файлу
                        blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                        async with session.get(blob_url, headers=gh_headers) as blob_resp:
                            if blob_resp.status != 200: continue
                            blob_data = await blob_resp.json()
                            text_content = base64.b64decode(blob_data['content']).decode('utf-8')
                            file_content = json.loads(text_content)
                            
                        # 3. Перебираємо рейси і ТОЧКОВО додаємо customName
                        total_flights = sum(len(p.get("flights", [])) for p in file_content)
                        processed = 0
                        changed = False
                        
                        for p in file_content:
                            for f in p.get("flights", []):
                                fid = str(f.get("_id") or f.get("id"))
                                
                                # Запит до Newsky чисто щоб дістати назву та ID борта
                                det = await fetch_api(session, f"/flight/{fid}")
                                if det and "flight" in det:
                                    ns_flight = det["flight"]
                                    custom_name = ns_flight.get("aircraft", {}).get("name")
                                    ac_id = ns_flight.get("aircraft", {}).get("_id")
                                    
                                    # Ювелірна операція: додаємо поля, інше не чіпаємо
                                    if "aircraft" in f:
                                        if custom_name:
                                            f["aircraft"]["customName"] = custom_name
                                            changed = True
                                        if ac_id:
                                            f["aircraft"]["_id"] = ac_id
                                            changed = True
                                        
                                processed += 1
                                if processed % 10 == 0:
                                    await status_msg.edit(content=f"⏳ **Обробка файлу `{week_file}`:** Звіряю з Newsky... ({processed}/{total_flights} рейсів)")
                                    
                        # 4. Зберігаємо файл назад на GitHub одним пушем
                        if changed:
                            await status_msg.edit(content=f"📤 **Обробка `{week_file}` завершена! Пушу оновлений файл на GitHub...**")
                            new_content_str = json.dumps(file_content, ensure_ascii=False, indent=4)
                            new_content_b64 = base64.b64encode(new_content_str.encode('utf-8')).decode('utf-8')
                            
                            push_payload = {
                                "message": f"🤖 Auto-patch: added customName to {week_file}",
                                "content": new_content_b64,
                                "sha": file_sha
                            }
                            
                            put_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
                            async with session.put(put_url, headers=gh_headers, json=push_payload) as put_resp:
                                if put_resp.status not in [200, 201]:
                                    print(f"Помилка запису {week_file}: {await put_resp.text()}")
                                    
            await status_msg.edit(content="✅ **Операція `!patchnames` успішно завершена!**\nТільки `customName` було додано, всі ваші ручні редагування ідеально збережені.")
            
        except Exception as e:
            await status_msg.edit(content=f"❌ **Помилка під час патчингу:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🔄 КОМАНДА: !syncghweek (СИНХРОНІЗАЦІЯ РЕЙСІВ ТИЖНЯ З ПІДТВЕРДЖЕННЯМ) ---
    if message.content.startswith("!syncghweek"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")

        # 1. Визначаємо початок тижня (вказаного або поточного)
        parts = message.content.split()
        now = datetime.now(timezone.utc)
        
        if len(parts) > 1 and parts[1].isdigit():
            target_week = int(parts[1])
            try:
                # Шукаємо понеділок для вказаного тижня у поточному році
                monday_start = datetime.fromisocalendar(now.year, target_week, 1).replace(tzinfo=timezone.utc)
                monday_start = monday_start.replace(hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                return await message.channel.send("❌ **Помилка:** Неіснуючий номер тижня.")
        else:
            # Якщо номер не вказано — беремо поточний тиждень
            monday_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

        start_iso = monday_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        current_week_tag = get_iso_week(start_iso) # Запускаємо через твою функцію для ідеального збігу формату

        status_msg = await message.channel.send(f"⏳ **Синхронізація з GitHub:** перевіряю рейси тижня (`{current_week_tag}`)...")

        try:
            async with aiohttp.ClientSession() as session:
                # 2. Завантажуємо рейси за тиждень з Newsky (з анти-лімітом)
                flights_list = []
                skip_count = 0
                batch_size = 100
                retry_count = 0
                
                while True:
                    if retry_count == 0:
                        await status_msg.edit(content=f"⏳ Завантажую з Newsky (зміщення: {skip_count})...")
                        
                    body = {"count": batch_size, "skip": skip_count, "start": start_iso}
                    recent = await fetch_api(session, "/flights/recent", method="POST", body=body)
                    
                    # Якщо Newsky заблокував запит (429 ліміт), чекаємо 5 сек і пробуємо знову
                    if recent is None:
                        retry_count += 1
                        if retry_count > 5:
                            await status_msg.edit(content=f"❌ **Зупинка:** API Newsky не відповідає після 5 спроб. Вже викачано {len(flights_list)} рейсів.")
                            break
                        await asyncio.sleep(5)
                        continue
                        
                    retry_count = 0 # Успіх! Скидаємо лічильник
                    
                    if not recent or "results" not in recent or not recent["results"]: 
                        break # Справжнє дно списку для цього тижня
                        
                    flights_list.extend(recent["results"])
                    if len(recent["results"]) < batch_size: 
                        break # Менше 100 рейсів — це остання сторінка
                        
                    skip_count += batch_size
                    
                total_found = len(flights_list)
                if total_found == 0:
                    return await status_msg.edit(content=f"✅ **За тиждень `{current_week_tag}` ще немає рейсів на Newsky.**")

                await status_msg.edit(content=f"⏳ **Знайдено {total_found} рейсів на Newsky. Читаю базу GitHub...**")

                # 3. Читаємо поточний файл тижня з GitHub
                week_filename = f"{current_week_tag}.json"
                file_path = f"FLIGHTS/{week_filename}"
                gh_headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                    "Cache-Control": "no-cache"
                }
                
                file_sha = None
                github_file_content = []
                
                dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
                async with session.get(dir_url, headers=gh_headers) as dir_resp:
                    if dir_resp.status == 200:
                        dir_data = await dir_resp.json()
                        if isinstance(dir_data, list):
                            for item in dir_data:
                                if item.get("name") == week_filename:
                                    file_sha = item.get("sha")
                                    break
                                    
                if file_sha:
                    blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                    async with session.get(blob_url, headers=gh_headers) as blob_resp:
                        if blob_resp.status == 200:
                            blob_data = await blob_resp.json()
                            text_content = base64.b64decode(blob_data['content']).decode('utf-8')
                            github_file_content = json.loads(text_content)

                # 4. Визначаємо відсутні рейси
                ignored_list = load_ignored()
                missing_flights = []
                flights_list.sort(key=lambda x: x.get("updatedAt", ""))

                for raw_f in flights_list:
                    if raw_f.get("deleted") or not raw_f.get("close"): continue
                    
                    fid = str(raw_f.get("_id") or raw_f.get("id"))
                    if fid in ignored_list: continue
                    
                    arrival_time = raw_f.get("arrTimeAct") or raw_f.get("close")
                    if get_iso_week(arrival_time) != current_week_tag: continue
                    
                    # Перевіряємо, чи рейс ВЖЕ Є на гітхабі
                    already_exists = any(
                        str(f.get("_id")) == fid or str(f.get("id")) == fid 
                        for p in github_file_content for f in p.get("flights", [])
                    )
                    
                    if not already_exists:
                        det = await fetch_api(session, f"/flight/{fid}")
                        if det and "flight" in det:
                            missing_flights.append(det["flight"])

                if not missing_flights:
                    return await status_msg.edit(content=f"✅ **Усі рейси за тиждень `{current_week_tag}` вже на GitHub!** Відсутніх рейсів немає.")

                # 5. Показуємо список ВСІХ рейсів (з розумним розбиттям на повідомлення)
                header = f"📋 **Маю додати ось такі рейси ({len(missing_flights)} шт) для {current_week_tag}:**\n"
                chunks = []
                current_chunk = header
                
                for mf in missing_flights:
                    fid = mf.get("_id") or mf.get("id")
                    cs = mf.get("flightNumber") or mf.get("callsign") or "Unknown"
                    line = f"✈️ [{cs}](https://newsky.app/flight/{fid})\n"
                    
                    if len(current_chunk) + len(line) > 1900:
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += line
                
                current_chunk += "\n✍️ Напиши `yes` щоб підтвердити, або `no` щоб скасувати (маєш 180 секунд)."
                chunks.append(current_chunk)

                await status_msg.edit(content=chunks[0], suppress=True)
                
                extra_msgs = []
                for chunk in chunks[1:]:
                    msg = await message.channel.send(content=chunk, suppress=True)
                    extra_msgs.append(msg)

                def check(m):
                    return m.author == message.author and m.channel == message.channel and m.content.lower() in ['yes', 'no']

                try:
                    reply = await client.wait_for('message', check=check, timeout=180.0)
                    if reply.content.lower() == 'no':
                        return await status_msg.edit(content="❌ **Синхронізацію скасовано.**")
                except asyncio.TimeoutError:
                    return await status_msg.edit(content="⏳ **Час вийшов (180 сек).** Синхронізацію скасовано.")
                finally:
                    # Очищаємо додаткові повідомлення
                    for msg in extra_msgs:
                        try: await msg.delete()
                        except: pass

                # 6. КОРИСТУВАЧ ПІДТВЕРДИВ (yes) -> М'ясорубка і Єдиний Пуш
                await status_msg.edit(content="⏳ **Підтверджено! Обробляю рейси та записую на GitHub єдиним комітом...**")
                
                async with GITHUB_DB_LOCK:
                    # Повторно витягуємо SHA, на випадок, якщо файл змінився під час 180 секунд очікування
                    async with session.get(dir_url, headers=gh_headers) as dir_resp:
                        if dir_resp.status == 200:
                            dir_data = await dir_resp.json()
                            if isinstance(dir_data, list):
                                for item in dir_data:
                                    if item.get("name") == week_filename:
                                        file_sha = item.get("sha")
                                        break
                                        
                    if file_sha:
                        blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                        async with session.get(blob_url, headers=gh_headers) as blob_resp:
                            if blob_resp.status == 200:
                                blob_data = await blob_resp.json()
                                github_file_content = json.loads(base64.b64decode(blob_data['content']).decode('utf-8'))

                    for f in missing_flights:
                        # Подвійний захист від дублів
                        fid = str(f.get("_id") or f.get("id"))
                        if any(str(ex_f.get("_id")) == fid for p in github_file_content for ex_f in p.get("flights", [])):
                            continue
                            
                        t = f.get("result", {}).get("totals", {})
                        if t.get("distance", 0) == 0 and t.get("time", 0) == 0: continue
                        
                        clean_flight = format_flight_for_db(f)
                        pilot_data = f.get("pilot", {})
                        pilot_id = pilot_data.get("_id", "unknown")
                        pilot_name = pilot_data.get("fullname", "Unknown Pilot")
                        pilot_avatar = pilot_data.get("avatar", "default")
                        
                        existing_pilot = next((p for p in github_file_content if p.get("pilot_id") == pilot_id), None)
                        if existing_pilot:
                            existing_pilot["flights"].append(clean_flight)
                        else:
                            github_file_content.append({
                                "pilot_id": pilot_id, "fullname": pilot_name, "avatar": pilot_avatar, "flights": [clean_flight]
                            })
                            
                    # Пушимо одним комітом
                    new_content_str = json.dumps(github_file_content, ensure_ascii=False, indent=4)
                    new_content_b64 = base64.b64encode(new_content_str.encode('utf-8')).decode('utf-8')
                    
                    push_payload = {
                        "message": f"🤖 Auto-sync: confirmed addition of {len(missing_flights)} flights to {current_week_tag}",
                        "content": new_content_b64
                    }
                    if file_sha: push_payload["sha"] = file_sha
                    
                    put_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
                    async with session.put(put_url, headers=gh_headers, json=push_payload) as put_resp:
                        if put_resp.status in [200, 201]:
                            await status_msg.edit(content=f"✅ **Успіх!** Додано **{len(missing_flights)}** рейсів у файл `{week_filename}` на GitHub.")
                        else:
                            await status_msg.edit(content=f"❌ **Помилка запису на GitHub:** {await put_resp.text()}")

        except Exception as e:
            await status_msg.edit(content=f"❌ **Сталася помилка:** {e}")
        return
    # -------------------------------------------------------------
	
# --- 📥 КОМАНДА: !syncweek (СИНХРОНІЗАЦІЯ ЗА ЧАСОМ ЗАКРИТТЯ РЕЙСІВ) ---
    if message.content.startswith("!syncweek"):
        if not is_admin: 
            return await message.channel.send("🚫 **Access Denied**")
        
        # 1. Читаємо кількість рейсів (за замовчуванням 100)
        parts = message.content.split()
        target_count = 100
        if len(parts) > 1 and parts[1].isdigit():
            target_count = int(parts[1])
            
        # 2. Визначаємо початок поточного тижня (Понеділок, 00:00:00 UTC)
        now = datetime.now(timezone.utc)
        monday = now - timedelta(days=now.weekday())
        monday_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        start_iso = monday_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        current_week_tag = get_iso_week()
        
        status_msg = await message.channel.send(f"⏳ **Рахую рейси (ліміт: {target_count}) з початку тижня ({monday_start.strftime('%d.%m.%Y')})...**")
        
        try:
            async with aiohttp.ClientSession() as session:
                # --- НОВИЙ БЛОК ЗБОРУ РЕЙСІВ (ПАГІНАЦІЯ) ---
                flights_list = []
                skip_count = 0
                
                while len(flights_list) < target_count:
                    request_count = min(100, target_count - len(flights_list))
                    
                    body = {
                        "count": request_count, 
                        "start": start_iso,
                        "skip": skip_count
                    }
                    recent = await fetch_api(session, "/flights/recent", method="POST", body=body)
                    
                    if not recent or "results" not in recent:
                        if skip_count == 0:
                            return await status_msg.edit(content="❌ **Помилка:** Не вдалося отримати список рейсів від Newsky.")
                        break
                    
                    batch = recent["results"]
                    flights_list.extend(batch)
                    
                    if len(batch) < request_count:
                        break
                        
                    skip_count += request_count
                # ---------------------------------------------
                
                # --- СТАРА ЛОГІКА ОБРОБКИ (БЕЗ ЗМІН) ---
                total_found = len(flights_list)
                added_count = 0
                
                if total_found == 0:
                    return await status_msg.edit(content="✅ **За цей тиждень ще немає завершених рейсів.**")
                
                # 🔥 МАГІЯ ТУТ: Сортуємо строго за часом ЗАКРИТТЯ рейсу (updatedAt)
                # Від тих, що закрилися першими, до тих, що закрилися щойно.
                # Це повністю імітує роботу живого бота!
                flights_list.sort(key=lambda x: x.get("updatedAt", ""))
                
                await status_msg.edit(content=f"⏳ **Знайдено {total_found} рейсів. Додаю в порядку їх закриття...**")

                ignored_list = load_ignored()
                fid = "Невідомо"
                for raw_f in flights_list:
                    if raw_f.get("deleted") or not raw_f.get("close"):
                        continue
                        
                    fid = str(raw_f.get("_id") or raw_f.get("id"))
                    
                    if fid in ignored_list:
                        continue
                    
                    det = await fetch_api(session, f"/flight/{fid}")
                    if not det or "flight" not in det: 
                        continue
                        
                    f = det["flight"]
                    t = f.get("result", {}).get("totals", {})
                    
                    if t.get("distance", 0) == 0 and t.get("time", 0) == 0:
                        continue
                        
                    sched_time = f.get("depTime") or f.get("createdAt")
                    week_tag = get_iso_week(sched_time)
                    
                    if week_tag != current_week_tag:
                        continue
                    
                    update_weekly_stats(f, week_tag)
                    added_count += 1
                    
                    if added_count % 5 == 0:
                        await status_msg.edit(content=f"⏳ **Синхронізація: додано {added_count} / {total_found} рейсів...**")
                        
                await status_msg.edit(content=f"✅ **Синхронізація завершена!**\nДодано **{added_count}** рейсів у статистику так, якби бот був онлайн весь тиждень.")
                
        except Exception as e:
            await status_msg.edit(content=f"❌ **Сталася помилка на рейсі {fid}:** {e}")
        return
    # -------------------------------------------------------------
    
    # --- 🧪 КОМАНДА: !testcharter (ПРОТЕСТУВАТИ ДИЗАЙН ЧАРТЕРУ - ТІЛЬКИ В ПП) ---
    if message.content == "!testcharter":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        # 🔥 Перевірка на те, чи це ПП (Direct Messages) 🔥
        if not isinstance(message.channel, discord.DMChannel):
            return await message.channel.send("⚠️ **This command ONLY works in Direct Messages (DM)!** Please send me a private message.")
        
        status_msg = await message.channel.send("⏳ **Шукаю будь-який чартер для тесту дизайну...**")
        try:
            ns_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
                "Accept": "application/json"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get("https://newsky.app/api/charter/recent/pax", headers=ns_headers, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            # Беремо просто перший чартер зі списку для тесту
                            charter = data[0] 
                            
                            dep_icao = charter.get("dep", {}).get("icao", "")
                            arr_icao = charter.get("arr", {}).get("icao", "")
                            
                            dep_str = format_airport_string(dep_icao, charter.get("dep", {}).get("name", ""))
                            arr_str = format_airport_string(arr_icao, charter.get("arr", {}).get("name", ""))
                            
                            exp_dt = datetime.fromisoformat(charter.get("activeTo", "").replace("Z", "+00:00"))
                            exp_str = exp_dt.strftime("%d %b, %H:%M UTC")
                            
                            quota = charter.get("quota", 0)
                            dist = charter.get("distance", 0)
                            
                            desc = (
                                    f"{dep_str} ➔ {arr_str}\n\n"
                                    f"📊 **Contract Info**\n"
                                    f"╰ 👫 Quota: **{quota} Pax**\n"
                                    f"╰ 📏 Dist: **{dist} nm**\n"
                                    f"╰ ⏳ Exp: **{exp_str}**"
                                )
                            
                            embed = discord.Embed(
                                title="<:charter:1499317989483221063> New Charter Contract Available!", 
                                description=desc, 
                                color=0xffcc00
                            )
                            
                            await message.channel.send(content="🛠️ **Ось так виглядатиме сповіщення:**", embed=embed)
                            await status_msg.delete()
                        else:
                            await status_msg.edit(content="❌ **Наразі немає жодного чартеру в API для тесту.**")
        except Exception as e:
            await status_msg.edit(content=f"❌ **Помилка:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🔄 КОМАНДА: !updatedemand (ПРИМУСОВЕ ОНОВЛЕННЯ GITHUB) ---
    if message.content == "!updatedemand":
        if not is_admin: 
            return await message.channel.send("🚫 **Access Denied**")

        status_msg = await message.channel.send("⏳ **Step 1: Fetching raw demand data...**")

        try:
            async with GITHUB_DB_LOCK:
                async with aiohttp.ClientSession() as session:
                    demand_content = await fetch_demand_data(session)
                    if not demand_content:
                        return await status_msg.edit(content="❌ **Error:** Failed to fetch or parse demand data.")
                    
                    files_to_push = {GITHUB_FILE_PATH: demand_content}
                    
                    # Отримуємо чартери локально ДО коміту
                    charter_results = await run_analytics_pipeline(session, demand_content=demand_content, ctx=message.channel)
                    if charter_results:
                        files_to_push["newsky-charter-results.txt"] = charter_results
                        
                    # Відправляємо обидва файли одним пушем
                    success = await push_to_github_batch(session, files_to_push, "🤖 Manual Demand & Tops Update")
                    
                    if success:
                        await status_msg.edit(content="✅ **All files updated on GitHub in ONE commit!** 🚀")
                    else:
                        await status_msg.edit(content="❌ **Error pushing to GitHub.**")
        except Exception as e:
            await status_msg.edit(content=f"❌ **Error:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🧪 КОМАНДА: !testgh <ID> (ПРИМУСОВИЙ ЗАПИС РЕЙСУ НА GITHUB) ---
    if message.content.startswith("!testgh"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!testgh <Flight_ID>`")
            
        fid = parts[1]
        status_msg = await message.channel.send(f"⏳ **Шукаю рейс `{fid}` та записую на GitHub...**")
        
        try:
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{fid}")
                
                if not det or "flight" not in det:
                    return await status_msg.edit(content=f"❌ **Помилка:** Рейс `{fid}` не знайдено в API.")
                
                f = det["flight"]
                cs = f.get("flightNumber") or f.get("callsign") or "Unknown"
                
                # 1. Визначаємо тиждень за часом посадки
                arrival_time = f.get("arrTimeAct") or f.get("close")
                target_week = get_iso_week(arrival_time)
                
                # 2. Форматуємо рейс
                clean_flight = format_flight_for_db(f)
                
                # 3. Дістаємо дані пілота
                pilot_data = f.get("pilot", {})
                pilot_id = pilot_data.get("_id", "unknown")
                pilot_name = pilot_data.get("fullname", "Unknown Pilot")
                pilot_avatar = pilot_data.get("avatar", "default")
                
                # 4. Записуємо на GitHub (тут використовуємо await, щоб дочекатися результату)
                await save_flight_to_github(clean_flight, pilot_id, pilot_name, pilot_avatar, target_week)
                
                await status_msg.edit(content=f"✅ **Успіх!** Рейс `{cs}` (ID: `{fid}`) відправлено на GitHub у папку `FLIGHTS/{target_week}.json`!")
                
        except Exception as e:
            await status_msg.edit(content=f"❌ **Внутрішня помилка:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🌪️ КОМАНДА: !testwind <ID> (ТЕСТОВЕ ПОВІДОМЛЕННЯ ПРО ПОСАДКУ) ---
    if message.content.startswith("!testwind"):
        if not is_admin: 
            return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!testwind <Flight_ID>`")
        
        fid = parts[1]
        msg = await message.channel.send(f"⏳ **Fetching data for flight `{fid}`...**")
        
        try:
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{fid}")
                
                if not det or "flight" not in det:
                    return await msg.edit(content=f"❌ **Error:** Flight `{fid}` not found in API.")
                
                f = det["flight"]
                
                # Відправляємо тестове повідомлення "Completed" у той же канал (або ПП)
                await send_flight_message(message.channel, "Completed", f, "result")
                
                await msg.edit(content=f"✅ **Test message generated successfully!** (Stats were NOT updated)")
                
        except Exception as e:
            await msg.edit(content=f"❌ **Internal error occurred:** {e}")
        return
    # -------------------------------------------------------------

	# --- ✏️ КОМАНДА: !rename <Channel_ID> <нова назва> (ПЕРЕЙМЕНУВАТИ КАНАЛ) ---
    if message.content.startswith("!rename"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split(" ", 2)
        if len(parts) < 3:
            return await message.channel.send("⚠️ **Format:** `!rename <Channel_ID> <нова_назва>`")
            
        target_id_str = parts[1]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ **Error:** Channel ID має бути числом.")
             
        target_channel_id = int(target_id_str)
        new_name = parts[2]
        
        # Визначаємо сервер (працюватиме навіть якщо ти пишеш команду в ПП)
        main_channel = client.get_channel(CHANNEL_ID)
        guild = message.guild if message.guild else (main_channel.guild if main_channel else None)
        
        if not guild:
            return await message.channel.send("❌ **Error:** Не вдалося визначити сервер.")
            
        # Шукаємо канал
        target_channel = guild.get_channel(target_channel_id)
        if not target_channel:
             try:
                 target_channel = await guild.fetch_channel(target_channel_id)
             except:
                 return await message.channel.send("❌ **Error:** Канал з таким ID не знайдено.")
                 
        try:
            old_name = target_channel.name
            await target_channel.edit(name=new_name)
            await message.add_reaction("✅")
            await message.channel.send(f"✅ **Успіх!** Канал `{old_name}` перейменовано на `{new_name}`.")
            
        except discord.Forbidden:
            await message.channel.send("❌ **Error:** У бота немає прав керувати цим каналом (потрібен дозвіл Manage Channels).")
        except discord.HTTPException as e:
            # Відловлюємо ліміти Discord (Rate Limit: 2 перейменування на 10 хвилин)
            if e.status == 429:
                await message.channel.send("⏳ **Discord Rate Limit:** Ти занадто часто перейменовував цей канал. Почекай 10 хвилин і спробуй ще раз.")
            else:
                await message.channel.send(f"❌ **HTTP Error:** {e}")
        except Exception as e:
            await message.channel.send(f"❌ **Error:** {e}")
            
        return
    # -------------------------------------------------------------

	# --- 🧪 КОМАНДА 1: !teststatspin (ЗІ ЗАКРІПЛЕННЯМ ТА ВІДКРІПЛЕННЯМ) ---
    if message.content == "!teststatspin":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        stats = load_weekly_stats()
        if not stats: return await message.channel.send("⚠️ **Stats file is empty.**")
        
        await message.channel.send("🛠️ **Generating real report (WITH pins)...**")
        try:
            pinned_msgs = await message.channel.pins()
            for p_msg in pinned_msgs:
                if p_msg.author == client.user and p_msg.embeds and p_msg.embeds[0].title and "Weekly Summary" in p_msg.embeds[0].title:
                    await p_msg.unpin()
        except Exception as e:
            print(f"Error unpinning: {e}")
            
        for week_tag, s in stats.items():
            new_msg = await publish_weekly_embed(message.channel, week_tag, s)
            if new_msg:
                try: await new_msg.pin()
                except Exception as e: print(f"Error pinning: {e}")
        return

    # --- 📌 КОМАНДА 2: !teststats (БЕЗ ЗАКРІПЛЕННЯ) ---
    if message.content == "!teststats":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        stats = load_weekly_stats()
        if not stats: return await message.channel.send("⚠️ **Stats file is empty.**")
        
        await message.channel.send("🛠️ **Generating real report (NO pins)...**")
        for week_tag, s in stats.items():
            await publish_weekly_embed(message.channel, week_tag, s)
        return

    # --- 🤡 КОМАНДА 3: !teststatstest (ФЕЙКОВІ ДАНІ ДЛЯ ПРЕЗЕНТАЦІЇ) ---
    if message.content == "!teststatstest":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        await message.channel.send("🛠️ **Generating presentation report (Randomized Fake Data)...**")
        
        # Рандомні базові значення
        f_count = random.randint(80, 200)
        
        # Допоміжна функція для генерації випадкових рейсів (ТОП-3)
        def gen_random_flights(time_min, time_max):
            apts = ["UKBB", "UKLL", "UKOO", "UKLU", "EPWA", "LOWW", "KJFK", "LTFM", "EDDF"]
            return [
                {
                    "time": random.randint(time_min, time_max),
                    "pilot": f"Pilot {random.choice(['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo'])}",
                    "dep": random.choice(apts),
                    "arr": random.choice(apts)
                } for _ in range(3)
            ]

        # Генеруємо красиві штучні дані
        dummy_s = {
            "flights": f_count, 
            "earnings": f_count * random.randint(8000, 20000), 
            "pax": f_count * random.randint(120, 250), 
            "cargo": f_count * random.randint(1500, 5000),
            
            "rating_sum": f_count * random.uniform(9.0, 9.9), 
            "fpm_sum": f_count * random.randint(-350, -150),    
            "g_sum": f_count * random.uniform(1.05, 1.25),      
            
            "pilots": {f"Capt. {name}": random.randint(10, 40) for name in ["Shevchenko", "Kovalenko", "Boyko"]},
            "airports": {apt: random.randint(15, 60) for apt in ["UKBB", "UKLL", "LOWW"]},
            "aircrafts": {ac: random.randint(20, 70) for ac in ["B738", "A320", "B77W"]},
            
            "records": {
                "butter": {"fpm": random.randint(-60, -10), "g": round(random.uniform(1.0, 1.05), 2), "pilot": "Capt. Silk"},
                "hardest": {"fpm": random.randint(-800, -500), "g": round(random.uniform(1.6, 2.2), 2), "pilot": "Capt. Ryan"},
                
                # Рандомні списки для ТОП-3 (відсортовані)
                "longest": sorted(gen_random_flights(400, 700), key=lambda x: x["time"], reverse=True),
                "shortest": sorted(gen_random_flights(25, 60), key=lambda x: x["time"])
            }
        }
        
        current_week = get_iso_week()
        await publish_weekly_embed(message.channel, current_week, dummy_s)
        return

    # --- 🧹 КОМАНДА: !clearstats (ОЧИСТИТИ ВСЮ СТАТИСТИКУ З ПЕРЕВІРКОЮ) ---
    if message.content == "!clearstats":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        # Генеруємо два числа від 3 до 9
        num1 = random.randint(3, 9)
        num2 = random.randint(3, 9)
        correct_answer = num1 * num2
        
        await message.channel.send(f"⚠️ **WARNING!** You are about to completely wipe all weekly statistics.\nTo confirm this action, please solve this math problem:\n**What is {num1} x {num2}?**\n*(Just type the number in the chat, you have 10 seconds)*")
        
        # Фільтр: бот прийме відповідь тільки від того, хто запустив команду, і тільки в цьому ж каналі
        def check(m):
            return m.author == message.author and m.channel == message.channel
            
        try:
            # Бот чекає на повідомлення 10 секунд
            reply = await client.wait_for('message', check=check, timeout=10.0)
            
            # Перевіряємо, чи зійшлася математика
            if reply.content.strip() == str(correct_answer):
                save_weekly_stats({})
                await message.channel.send("🗑️ **Stats file (`weekly_stats.json`) completely wiped!**")
            else:
                await message.channel.send(f"❌ **Wrong answer (it should be {correct_answer}).** Protection triggered, deletion cancelled! 😅")
                
        except asyncio.TimeoutError:
            # Якщо за 20 секунд ніхто нічого не написав
            await message.channel.send("⏳ **Time's up.** Deletion operation cancelled.")
            
        return
    # -------------------------------------------------------------

	# --- 🛑 КОМАНДА: !ignore <ID> (ІГНОРУВАТИ БАГОВАНИЙ РЕЙС) ---
    if message.content.startswith("!ignore"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!ignore <Flight_ID>`")
        
        fid = parts[1]
        ignored_list = load_ignored()
        if fid not in ignored_list:
            ignored_list.append(fid)
            save_ignored(ignored_list)
        
        await message.channel.send(f"🛑 **Рейс `{fid}` назавжди додано в `ignored.json`!**")
        return
    # -------------------------------------------------------------

	# --- ➕ КОМАНДА: !addghflight <ID> [emer] (ЗАПИСАТИ РЕЙС НА GITHUB ВРУЧНУ) ---
    if message.content.startswith("!addghflight"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ **Формат:** `!addghflight <Flight_ID> [emer]`")
            
        fid = parts[1]
        # Перевіряємо, чи є третє слово і чи це "emer"
        force_emer = len(parts) > 2 and parts[2].lower() == "emer"
        
        status_text = f"⏳ **Шукаю рейс `{fid}` в API Newsky...**"
        if force_emer:
            status_text += "\n*(Увімкнено режим примусового EMERGENCY 🚨)*"
            
        msg = await message.channel.send(status_text)
        
        try:
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{fid}")
                
                if not det or "flight" not in det:
                    return await msg.edit(content=f"❌ **Помилка:** Рейс `{fid}` не знайдено в API.")
                
                f = det["flight"]
                cs = f.get("flightNumber") or f.get("callsign") or "Unknown"
                
                # 1. Пропускаємо через м'ясорубку
                clean_flight = format_flight_for_db(f)
                
                # 2. Якщо вказано emer — імітуємо СПРАВЖНІЙ emergency!
                if force_emer:
                    clean_flight["emergency"] = True
                    
                    if "result" in clean_flight:
                        # Анулюємо фінансові показники
                        if "totals" in clean_flight["result"]:
                            clean_flight["result"]["totals"]["expenses"] = 0
                            clean_flight["result"]["totals"]["penalties"] = 0
                            clean_flight["result"]["totals"]["revenue"] = 0
                            clean_flight["result"]["totals"]["balance"] = 0
                            
                        # Додаємо запис про Emergency Declared, нічого не замінюючи
                        if "violations" in clean_flight["result"]:
                            clean_flight["result"]["violations"].append({
                                "title": "Emergency Declared",
                                "penalty": {
                                    "points": 0,
                                    "cash": 0
                                },
                                "entry": {
                                    "type": "emergency",
                                    "payload": {}
                                },
                                "wiki": "https://wiki.newsky.app/en/flight/rating-system#emergency"
                            })
                
                # 3. Визначаємо тиждень за часом посадки
                arrival_time = f.get("arrTimeAct") or f.get("close")
                target_week = get_iso_week(arrival_time)
                
                # 4. Витягуємо дані пілота
                pilot_data = f.get("pilot", {})
                pilot_id = pilot_data.get("_id", "unknown")
                pilot_name = pilot_data.get("fullname", "Unknown Pilot")
                pilot_avatar = pilot_data.get("avatar", "default")
                
                await msg.edit(content=f"⏳ Дані отримано. Записую рейс `{cs}` у файл `{target_week}.json` на GitHub...")
                
                # 5. Відправляємо на GitHub
                await save_flight_to_github(clean_flight, pilot_id, pilot_name, pilot_avatar, target_week)
                
                # 6. Звіт про успіх
                if force_emer:
                    await msg.edit(content=f"🚨 **Успіх!** Рейс `{cs}` (ID: `{fid}`) записано на GitHub у тиждень `{target_week}` **із повною імітацією EMERGENCY (штрафи анульовано, лог додано)!**")
                else:
                    await msg.edit(content=f"✅ **Успіх!** Рейс `{cs}` (ID: `{fid}`) успішно додано на GitHub у тиждень `{target_week}`.")
                
        except Exception as e:
            await msg.edit(content=f"❌ **Внутрішня помилка:** {e}")
        return
# -------------------------------------------------------------

    # --- ➕ КОМАНДА: !addflight <ID> (ДОДАТИ ПРОПУЩЕНИЙ РЕЙС) ---
    if message.content.startswith("!addflight"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!addflight <Flight_ID>`")
        
        fid = parts[1]
        msg = await message.channel.send(f"⏳ **Searching for flight `{fid}` in Newsky DB...**")
        
        try:
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{fid}")
                
                if not det or "flight" not in det:
                    return await msg.edit(content=f"❌ **Error:** Flight `{fid}` not found in API.")
                
                f = det["flight"]
                
                sched_time = f.get("depTimeSched") or f.get("creationDate")
                week_tag = get_iso_week(sched_time)
                
                update_weekly_stats(f, week_tag)
                
                cs = f.get("flightNumber") or f.get("callsign") or "Unknown"
                await msg.edit(content=f"✅ **Flight `{cs}` successfully found and added to week `{week_tag}`!**")
                
        except Exception as e:
            await msg.edit(content=f"❌ **Internal error occurred:** {e}")
        return
    # -------------------------------------------------------------

	# --- 🗑️ КОМАНДА: !delghflight <ID> (ВИДАЛИТИ РЕЙС З GITHUB) ---
    if message.content.startswith("!delghflight"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!delghflight <Flight_ID>`")
            
        target_id = parts[1]
        status_msg = await message.channel.send(f"⏳ **Шукаю рейс `{target_id}` у базі GitHub...**")
        
        if not GITHUB_TOKEN:
            return await status_msg.edit(content="❌ **Помилка:** Немає токену GITHUB_TOKEN.")

        gh_headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Cache-Control": "no-cache"
        }

        deleted = False
        target_file = ""

        try:
            async with GITHUB_DB_LOCK:
                async with aiohttp.ClientSession() as session:
                    # 1. Отримуємо актуальний список файлів (з антикешем)
                    dir_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/FLIGHTS?t={int(time.time())}"
                    async with session.get(dir_url, headers=gh_headers) as dir_resp:
                        if dir_resp.status != 200:
                            return await status_msg.edit(content="❌ **Помилка:** Не вдалося отримати список файлів з GitHub.")
                        dir_data = await dir_resp.json()

                    # 2. Перебираємо всі файли у папці
                    for item in dir_data:
                        file_name = item.get("name", "")
                        if not file_name.endswith(".json"): continue
                        
                        file_path = item["path"]
                        file_sha = item["sha"]
                        
                        # 3. Читаємо файл через Blob API (найсвіжіші дані)
                        blob_url = f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs/{file_sha}"
                        async with session.get(blob_url, headers=gh_headers) as blob_resp:
                            if blob_resp.status != 200: continue
                            try:
                                blob_data = await blob_resp.json()
                                text_content = base64.b64decode(blob_data['content']).decode('utf-8')
                                file_content = json.loads(text_content)
                            except:
                                continue
                        
                        changed = False
                        
                        # 4. Шукаємо рейс усередині файлу
                        for p in file_content:
                            original_len = len(p["flights"])
                            # Фільтруємо рейси, залишаючи всі, крім того, що треба видалити
                            p["flights"] = [f for f in p["flights"] if str(f.get("_id")) != target_id and str(f.get("id")) != target_id]
                            
                            # Якщо довжина масиву змінилася, значить рейс був тут!
                            if len(p["flights"]) < original_len:
                                changed = True
                        
                        # 5. Якщо рейс знайшли і видалили — записуємо файл назад
                        if changed:
                            # Підчищаємо пілотів, у яких 0 рейсів після видалення
                            file_content = [p for p in file_content if len(p["flights"]) > 0]
                            
                            new_content_str = json.dumps(file_content, ensure_ascii=False, indent=4)
                            new_content_b64 = base64.b64encode(new_content_str.encode('utf-8')).decode('utf-8')
                            
                            push_payload = {
                                "message": f"🤖 Auto delete flight {target_id}",
                                "content": new_content_b64,
                                "sha": file_sha
                            }
                            
                            put_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
                            async with session.put(put_url, headers=gh_headers, json=push_payload) as put_resp:
                                if put_resp.status in [200, 201]:
                                    deleted = True
                                    target_file = file_name
                            
                            # Оскільки рейс унікальний, якщо ми його знайшли і видалили — далі шукати немає сенсу
                            break 
                            
        except Exception as e:
            return await status_msg.edit(content=f"❌ **Помилка під час видалення:** {e}")

        # 6. Виводимо результат
        if deleted:
            await status_msg.edit(content=f"✅ **Успіх!** Рейс `{target_id}` назавжди видалено з файлу `{target_file}` на GitHub.")
        else:
            await status_msg.edit(content=f"⚠️ **Рейс не знайдено.** Рейсу `{target_id}` немає в жодному JSON-файлі на GitHub.")
        return
    # -------------------------------------------------------------

	# --- ➖ КОМАНДА: !delflight <ID> (ВІДНЯТИ РЕЙС ЗІ СТАТИСТИКИ) ---
    if message.content.startswith("!delflight"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!delflight <Flight_ID>`")
        
        fid = parts[1]
        msg = await message.channel.send(f"⏳ **Removing flight `{fid}` from stats...**")
        
        try:
            async with aiohttp.ClientSession() as session:
                det = await fetch_api(session, f"/flight/{fid}")
                
                if not det or "flight" not in det:
                    return await msg.edit(content=f"❌ **Error:** Flight `{fid}` not found in API.")
                
                f = det["flight"]
                sched_time = f.get("depTimeSched") or f.get("creationDate")
                week_tag = get_iso_week(sched_time)
                
                stats = load_weekly_stats()
                if week_tag not in stats:
                    return await msg.edit(content=f"⚠️ Week `{week_tag}` not found in stats.")
                
                s = stats[week_tag]
                
                t = f.get("result", {}).get("totals", {})
                balance = int(t.get("balance", 0))
                
                raw_pax = t.get("payload", {}).get("pax", 0)
                if raw_pax == 0 and f.get("type") != "cargo":
                    raw_pax = int(f.get("payload", {}).get("pax", 0))
                    
                cargo_kg = int(f.get("payload", {}).get("weights", {}).get("cargo", 0))
                rating = float(f.get("rating", 0.0))
                
                pilot = f.get("pilot", {}).get("fullname", "Unknown Pilot")
                
                check_g, check_fpm = 0.0, 0
                if "result" in f and "violations" in f["result"]:
                    for v in f["result"]["violations"]:
                        entry = v.get("entry", {}).get("payload", {}).get("touchDown", {})
                        if entry:
                            check_g = float(entry.get("gForce", 0))
                            check_fpm = int(entry.get("rate", 0))
                            break 
                if check_g == 0 and "landing" in f:
                    check_g = float(f.get("landing", {}).get("gForce", 0))
                    check_fpm = int(f.get("landing", {}).get("rate", 0) or f.get("landing", {}).get("touchDownRate", 0))
                    
                fpm_val = -abs(check_fpm) if check_fpm != 0 else 0
                
                if s["flights"] > 0:
                    s["flights"] -= 1
                    s["earnings"] -= balance
                    s["pax"] -= raw_pax
                    s["cargo"] -= cargo_kg
                    s["rating_sum"] -= rating
                    s["fpm_sum"] -= fpm_val
                    s["g_sum"] -= check_g
                
                if pilot in s.get("pilots", {}):
                    s["pilots"][pilot] -= 1
                    if s["pilots"][pilot] <= 0: del s["pilots"][pilot]
                
                save_weekly_stats(stats)
                
                cs = f.get("flightNumber") or f.get("callsign") or "Unknown"
                await msg.edit(content=f"✅ **Flight `{cs}` successfully REMOVED from week `{week_tag}`!**\n*(Note: Totals and averages are fixed. Weekly records like 'Hardest Landing' are not changed).*")
                
        except Exception as e:
            await msg.edit(content=f"❌ **Internal error occurred:** {e}")
        return
    # -------------------------------------------------------------

        # --- 📊 КОМАНДА: !stats (СКАЧАТИ ФАЙЛ СТАТИСТИКИ) ---
    if message.content == "!stats":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        try:
            if not WEEKLY_STATS_FILE.exists() or os.path.getsize(WEEKLY_STATS_FILE) == 0:
                return await message.channel.send("⚠️ **Stats file (weekly_stats.json) is empty or does not exist yet.**")
                
            with open(WEEKLY_STATS_FILE, "rb") as fp:
                await message.channel.send(
                    content="📊 **Weekly Stats File:**", 
                    file=discord.File(fp, filename="weekly_stats.json")
                )
        except Exception as e:
            await message.channel.send(f"❌ **Error sending file:** {e}")
        return
    # --------------------------------------------------------

    # --- 📜 КОМАНДА: !audit [all/кількість] (СКАЧАТИ ЖУРНАЛ АУДИТУ) ---
    if message.content.startswith("!audit"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        
        fetch_limit = 50
        is_all = False
        
        if len(parts) > 1:
            if parts[1].lower() == "all":
                fetch_limit = None
                is_all = True
            elif parts[1].isdigit():
                fetch_limit = int(parts[1])

        main_channel = client.get_channel(CHANNEL_ID)
        if not main_channel:
            return await message.channel.send("❌ **Error:** Cannot find the main server. Check CHANNEL_ID.")
        
        guild = main_channel.guild
        
        if is_all:
            await message.channel.send(f"⏳ **Gathering ALL available logs (up to 90 days) from '{guild.name}'... This may take a minute.**")
        else:
            await message.channel.send(f"⏳ **Gathering the last {fetch_limit} audit logs from '{guild.name}'...**")
        
        try:
            audit_text = f"=== Audit Log: {guild.name} ===\n"
            audit_text += f"Limit: {'All available (up to 90 days)' if is_all else fetch_limit}\n" + "="*50 + "\n\n"
            
            count = 0
            async for entry in guild.audit_logs(limit=fetch_limit):
                time_str = entry.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                user = entry.user
                action = entry.action.name
                target = entry.target
                reason = entry.reason or "Not specified"
                
                audit_text += f"[{time_str}] {user} -> ACTION: {action}\n"
                audit_text += f"   Target: {target}\n"
                audit_text += f"   Reason: {reason}\n"
                audit_text += "-"*50 + "\n"
                count += 1
                
            audit_text += f"\nTotal logs gathered: {count}"
                
            file_bin = io.BytesIO(audit_text.encode('utf-8'))
            
            await message.channel.send(
                content=f"✅ **Done! Found {count} logs.** Here is your report:", 
                file=discord.File(file_bin, filename=f"audit_log_{'all' if is_all else fetch_limit}.txt")
            )
            
        except discord.Forbidden:
            await message.channel.send("❌ **Error:** I don't have the 'View Audit Log' (Перегляд журналу аудиту) permission.")
        except Exception as e:
            await message.channel.send(f"❌ **Error:** {e}")
        return
    # -------------------------------------------------------------

    # --- 🗑️ КОМАНДА: !del (РОЗУМНЕ ВИДАЛЕННЯ ТІЛЬКИ ЗА ID) ---
    if message.content.startswith("!del "):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) != 2:
            return await message.channel.send("⚠️ **Format:** `!del <Message_ID>`")
            
        try:
            msg_id = int(parts[1])
        except ValueError:
            return await message.channel.send("❌ **Error:** Message ID must be a number.")
            
        status_msg = await message.channel.send("⏳ **Searching for message across all channels...**")
        
        found_msg = None
        
        for guild in client.guilds:
            for channel in guild.text_channels:
                try:
                    found_msg = await channel.fetch_message(msg_id)
                    break
                except:
                    continue
            if found_msg:
                break
                
        if found_msg:
            try:
                await found_msg.delete()
                await status_msg.edit(content=f"✅ **Success!** Message silently deleted in channel `#{found_msg.channel.name}` 🥷")
            except discord.Forbidden:
                await status_msg.edit(content="❌ **Error:** Found the message, but lack permission to delete it (check Manage Messages).")
        else:
            await status_msg.edit(content="❌ **Error:** Message with this ID not found on the server (or already deleted).")
            
        return
    # -------------------------------------------------------------------------

    # --- 🧹 КОМАНДА: !clearwow <ID> (ОЧИСТИТИ ВСІ РЕАКЦІЇ) ---
    if message.content.startswith("!clearwow"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!clearwow <Message_ID>`")
        
        target_id = parts[1]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            try:
                await found_message.clear_reactions()
                channel_mention = found_message.channel.mention if hasattr(found_message.channel, 'mention') else "Direct Messages"
                await message.channel.send(f"✅ **Cleared all reactions from message in {channel_mention}**")
            except discord.Forbidden:
                await message.channel.send("❌ **Error:** I don't have 'Manage Messages' permission to clear reactions. Please check role settings.")
            except Exception as e:
                await message.channel.send(f"❌ **Error clearing reactions:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

	# --- 🔇 КОМАНДА: !hideuser <User_ID> <Channel_ID> (ПРИХОВАТИ ЮЗЕРА) ---
    if message.content.startswith("!hideuser"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 3:
            return await message.channel.send("⚠️ **Формат:** `!hideuser <ID_користувача> <ID_каналу>`")
            
        target_user = int(parts[1])
        target_channel = str(parts[2]) # Зберігаємо як текст для JSON
        
        if target_channel not in HIDDEN_USERS:
            HIDDEN_USERS[target_channel] = []
            
        if target_user not in HIDDEN_USERS[target_channel]:
            HIDDEN_USERS[target_channel].append(target_user)
            save_hidden_users(HIDDEN_USERS) # 🔥 ЗБЕРІГАЄМО У ФАЙЛ
            await message.channel.send(f"🥷 **Готово!** Тепер повідомлення від користувача `{target_user}` у каналі `{target_channel}` будуть назавжди приховані.")
        else:
            await message.channel.send("⚠️ Цей користувач вже прихований у цьому каналі.")
        return

    # --- 🔊 КОМАНДА: !unhideuser <User_ID> <Channel_ID> (ПОВЕРНУТИ ЮЗЕРА) ---
    if message.content.startswith("!unhideuser"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 3:
            return await message.channel.send("⚠️ **Формат:** `!unhideuser <ID_користувача> <ID_каналу>`")
            
        target_user = int(parts[1])
        target_channel = str(parts[2])
        
        if target_channel in HIDDEN_USERS and target_user in HIDDEN_USERS[target_channel]:
            HIDDEN_USERS[target_channel].remove(target_user)
            # Якщо список порожній, підчищаємо канал з бази
            if not HIDDEN_USERS[target_channel]:
                del HIDDEN_USERS[target_channel]
                
            save_hidden_users(HIDDEN_USERS) # 🔥 ЗБЕРІГАЄМО У ФАЙЛ
            await message.channel.send(f"✅ **Знято!** Користувач `{target_user}` знову може писати в канал `{target_channel}`.")
        else:
            await message.channel.send("⚠️ Цього користувача не було в списку прихованих для цього каналу.")
        return

    # --- 🛡️ КОМАНДА: !banwow <ID> (ЗАБОРОНИТИ СМАЙЛИ) ---
    if message.content.startswith("!banwow"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.channel.send("⚠️ Usage: `!banwow <Message_ID>`")
        
        msg_id = int(parts[1])
        BANNED_WOW_MESSAGES.add(msg_id)
        await message.channel.send(f"🛡️ **Message {msg_id} is now protected!**\nAny new reactions will be instantly deleted.")
        return

    # --- 🟢 КОМАНДА: !unbanwow <ID> (ДОЗВОЛИТИ СМАЙЛИ) ---
    if message.content.startswith("!unbanwow"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.channel.send("⚠️ Usage: `!unbanwow <Message_ID>`")
        
        msg_id = int(parts[1])
        if msg_id in BANNED_WOW_MESSAGES:
            BANNED_WOW_MESSAGES.remove(msg_id)
            await message.channel.send(f"✅ **Protection removed.** You can now react to message {msg_id} again.")
        else:
            await message.channel.send("⚠️ This message is not currently blacklisted.")
        return
    
	# --- 👹 КОМАНДА: !wow <ID> <EMOJI> (СТАВИТИ РЕАКЦІЮ) ---
    if message.content.startswith("!wow"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 3:
            return await message.channel.send("⚠️ Usage: `!wow <Message_ID> <Emoji>`")
        
        target_id = parts[1]
        emoji_input = parts[2]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            reaction_emoji = emoji_input
            
            # 🔥 Перевіряємо, чи це ПРОСТО НАЗВА (тільки букви, цифри та нижнє підкреслення)
            if re.match(r"^[a-zA-Z0-9_]+$", emoji_input):
                # Якщо команду написали на сервері — шукаємо на цьому ж сервері
                if message.guild:
                    found_emoji = discord.utils.get(message.guild.emojis, name=emoji_input)
                    if found_emoji:
                        reaction_emoji = found_emoji
                    else:
                        return await message.channel.send(f"❌ **Емодзі `{emoji_input}` не знайдено на цьому сервері!**")
                else:
                    # Якщо команду написали в ПП (тут немає бази кастомних емодзі)
                    return await message.channel.send("❌ **Помилка:** В особистих повідомленнях не можна шукати кастомні емодзі за назвою. Відправ стандартний смайлик або його повний код `<:name:ID>`.")

            try:
                await found_message.add_reaction(reaction_emoji)
                channel_mention = found_message.channel.mention if hasattr(found_message.channel, 'mention') else "Direct Messages"
                await message.channel.send(f"✅ **Reacted {reaction_emoji} to message in {channel_mention}**")
            except discord.HTTPException as e:
                if e.status == 400:
                    await message.channel.send(f"❌ **Помилка:** Неправильний формат емодзі або бот не має до нього доступу.")
                else:
                    await message.channel.send(f"❌ **Error adding reaction:** {e}")
            except Exception as e:
                await message.channel.send(f"❌ **Error adding reaction:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

    # --- 💽 КОМАНДА: !disk (РЕАЛЬНА ПАМ'ЯТЬ БОТА) ---
    if message.content == "!disk":
        def get_size(path="."):
            total = 0
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
            return total / (1024 * 1024)

        app_size_mb = get_size("/app") 
        volume_size_mb = get_size("/app/data") if os.path.exists("/app/data") else 0
        
        temp_used_mb = app_size_mb - volume_size_mb
        
        LIMIT_MB = 1024.0
        free_mb = LIMIT_MB - temp_used_mb
        
        text = (
            f"💽 **Bot's Real Memory Stats:**\n"
            f"**Available Limit:** {LIMIT_MB} MB (1 GB)\n"
            f"**Currently Used:** {temp_used_mb:.2f} MB\n"
            f"**FREE TO WRITE:** {free_mb:.2f} MB\n"
            f"*(Reference: Volume persistent memory takes {volume_size_mb:.2f} MB)*"
        )
        return await message.channel.send(text)
    # -------------------------------------------------------------

	# --- 📋 КОМАНДА: !chlist (СПИСОК ВСІХ КАНАЛІВ) ---
    if message.content == "!chlist":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        main_channel = client.get_channel(CHANNEL_ID)
        if not main_channel:
            return await message.channel.send("❌ **Помилка:** Не вдалося знайти головний сервер (перевір CHANNEL_ID).")
        
        guild = main_channel.guild
        await message.channel.send(f"⏳ **Збираю список каналів сервера `{guild.name}`...**")
        
        lines = [f"=== Список каналів сервера: {guild.name} ===\n"]
        
        # Спочатку категорії та канали всередині них
        for category in guild.categories:
            lines.append(f"📂 [Категорія] {category.name} (ID: {category.id})")
            for ch in category.channels:
                lines.append(f"   ├─ [{ch.type.name.upper()}] {ch.name} (ID: {ch.id})")
            lines.append("")
            
        # Канали без категорій (якщо такі є)
        uncategorized = [ch for ch in guild.channels if ch.category is None]
        if uncategorized:
            lines.append("📁 [Без категорії]")
            for ch in uncategorized:
                lines.append(f"   ├─ [{ch.type.name.upper()}] {ch.name} (ID: {ch.id})")
                
        file_bin = io.BytesIO("\n".join(lines).encode('utf-8'))
        await message.channel.send(
            content=f"✅ **Готово! Знайдено {len(guild.channels)} каналів.**",
            file=discord.File(file_bin, filename="channel_list.txt")
        )
        return

    # --- 📥 КОМАНДА: !chview <ID> (ВИКАЧАТИ ІСТОРІЮ КАНАЛУ) ---
    if message.content.startswith("!chview"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.channel.send("⚠️ **Формат:** `!chview <Channel_ID>`")
            
        target_id = int(parts[1])
        main_channel = client.get_channel(CHANNEL_ID)
        guild = main_channel.guild if main_channel else None
        
        if not guild:
            return await message.channel.send("❌ **Помилка:** Не вдалося знайти головний сервер.")
            
        # Знаходимо канал
        target_channel = guild.get_channel(target_id)
        if not target_channel:
            try:
                target_channel = await guild.fetch_channel(target_id)
            except:
                return await message.channel.send("❌ **Помилка:** Канал з таким ID не знайдено.")
                
        # Перевіряємо, чи можна в ньому взагалі читати повідомлення
        if not hasattr(target_channel, 'history'):
            return await message.channel.send("❌ **Помилка:** Цей тип каналу не підтримує історію повідомлень (наприклад, це голосовий канал без чату).")
            
        status_msg = await message.channel.send(f"⏳ **Викачую історію з каналу `{target_channel.name}`...** Це може зайняти деякий час.")
        
        try:
            lines = [f"=== Історія повідомлень каналу: {target_channel.name} (ID: {target_id}) ===\n"]
            count = 0
            
            # oldest_first=True викачує від найстаріших повідомлень до найновіших
            async for msg in target_channel.history(limit=None, oldest_first=True):
                time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                author = msg.author.name
                
                # Замінюємо переноси рядків на пробіли, щоб одне повідомлення займало один рядок у файлі
                content = msg.content.replace('\n', ' ') 
                
                lines.append(f"[{time_str}] {author}: {content}")
                
                # Якщо є вкладення (фото, файли), зберігаємо посилання на них
                for att in msg.attachments:
                    lines.append(f"   📎 [Вкладення]: {att.url}")
                    
                count += 1
                if count % 1000 == 0:
                    await status_msg.edit(content=f"⏳ **Викачую... Вже оброблено {count} повідомлень.**")
                    
            if count == 0:
                return await status_msg.edit(content="✅ **Канал абсолютно порожній.**")
                
            file_bin = io.BytesIO("\n".join(lines).encode('utf-8'))
            
            await status_msg.delete()
            await message.channel.send(
                content=f"✅ **Готово! Викачано {count} повідомлень з `#{target_channel.name}`.**",
                file=discord.File(file_bin, filename=f"export_{target_channel.name}.txt")
            )
            
        except discord.Forbidden:
            await status_msg.edit(content="❌ **Помилка:** У бота немає прав на читання історії цього каналу.")
        except Exception as e:
            await status_msg.edit(content=f"❌ **Сталася помилка:** {e}")
            
        return
    # -------------------------------------------------------------

	# --- 🔍 КОМАНДА: !idemoji <назва> (ДІЗНАТИСЯ КОД ЕМОДЗІ) ---
    if message.content.startswith("!idemoji"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!idemoji <name>` (without colons)")
        
        if not message.guild:
            return await message.channel.send("⚠️ Please use this command in a server channel, not in DM.")

        emoji_name = parts[1].replace(":", "") # Прибираємо двокрапки, якщо випадково написав
        
        # Шукаємо емодзі на сервері
        found_emoji = discord.utils.get(message.guild.emojis, name=emoji_name)
        
        if found_emoji:
            # Формуємо правильний код (з 'a', якщо анімований)
            emoji_code = f"<{'a' if found_emoji.animated else ''}:{found_emoji.name}:{found_emoji.id}>"
            await message.channel.send(f"✅ **Found it!**\nCopy this code:\n`{emoji_code}`\n\nPreview: {emoji_code}")
        else:
            await message.channel.send(f"❌ **Error:** Emoji named `{emoji_name}` not found on this server.")
        return
    # -------------------------------------------------------------

	# --- 🖼️ КОМАНДА: !avatar <User_ID> (СКАЧАТИ АВАТАРКУ КОРИСТУВАЧА) ---
    if message.content.startswith("!avatar"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ **Формат:** `!avatar <User_ID>`")
            
        target_id_str = parts[1]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ **Помилка:** ID користувача має бути числом.")
             
        target_user_id = int(target_id_str)
        
        status_msg = await message.channel.send("⏳ **Шукаю користувача та завантажую аватарку...**")
        
        try:
            # Шукаємо користувача глобально через API Discord
            target_user = await client.fetch_user(target_user_id)
            
            # Беремо аватарку (display_avatar автоматично віддає кастомну або дефолтну)
            avatar_asset = target_user.display_avatar
            
            # Скачуємо картинку в оперативну пам'ять (як байти)
            avatar_bytes = await avatar_asset.read()
            
            # Перетворюємо байти на файл для відправки в Discord
            image_file = discord.File(io.BytesIO(avatar_bytes), filename=f"avatar_{target_user.name}.png")
            
            # Створюємо гарний Embed, щоб прикріпити туди цю фотку
            embed = discord.Embed(
                title=f"🖼️ Аватарка: {target_user.display_name}", 
                description=f"**Нікнейм:** `{target_user.name}`\n**ID:** `{target_user.id}`",
                color=0x3498db
            )
            # Прив'язуємо файл до Embed
            embed.set_image(url=f"attachment://avatar_{target_user.name}.png")
            
            # Відправляємо повідомлення з файлом і видаляємо "годинник"
            await message.channel.send(embed=embed, file=image_file)
            await status_msg.delete()
            
        except discord.NotFound:
            await status_msg.edit(content="❌ **Помилка:** Користувача з таким ID не знайдено в Discord.")
        except Exception as e:
            await status_msg.edit(content=f"❌ **Помилка під час завантаження:** {e}")
        return
    # -------------------------------------------------------------

    # --- 📂 КОМАНДА: !files (ВМІСТ ПОСТІЙНОЇ ПАМ'ЯТІ / VOLUME) ---
    if message.content == "!files":
        folder_path = "/app/data"
        
        if os.path.exists(folder_path):
            files = os.listdir(folder_path)
            
            if len(files) > 0:
                file_list = "\n".join([f"📄 {file}" for file in files])
                await message.channel.send(f"📂 **Contents of persistent folder `{folder_path}`:**\n```text\n{file_list}\n```")
            else:
                await message.channel.send(f"📂 Folder `{folder_path}` is currently completely empty.")
        else:
            await message.channel.send(f"❌ Error: Folder `{folder_path}` does not exist! Volume is not attached or the path is incorrect.")
        return

# --- 🗑️ КОМАНДА: !unwow <ID> <EMOJI> (ПРИБРАТИ РЕАКЦІЮ) ---
    if message.content.startswith("!unwow"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 3:
            return await message.channel.send("⚠️ Usage: `!unwow <Message_ID> <Emoji>`")
        
        target_id = parts[1]
        emoji_input = parts[2]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            # 🔥 ДОДАТКОВО: Шукаємо емодзі, якщо передана тільки назва
            reaction_emoji = emoji_input
            if ":" not in emoji_input and not emoji_input.startswith("<"):
                found_emoji = discord.utils.get(message.guild.emojis, name=emoji_input)
                if found_emoji:
                    reaction_emoji = found_emoji
                else:
                    return await message.channel.send(f"❌ **Емодзі `{emoji_input}` не знайдено на цьому сервері!**")

            try:
                # Видаляємо реакцію саме від бота (client.user)
                await found_message.remove_reaction(reaction_emoji, client.user)
                await message.channel.send(f"✅ **Removed {reaction_emoji} from message in {found_message.channel.mention}**")
            except Exception as e:
                await message.channel.send(f"❌ **Error removing reaction:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

    # --- 💬 НОВА КОМАНДА: !reply <ID> <text> (ВІДПОВІСТИ НА ПОВІДОМЛЕННЯ) ---
    if message.content.startswith("!reply"):
        if not is_admin: 
            return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 3:
            return await message.channel.send("⚠️ Usage: `!reply <Message_ID> <text>`")
        
        target_id = parts[1]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        content = " ".join(parts[2:])
        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            try:
                sent_msg = await found_message.reply(content)
                last_sent_message = sent_msg # 🔥 Зберігаємо для команди !undo
                await message.channel.send(f"✅ **Replied to message in {found_message.channel.mention}:**\n{content}")
            except Exception as e:
                await message.channel.send(f"❌ **Error replying:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

    # --- 🔨 КОМАНДА: !ban <0|1> <User_ID> [reason] (БАН КОРИСТУВАЧА НА СЕРВЕРІ) ---
    if message.content.startswith("!ban ") or message.content == "!ban":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        
        # Тепер нам потрібно мінімум 3 частини: !ban, прапорець (0/1) та ID
        if len(parts) < 3:
            return await message.channel.send("⚠️ Usage: `!ban <0 або 1> <User_ID> [reason]`\n*(0 = зберегти повідомлення, 1 = видалити за останні 24 год)*")
        
        delete_flag = parts[1]
        if delete_flag not in ["0", "1"]:
            return await message.channel.send("⚠️ Помилка: Першим параметром після команди має бути `0` або `1`.")
            
        target_id_str = parts[2]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ User ID must be a number.")
        
        target_user_id = int(target_id_str)

        # Причина тепер починається з 4-го слова (індекс 3)
        ban_reason = " ".join(parts[3:]) if len(parts) > 3 else None
        
        # 86400 секунд = 24 години
        del_seconds = 86400 if delete_flag == "1" else 0

        main_channel = client.get_channel(CHANNEL_ID)
        if not main_channel:
            return await message.channel.send("❌ **Error:** Cannot find the main server. Check CHANNEL_ID.")
        
        guild = main_channel.guild
        
        try:
            user_to_ban = discord.Object(id=target_user_id)
            # Якщо ban_reason = None, Discord просто не запише причину в журнал аудиту
            await guild.ban(user_to_ban, reason=ban_reason, delete_message_seconds=del_seconds)
            
            reason_text = f" for: {ban_reason}" if ban_reason else ""
            msg_status = "(Повідомлення збережено)" if del_seconds == 0 else "(Повідомлення за останні 24 год видалено)"
            
            await message.channel.send(f"✅ **User {target_user_id} has been banned{reason_text}.**\n{msg_status}")
        except discord.Forbidden:
            await message.channel.send("❌ **Error:** I don't have the 'Ban Members' permission, or my role is lower than the target's role.")
        except Exception as e:
            await message.channel.send(f"❌ **Error banning user:** {e}")
        return
    # -------------------------------------------------------------

    # --- 🕊️ КОМАНДА: !unban <User_ID> (РОЗБАН КОРИСТУВАЧА) ---
    if message.content.startswith("!unban"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!unban <User_ID>`")
        
        target_id_str = parts[1]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ User ID must be a number.")
        
        target_user_id = int(target_id_str)
        main_channel = client.get_channel(CHANNEL_ID)
        if not main_channel:
            return await message.channel.send("❌ **Error:** Cannot find the main server.")
        
        guild = main_channel.guild
        
        try:
            user_to_unban = discord.Object(id=target_user_id)
            await guild.unban(user_to_unban, reason="Unbanned via bot.")
            
            await message.channel.send(f"✅ **User {target_user_id} has been unbanned in '{guild.name}'.**")
        except discord.NotFound:
            await message.channel.send(f"⚠️ **User {target_user_id} is not banned on this server.**")
        except discord.Forbidden:
            await message.channel.send("❌ **Error:** I don't have the 'Ban Members' permission.")
        except Exception as e:
            await message.channel.send(f"❌ **Error unbanning user:** {e}")
        return
    # -------------------------------------------------------------
    
    # --- 📋 КОМАНДА: !banlist (ПОКАЗАТИ СПИСОК ЗАБАНЕНИХ) ---
    if message.content == "!banlist":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        main_channel = client.get_channel(CHANNEL_ID)
        if not main_channel:
            return await message.channel.send("❌ **Error:** Cannot find the main server.")
        
        guild = main_channel.guild
        
        try:
            await message.channel.send(f"⏳ **Fetching ban list for server '{guild.name}'...**")
            
            ban_list_text = f"=== Ban List for Server: {guild.name} ===\n\n"
            count = 0
            
            async for ban_entry in guild.bans():
                user = ban_entry.user
                reason = ban_entry.reason or "Not specified"
                ban_list_text += f"User: {user} (ID: {user.id})\nReason: {reason}\n{'-'*40}\n"
                count += 1
                
            if count == 0:
                return await message.channel.send("✅ **The ban list is empty!** No banned users on this server.")
                
            ban_list_text += f"\nTotal banned users: {count}."
            
            file_bin = io.BytesIO(ban_list_text.encode('utf-8'))
            await message.channel.send(
                content=f"📜 **Done! Found {count} banned users.** Here is the file:", 
                file=discord.File(file_bin, filename="banlist.txt")
            )
            
        except discord.Forbidden:
            await message.channel.send("❌ **Error:** I don't have the 'Ban Members' permission to view the ban list.")
        except Exception as e:
            await message.channel.send(f"❌ **Error fetching ban list:** {e}")
        return
    # -------------------------------------------------------------

	# --- 📝 КОМАНДА: !rename <ID> <текст> (ДОДАТИ ТЕКСТ В КАРТКУ ЧЕРЕЗ ПП) ---
    if message.content.startswith("!rename"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split(maxsplit=2)
        if len(parts) < 3:
            return await message.channel.send("⚠️ Usage: `!rename <Message_ID> <text>`")
            
        target_id_str = parts[1]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")
             
        new_text = parts[2]
        
        found_message = await find_discord_message(int(target_id_str), message)
        
        if found_message:
            if found_message.author == client.user and found_message.embeds:
                try:
                    embed = found_message.embeds[0]
                    
                    old_desc = embed.description or ""
                    embed.description = f"{old_desc}\n\n{new_text}"
                    
                    await found_message.edit(embed=embed)
                    
                    channel_mention = found_message.channel.mention if hasattr(found_message.channel, 'mention') else "Direct Messages"
                    await message.channel.send(f"✅ **Success!** Text added to message in {channel_mention}.")
                    
                    if not isinstance(message.channel, discord.DMChannel):
                        try:
                            await message.delete()
                        except discord.Forbidden:
                            pass
                            
                except Exception as e:
                    await message.channel.send(f"❌ **Error updating message:** {e}")
            else:
                await message.channel.send("❌ **Error:** Message must be sent by the bot and contain an embed.")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

	# --- ✉️ КОМАНДА: !pmsg <User_ID> <текст> (НАПИСАТИ В ПП КОРИСТУВАЧУ) ---
    if message.content.startswith("!pmsg"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split(" ", 2)
        if len(parts) < 2 and not message.attachments:
            return await message.channel.send("⚠️ **Format:** `!pmsg <User_ID> <text>` (можна прикріплювати картинки)")
            
        target_id_str = parts[1]
        if not target_id_str.isdigit():
             return await message.channel.send("⚠️ **Error:** User ID має бути числом.")
             
        target_user_id = int(target_id_str)
        text_to_send = parts[2] if len(parts) > 2 else ""
        
        files_to_send = []
        for attachment in message.attachments:
            files_to_send.append(await attachment.to_file())
            
        if not text_to_send and not files_to_send:
            return await message.channel.send("⚠️ **Error:** Немає тексту або зображення для відправки.")
            
        try:
            # Шукаємо користувача
            target_user = await client.fetch_user(target_user_id)
            if not target_user:
                return await message.channel.send("❌ **Error:** Користувача з таким ID не знайдено.")
                
            # Відправляємо повідомлення
            await target_user.send(content=text_to_send, files=files_to_send)
            await message.add_reaction("✅")
            await message.channel.send(f"✅ **Повідомлення успішно доставлено в ПП користувачу {target_user.display_name}.**")
            
        except discord.Forbidden:
            await message.channel.send(f"❌ **Error:** Неможливо надіслати повідомлення {target_user_id}. Можливо, у нього закриті ПП для не-друзів, або він заблокував бота.")
        except Exception as e:
            await message.channel.send(f"❌ **Error:** {e}")
        return
    # -------------------------------------------------------------------------

   # --- ✉️ КОМАНДА: !msg <ID_каналу> <текст> (+ МОЖНА ПРИКРІПЛЯТИ КАРТИНКИ) ---
    if message.content.startswith("!msg"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split(" ", 2)
        if len(parts) < 2 and not message.attachments:
            return await message.channel.send("⚠️ **Format:** `!msg <Channel_ID> <text>` (and/or attach an image)")
            
        try:
            channel_id = int(parts[1])
            target_channel = client.get_channel(channel_id) 
            if not target_channel:
                return await message.channel.send("❌ **Error:** Channel with this ID not found.")
                
            text_to_send = parts[2] if len(parts) > 2 else ""
            
            files_to_send = []
            for attachment in message.attachments:
                files_to_send.append(await attachment.to_file())
                
            if not text_to_send and not files_to_send:
                return await message.channel.send("⚠️ **Error:** No text or image to send.")
                
            sent_msg = await target_channel.send(content=text_to_send, files=files_to_send)
            
            last_sent_message = sent_msg
            
            await message.add_reaction("✅") 
            
        except ValueError:
            await message.channel.send("❌ **Error:** Channel ID must be a number.")
        except Exception as e:
            await message.channel.send(f"❌ **Error sending message:** {e}")
        return
    # -------------------------------------------------------------------------
    
# --- 📡 КОМАНДА: !traffic (ПОКАЗАТИ АКТИВНІ РЕЙСИ) ---
    if message.content == "!traffic":
        global LAST_TRAFFIC_TIME
        current_time = time.time()

        if current_time - LAST_TRAFFIC_TIME < 10:
            remaining = int(10 - (current_time - LAST_TRAFFIC_TIME))
            warning_msg = await message.channel.send(f"⏳ **Please wait {remaining} seconds** before requesting traffic again.")
            await asyncio.sleep(3)
            try:
                await warning_msg.delete()
            except:
                pass
            return
            
        LAST_TRAFFIC_TIME = current_time
        
        msg = await message.channel.send("🔄 **Fetching live traffic data...**")
        
        async with aiohttp.ClientSession() as session:
            ongoing = await fetch_api(session, "/flights/ongoing")
            
            # 1. ОБРОБКА СИТУАЦІЇ, КОЛИ НЕМАЄ РЕЙСІВ
            if not ongoing or "results" not in ongoing or len(ongoing["results"]) == 0:
                embed = discord.Embed(title="📡 Live Traffic - Ukraine Classic Air Alliance", description="😴 **No active flights.**", color=0xffff00)
                current_utc_time = datetime.now(timezone.utc).strftime('%H:%M')
                return await msg.edit(content=None, embed=embed)
            
            # 2. ЯКЩО РЕЙСИ Є, ОБРОБЛЯЄМО ЇХ
            desc_lines = []
            for raw_f in ongoing["results"]:
                fid = str(raw_f.get("_id") or raw_f.get("id"))
                
                det = await fetch_api(session, f"/flight/{fid}")
                
                alt_str, gs_str = "---", "---"
                phase_str = "⏳ Unknown"
                
                if det and "flight" in det:
                    f = det["flight"]
                    last_state = f.get("lastState", {})
                    
                    loc = last_state.get("location", {})
                    spd = last_state.get("speed", {})
                    
                    # alt_ft - стандартна висота (MSL) для ешелону, agl_ft - радіовисотомір
                    alt_ft = int(loc.get("alt", 0))
                    alt_str = f"{alt_ft:,}".replace(",", ".") + " ft"
                    
                    gs_kts = int(spd.get("gs", 0))
                    gs_str = f"{gs_kts} kts"
                    
                    agl_ft = int(loc.get("agl", alt_ft))
                    vs_fpm = int(spd.get("vs", 0))
                    
                    dep_time = f.get("depTimeAct")
                    takeoff_time = f.get("takeoffTimeAct")
                    arr_time = f.get("arrTimeAct") or f.get("landing")
                    
                    # --- ЛОГІКА ФАЗ ПОЛЬОТУ ---
                    if not takeoff_time:
                        if dep_time:
                            phase_str = "🚜 Taxiing"
                        else:
                            phase_str = "🧳 Boarding"
                    else:
                        if arr_time or (agl_ft < 200 and gs_kts < 50):
                            if agl_ft > 200:
                                phase_str = "⤴️ Go Around"
                            else:
                                phase_str = "🏁 Arrived"
                        else:
                            # Аналіз історії для Cruise (перевірка MSL висоти 60 сек тому)
                            current_ts = last_state.get("timestamp", time.time() * 1000) / 1000.0
                            found_old = False
                            old_alt = alt_ft
                            
                            for p in reversed(f.get("path", [])):
                                p_time = p.get("createdAt")
                                if p_time:
                                    p_ts = datetime.fromisoformat(p_time.replace("Z", "+00:00")).timestamp()
                                    if (current_ts - p_ts) >= 60:
                                        old_alt = int(p.get("alt", alt_ft))
                                        found_old = True
                                        break
                                        
                            is_cruising = False
                            if found_old and abs(alt_ft - old_alt) <= 250:
                                is_cruising = True
                            elif not found_old and abs(vs_fpm) <= 250:
                                is_cruising = True
                                
                            if is_cruising:
                                phase_str = "✈️ Cruise"
                            elif vs_fpm < -250 or (found_old and (old_alt - alt_ft) > 250):
                                if agl_ft < 10000:
                                    phase_str = "🛬 Approach"
                                else:
                                    phase_str = "↘️ Descent"
                            else:
                                if agl_ft < 10000:
                                    phase_str = "🛫 Departed"
                                else:
                                    phase_str = "↗️ Climb"
                else:
                    f = raw_f

                cs = f.get("flightNumber") or f.get("callsign") or "N/A"
                airline = f.get("airline", {}).get("icao", "")
                full_cs = f"{airline} {cs}".strip() if airline else cs
                
                pilot_data = f.get("pilot", {})
                pilot = pilot_data.get("fullname", "Unknown Pilot") if isinstance(pilot_data, dict) else "Unknown Pilot"
                
                ac_data = f.get("aircraft", {})
                ac = "A/C"
                if isinstance(ac_data, dict):
                    ac = ac_data.get("icao") or ac_data.get("airframe", {}).get("icao") or ac_data.get("airframe", {}).get("name") or "A/C"
                
                dep = f.get("dep", {}).get("icao", "???") if isinstance(f.get("dep"), dict) else "???"
                arr = f.get("arr", {}).get("icao", "???") if isinstance(f.get("arr"), dict) else "???"
                
                desc_lines.append(f"**{full_cs}** • {pilot} • {ac} • {dep} ➔ {arr}\n╰ ⛰️ {alt_str}  |  🛰️ {gs_str}  |  {phase_str}")
            
            embed = discord.Embed(title="📡 Live Traffic - Ukraine Classic Air Alliance", description="\n\n".join(desc_lines), color=0x3498db)
            
            current_utc_time = datetime.now(timezone.utc).strftime('%H:%M')
            total_flights = len(ongoing["results"])
            embed.set_footer(text=f"✈️ Active flights: {total_flights}  |  🔄 Updated: {current_utc_time} UTC  |  Newsky API")
            
            await msg.edit(content=None, embed=embed)
        return
    # -------------------------------------------------------------
	
	# --- 🎤 КОМАНДА: !enter <Channel_ID> (Зайти в голосовий канал) ---
    if message.content.startswith("!enter"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return await message.channel.send("⚠️ Usage: `!enter <Voice_Channel_ID>`")

        vc_id = int(parts[1])
        target_vc = client.get_channel(vc_id)

        if not target_vc or not isinstance(target_vc, discord.VoiceChannel):
            return await message.channel.send("❌ **Error:** Voice channel with this ID not found.")

        guild = target_vc.guild

        if guild.voice_client:
            await guild.voice_client.disconnect()

        try:
            await target_vc.connect()
            await message.channel.send(f"✅ **Bot joined channel:** {target_vc.name}")
        except Exception as e:
            await message.channel.send(f"❌ **Error:** {e}\n*(Make sure `PyNaCl` is in your requirements.txt)*")
        return
    # -------------------------------------------------------------

    # --- 🔇 КОМАНДА: !mute (Замутити/Розмутити бота) ---
    if message.content == "!mute":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")

        main_channel = client.get_channel(CHANNEL_ID)
        guild = message.guild if message.guild else (main_channel.guild if main_channel else None)
        
        if not guild or not guild.voice_client:
            return await message.channel.send("⚠️ **Bot is not currently in any voice channel.**")

        bot_voice_state = guild.me.voice
        current_mute = bot_voice_state.self_mute if bot_voice_state else False
        new_mute_state = not current_mute

        try:
            await guild.change_voice_state(channel=guild.voice_client.channel, self_mute=new_mute_state)
            status_text = "🔇 **Bot microphone MUTED.**" if new_mute_state else "🔊 **Bot microphone UNMUTED.**"
            await message.channel.send(f"✅ {status_text}")
        except Exception as e:
            await message.channel.send(f"❌ **Error:** {e}")
        return
    # -------------------------------------------------------------

    # --- 🚪 КОМАНДА: !leave (Вийти з голосового) ---
    if message.content == "!leave":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")

        main_channel = client.get_channel(CHANNEL_ID)
        guild = message.guild if message.guild else (main_channel.guild if main_channel else None)

        if guild and guild.voice_client:
            await guild.voice_client.disconnect()
            await message.channel.send("✅ **Bot left the voice channel.**")
        else:
            await message.channel.send("⚠️ **Bot is already not in a voice channel.**")
        return
    # -------------------------------------------------------------

	# --- 📌 КОМАНДА: !pin <ID> (ЗАКРІПИТИ ПОВІДОМЛЕННЯ) ---
    if message.content.startswith("!pin"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!pin <Message_ID>`")
            
        target_id = parts[1]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            try:
                await found_message.pin()
                await message.channel.send(f"📌 **Message successfully pinned in {found_message.channel.mention}!**")
            except discord.Forbidden:
                await message.channel.send("❌ **Error:** I don't have 'Manage Messages' permission to pin this.")
            except Exception as e:
                await message.channel.send(f"❌ **Error pinning message:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

    # --- 🧲 КОМАНДА: !unpin <ID> (ВІДКРІПИТИ ПОВІДОМЛЕННЯ) ---
    if message.content.startswith("!unpin"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        parts = message.content.split()
        if len(parts) < 2:
            return await message.channel.send("⚠️ Usage: `!unpin <Message_ID>`")
            
        target_id = parts[1]
        if not target_id.isdigit():
             return await message.channel.send("⚠️ ID must be a number.")

        found_message = await find_discord_message(int(target_id), message)
        
        if found_message:
            try:
                await found_message.unpin()
                await message.channel.send(f"🧲 **Message successfully unpinned in {found_message.channel.mention}!**")
            except discord.Forbidden:
                await message.channel.send("❌ **Error:** I don't have 'Manage Messages' permission to unpin this.")
            except Exception as e:
                await message.channel.send(f"❌ **Error unpinning message:** {e}")
        else:
            await message.channel.send("❌ **Message not found.** (Check ID or bot permissions)")
        return
    # -------------------------------------------------------------

	# --- 📝 КОМАНДА: !editsent (ЗАМІНИТИ ФАЙЛ ПАМ'ЯТІ - ТІЛЬКИ В ПП) ---
    if message.content.startswith("!editsent"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        
        # 🔥 Перевірка на те, чи це ПП (Direct Messages) 🔥
        if not isinstance(message.channel, discord.DMChannel):
            return await message.channel.send("⚠️ **This command ONLY works in Direct Messages (DM)!** Please send me a private message.")
        
        attachment = None
        
        # Перевіряємо, чи файл прикріплено відразу до повідомлення з командою
        if message.attachments:
            attachment = message.attachments[0]
        else:
            await message.channel.send("📥 **Please send the new `sent.json` file here.** You have 60 seconds.")
            
            # Фільтр: чекаємо повідомлення від тебе, в цьому ж каналі (ПП), з файлом
            def check(m):
                return m.author == message.author and m.channel == message.channel and m.attachments
                
            try:
                reply = await client.wait_for('message', check=check, timeout=60.0)
                attachment = reply.attachments[0]
            except asyncio.TimeoutError:
                return await message.channel.send("⏳ **Time's up.** Operation cancelled.")
                
        # Перевірка формату файлу
        if not attachment.filename.endswith('.json'):
            return await message.channel.send("❌ **Error:** The file must be in `.json` format!")
            
        try:
            # Читаємо файл і перевіряємо, чи це валідний JSON
            file_bytes = await attachment.read()
            new_data = json.loads(file_bytes.decode('utf-8'))
            
            if not isinstance(new_data, dict):
                return await message.channel.send("❌ **Error:** Invalid file structure (must be a JSON dictionary).")
                
            # Записуємо нові дані у файл
            STATE_FILE.write_text(json.dumps(new_data, indent=4), encoding="utf-8")
            
            await message.channel.send("✅ **File `sent.json` successfully replaced!**\n🔄 Performing automatic reboot to apply new data...")
            
            # Вбиваємо процес. Railway автоматично перезапустить бота за секунду!
            await asyncio.sleep(1)
            os._exit(1)
            
        except json.JSONDecodeError as e:
            await message.channel.send(f"❌ **Error:** File is corrupted or invalid JSON!\n`{e}`")
        except Exception as e:
            await message.channel.send(f"❌ **An error occurred:** {e}")
            
        return
    # -------------------------------------------------------------

# --- 📚 КОМАНДА: !help (ДИНАМІЧНА ДЛЯ КОРИСТУВАЧІВ, АДМІНІВ ТА ВЛАСНИКА) ---
    if message.content == "!help":
        is_owner = message.author.id in ADMIN_IDS
        
        embed = discord.Embed(title="📚 Bot Commands", color=0x3498db)
        
        # 1. Це бачать УСІ користувачі
        desc = "**🔹 User Commands:**\n"
        desc += "**`!help`** — Show command list\n"
        desc += "**`!traffic`** — Show active flights\n\n"
        
        # 2. Це бачать АДМІНІСТРАТОРИ сервера (і ти також)
        if is_admin:
            desc += "**🔒 Admin Commands:**\n"
            desc += "**`!status`** — System status\n"
            desc += "**`!test [min]`** — Run test scenarios\n"
            desc += "**`!syncweek`** — Sync flights for current week\n"
            desc += "**`!updatedemand`** — Force GitHub demand update\n"
            desc += "**`!delflight <ID>`** — Remove specific flight from stats\n"
            desc += "**`!ban <ID> [reason]`** — Ban user on server\n"
            desc += "**`!unban <ID>`** — Unban user on server\n"
            desc += "**`!audit [all/num]`** — Download server audit log\n"
            desc += "**`!cache`** — Download bot memory (sent.json)\n"
            desc += "**`!spy <ID>`** — Dump raw flight JSON data\n"
            desc += "**`!stats`** — Download weekly_stats.json\n"
            desc += "**`!banlist`** — Download banned users list\n"
            desc += "**`!disk`** — Show server disk/memory usage\n\n"
            
            desc += "**💬 Message & UI Management:**\n"
            desc += "**`!msg [ID] <text/pic>`** — Send text or image message\n"
            desc += "**`!reply <ID> <text>`** — Reply to a message\n"
            desc += "**`!rename <ID> <text>`** — Append text to an existing embed\n"
            desc += "**`!del <msg_ID>`** — Delete any message globally\n"
            desc += "**`!pin <ID>`** — Pin a message\n"
            desc += "**`!unpin <ID>`** — Unpin a message\n"
            desc += "**`!wow <ID> <emoji>`** — Add reaction to a message\n"
            desc += "**`!unwow <ID> <emoji>`** — Remove reaction\n"
            desc += "**`!teststatspin`** — Generate weekly report (PINS message)\n"
            desc += "**`!teststats`** — Generate weekly report (NO pin)\n"
            desc += "**`!teststatstest`** — Generate presentation report (Fake Data)\n"
            desc += "**`!addflight <ID>`** — Force add missed flight to stats\n"
            desc += "**`!clearstats`** — Wipe all weekly stats data\n"
            desc += "**`!idemoji <name>`** — Get code for a custom server emoji\n\n"
            
            desc += "**🎭 Status Management:**\n"
            desc += "**`!next`** — Force next bot status\n"
            desc += "**`!addstatus <type> <text>`** — Save & Add status\n"
            desc += "**`!delstatus [num]`** — Delete status\n\n"
            
        # 3. Це бачиш ТІЛЬКИ ТИ (ID з ADMIN_IDS)
        if is_owner:
            desc += "**👑 Owner Commands (Super Secret):**\n"
            desc += "**`!clearwow <ID>`** — Clear ALL reactions from a msg\n"
            desc += "**`!banwow <ID>`** — Protect msg from new reactions\n"
            desc += "**`!unbanwow <ID>`** — Remove reaction protection\n"
            desc += "**`!enter <ID>`** — Enter voice channel\n"
            desc += "**`!leave`** — Leave voice channel\n"
            desc += "**`!mute`** — Toggle bot microphone mute\n"
            desc += "**`!files`** — List files in local storage (/app/data)\n\n"
            
        embed.description = desc
        await message.channel.send(embed=embed)
        return
    # ----------------------------------------------------------------
    
    if message.content == "!next":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        await change_status()
        await message.channel.send("✅ **Status switched!**")
        return

    if message.content.startswith("!addstatus"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split(maxsplit=2)
        if len(parts) < 3: return await message.channel.send("⚠️ Usage: `!addstatus <watch/play> <text>`")
        sType = parts[1].lower()
        if sType not in ["watch", "play", "listen"]: return await message.channel.send("⚠️ Use: `watch`, `play`, `listen`")
        status_list.append({"type": sType, "name": parts[2]})
        save_statuses()
        global status_cycle
        status_cycle = cycle(status_list)
        await message.channel.send(f"✅ Saved & Added: **{parts[2]}**")
        return

    if message.content.startswith("!delstatus"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) == 1:
            list_str = "\n".join([f"`{i+1}.` {s['type'].upper()}: {s['name']}" for i, s in enumerate(status_list)])
            embed = discord.Embed(title="🗑️ Delete Status", description=f"Type `!delstatus <number>` to delete.\n\n{list_str}", color=0xe74c3c)
            return await message.channel.send(embed=embed)
        try:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(status_list):
                if len(status_list) <= 1: return await message.channel.send("⚠️ Cannot delete the last status!")
                removed = status_list.pop(idx)
                save_statuses()
                status_cycle = cycle(status_list) 
                await message.channel.send(f"🗑️ Deleted & Saved: **{removed['name']}**")
            else:
                await message.channel.send("⚠️ Invalid number.")
        except ValueError:
            await message.channel.send("⚠️ Please enter a number.")
        return

    if message.content == "!status":
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        msg = await message.channel.send("🔄 **Checking Systems...**")
        api_status = "❌ API Error"
        flights_count = 0 
        
        async with aiohttp.ClientSession() as session:
            test = await fetch_api(session, "/flights/ongoing")
            if test is not None: 
                api_status = "✅ Connected to Newsky"
                if "results" in test:
                    flights_count = len(test["results"])
        
        launch_str = START_TIME.strftime("%d-%m-%Y %H:%M:%S UTC")

        embed = discord.Embed(title="🤖 Bot System Status", color=0x2ecc71)
        embed.add_field(name="📡 Newsky API", value=api_status, inline=False)
        embed.add_field(name="✈️ Active Flights", value=f"**{flights_count}** tracking", inline=False)
        embed.add_field(name="🌍 Airports DB", value=f"✅ Loaded ({len(AIRPORTS_DB)} airports)", inline=False)
        embed.add_field(name="📶 Discord Ping", value=f"**{round(client.latency * 1000)}ms**", inline=False)
        embed.add_field(name="🚀 Launched at", value=f"`{launch_str}`", inline=False)
        await msg.edit(content=None, embed=embed)
        return

    if message.content.startswith("!spy"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        try:
            parts = message.content.split()
            if len(parts) < 2: return await message.channel.send("⚠️ Usage: `!spy <ID>`")
            fid = parts[1]
            await message.channel.send(f"🕵️ **Analyzing {fid}...**")
            async with aiohttp.ClientSession() as session:
                data = await fetch_api(session, f"/flight/{fid}")
                if not data: return await message.channel.send("❌ API Error")
                file_bin = io.BytesIO(json.dumps(data, indent=4).encode())
                await message.channel.send(content=f"📂 **Dump {fid}:**", file=discord.File(file_bin, filename=f"flight_{fid}.json"))
        except Exception as e: await message.channel.send(f"Error: {e}")
        return

    if message.content.startswith("!test"):
        if not is_admin: return await message.channel.send("🚫 **Access Denied**")
        parts = message.content.split()
        if len(parts) == 2:
            try:
                custom_delay = int(parts[1])
                await message.channel.send(f"🛠️ **Custom Test (Delay: {custom_delay} min)...**")
                mock_custom = {"_id": "test_custom", "flightNumber": "TEST1", "airline": {"icao": "OSA"}, "dep": {"icao": "UKBB", "name": "Boryspil"}, "arr": {"icao": "LPMA", "name": "Madeira"}, "aircraft": {"airframe": {"name": "B738"}}, "pilot": {"fullname": "Capt. Test"}, "payload": {"pax": 140, "cargo": 35}, "network": "VATSIM", "rating": 9.9, "landing": {"rate": -120, "gForce": 1.05}, "delay": custom_delay, "result": {"totals": {"distance": 350, "time": 55, "balance": 12500, "payload": {"pax": 140, "cargo": 35}}}}
                await send_flight_message(message.channel, "Completed", mock_custom, "test")
                return
            except ValueError: pass

        await message.channel.send("🛠️ **Running Full Test Suite...**")
        mock_dep = {"_id": "test_dep", "flightNumber": "TEST1", "airline": {"icao": "OSA"}, "dep": {"icao": "UKBB", "name": "Boryspil"}, "arr": {"icao": "LPMA", "name": "Madeira"}, "aircraft": {"airframe": {"name": "B738"}}, "pilot": {"fullname": "Capt. Test"}, "payload": {"pax": 145, "cargo": 35}, "delay": 2}
        await send_flight_message(message.channel, "Departed", mock_dep, "test")
        mock_norm = {"_id": "test_norm", "schedule": True, "flightNumber": "TEST1", "airline": {"icao": "OSA"}, "dep": {"icao": "UKBB", "name": "Boryspil"}, "arr": {"icao": "LPMA", "name": "Madeira"}, "aircraft": {"airframe": {"name": "B738"}}, "pilot": {"fullname": "Capt. Test"}, "payload": {"pax": 100, "cargo": 40}, "network": "VATSIM", "rating": 9.9, "landing": {"rate": -150, "gForce": 1.1}, "delay": -10, "result": {"totals": {"distance": 350, "time": 55, "balance": 12500, "payload": {"pax": 100, "cargo": 40}}}}
        await send_flight_message(message.channel, "Completed", mock_norm, "test")
        mock_emerg = mock_norm.copy(); mock_emerg["_id"] = "test_emerg"; mock_emerg["emergency"] = True; mock_emerg["delay"] = 45; mock_emerg["result"] = {"totals": {"distance": 350, "time": 55, "balance": 0, "payload": {"pax": 100, "cargo": 40}}}
        await send_flight_message(message.channel, "Completed", mock_emerg, "test")
        mock_crash = mock_norm.copy(); mock_crash["_id"] = "test_crash"; mock_crash["landing"] = {"rate": -2500, "gForce": 4.5}; mock_crash["rating"] = 0.0; mock_crash["delay"] = 0; mock_crash["result"] = {"totals": {"distance": 350, "time": 55, "balance": -1150000, "payload": {"pax": 100, "cargo": 40}}}
        await send_flight_message(message.channel, "Completed", mock_crash, "test")
        return

async def main_loop():
    await client.wait_until_ready()
    await update_airports_db()
    
    # 🔥 ЗАПУСКАЄМО РАДАР ТУТ (після того, як база 100% завантажена) 🔥
    if not check_charters_task.is_running():
        check_charters_task.start()
        print("🚁 Charter radar started!")
    
    channel = client.get_channel(CHANNEL_ID)
    state = load_state()
    first_run = True
    
    channel = client.get_channel(CHANNEL_ID)
    state = load_state()
    first_run = True

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                ongoing_ids = None
                ongoing = await fetch_api(session, "/flights/ongoing")
                if ongoing and "results" in ongoing:
                    ongoing_ids = set()
                    print(f"📡 Tracking {len(ongoing['results'])} flights...", end='\r')
                    for raw_f in ongoing["results"]:
                        fid = str(raw_f.get("_id") or raw_f.get("id"))
                        ongoing_ids.add(fid)
                        
                        state.setdefault(fid, {})
                        
                        # --- 1. ЛІНИВА ПЕРЕВІРКА ---
                        if state[fid].get("takeoff"):
                            continue
                            
                        det = await fetch_api(session, f"/flight/{fid}")
                        if not det or "flight" not in det: continue
                        f = det["flight"]
                        cs = f.get("flightNumber") or f.get("callsign") or "N/A"
                        if cs == "N/A": continue
                        
                        if f.get("takeoffTimeAct") and not state[fid].get("takeoff"):
                            msg_id = await send_flight_message(channel, "Departed", f, "ongoing")
                            state[fid]["takeoff"] = True
                            
                            # 🔥 НОВЕ: Генерація мітки тижня під час зльоту 🔥
                            sched_time = f.get("depTimeSched") or f.get("creationDate")
                            state[fid]["week"] = get_iso_week(sched_time)
                            
                            if msg_id:
                                state[fid]["msg_id"] = msg_id

                recent = await fetch_api(session, "/flights/recent", method="POST", body={"count": 5})
                if recent and "results" in recent:
                    for raw_f in recent["results"]:
                        fid = str(raw_f.get("_id") or raw_f.get("id"))
                        if first_run:
                            state.setdefault(fid, {})["completed"] = True
                            continue
                        if fid in state and state[fid].get("completed"): continue
                        
                        # --- ЛОГІКА ДЛЯ ЗАКРИТИХ ТА ВИДАЛЕНИХ РЕЙСІВ ---
                        if raw_f.get("close"):
                            det = await fetch_api(session, f"/flight/{fid}")
                            
                            # Універсальний аналізатор запобіжників
                            def check_safeguards(api_data):
                                if not api_data or "flight" not in api_data:
                                    return "API повернуло порожню відповідь (немає даних про рейс)"
                                fl = api_data["flight"]
                                totals = fl.get("result", {}).get("totals", {})
                                d = totals.get("distance", 0)
                                t_time = totals.get("time", 0)
                                if d == 0 and t_time == 0:
                                    return f"Нульові показники (Дистанція: {d} nm, Час: {t_time} min)"
                                callsign = fl.get("flightNumber") or fl.get("callsign")
                                if not callsign or callsign == "N/A":
                                    return "Відсутній позивний (Callsign = N/A)"
                                return None # Якщо None - рейс ідеальний
                                
                            reason = check_safeguards(det)
                            
                            if reason:
                                for attempt in range(1, 4): # Робимо рівно 3 спроби
                                    print(f"⚠️ Рейс {fid} спіймав запобіжник ({reason}). Спроба {attempt}/3. Чекаю 3 секунди...")
                                    await asyncio.sleep(3) # Чекаємо САМЕ 3 секунди
                                    
                                    # Робимо ТОЙ САМИЙ ЗАПИТ ще раз
                                    det = await fetch_api(session, f"/flight/{fid}")
                                    reason = check_safeguards(det)
                                    
                                    if not reason:
                                        break # Дані оновилися, запобіжник знято! Виходимо з циклу очікування
                                        
                                if reason: # Якщо після всіх 3 спроб все ще є баг
                                    print(f"🙈 Проігноровано рейс {fid} | {reason}")
                                    state.setdefault(fid, {})["completed"] = True
                                    
                                    # Відправляємо звіт тобі в ПП з причиною
                                    try:
                                        owner = await client.fetch_user(ADMIN_IDS[0])
                                        await owner.send(
                                            f"🙈 **Проігноровано багований рейс!**\n"
                                            f"🔗 **ID:** `{fid}`\n"
                                            f"🌐 [Відкрити рейс на Newsky](https://newsky.app/flight/{fid})\n"
                                            f"🛑 **Причина ігнору:** {reason}\n"
                                            f"*(Дані не оновилися навіть після трьох спроб по 3 секунди)*"
                                        )
                                    except Exception as e:
                                        print(f"Помилка відправки в ПП: {e}")
                                        
                                    continue # Скидаємо рейс і йдемо далі

                            # Якщо пройшли запобіжники (одразу або після вдалих спроб)
                            f = det["flight"]
                            cs = f.get("flightNumber") or f.get("callsign") or "N/A"
                            if cs == "N/A": continue

                            reply_id = state.get(fid, {}).get("msg_id")
                            await send_flight_message(channel, "Completed", f, "result", reply_to_id=reply_id)
                            
                            # 🔥 НОВЕ: Збір статистики після посадки 🔥
                            week_tag = state.get(fid, {}).get("week") or get_iso_week()
                            update_weekly_stats(f, week_tag)
                            
                            # ==========================================
                            # 🚀 МАГІЯ GITHUB (ЗАПИС РЕЙСУ НА САЙТ)
                            # ==========================================
                            # Визначаємо тиждень за часом посадки (arrTimeAct), як ти і просив!
                            arrival_time = f.get("arrTimeAct") or f.get("close")
                            target_week = get_iso_week(arrival_time)
                            
                            clean_flight = format_flight_for_db(f)
                            
                            pilot_data = f.get("pilot", {})
                            pilot_id = pilot_data.get("_id", "unknown")
                            pilot_name = pilot_data.get("fullname", "Unknown Pilot")
                            pilot_avatar = pilot_data.get("avatar", "default")
                            
                            # Відправляємо на GitHub як окреме фонове завдання, 
                            # щоб бот не зависав і миттєво відправляв повідомлення в Discord
                            client.loop.create_task(
                                save_flight_to_github(clean_flight, pilot_id, pilot_name, pilot_avatar, target_week)
                            )
                            # ==========================================
                            
                            state.setdefault(fid, {})["completed"] = True
                            print(f"✅ Report Sent: {cs}")
                        
                        elif raw_f.get("deleted"):
                            
                            det = await fetch_api(session, f"/flight/{fid}")
                            if not det or "flight" not in det: continue
                            f = det["flight"]
                            cs = f.get("flightNumber") or f.get("callsign") or "N/A"
                            if cs == "N/A": continue

                            reply_id = state.get(fid, {}).get("msg_id")
                            await send_flight_message(channel, "Cancelled", f, "ongoing", reply_to_id=reply_id)
                            state.setdefault(fid, {})["completed"] = True
                            print(f"⚫ Cancel Report Sent: {cs}")

                if first_run:
                    print("🔕 First run sync complete. No spam.")
                    first_run = False

                save_state(state)
                
                # 🔥 ОНОВЛЕНИЙ ВИКЛИК: Передаємо список живих рейсів (ongoing_ids)
                if ongoing_ids is not None:
                    await check_and_publish_weekly_stats(channel, state, ongoing_ids)
                
            except Exception as e: 
                print(f"Loop Error: {e}")
            
            await asyncio.sleep(CHECK_INTERVAL)

# --- ⚡ РАДАР РЕАКЦІЙ (МИТТЄВЕ ВИДАЛЕННЯ) ---
@client.event
async def on_raw_reaction_add(payload):
    if payload.user_id == client.user.id:
        return

    if payload.message_id in BANNED_WOW_MESSAGES:
        try:
            channel = client.get_channel(payload.channel_id)
            if not channel: return
            
            message = await channel.fetch_message(payload.message_id)
            
            user = payload.member
            if not user:
                user = await client.fetch_user(payload.user_id)

            await message.remove_reaction(payload.emoji, user)
        except discord.Forbidden:
            print("⚠️ Error: Bot lacks 'Manage Messages' permission on the server!")
        except Exception as e:
            print(f"Error removing reaction: {e}")

# ====================================================================
# 🌐 WEBHOOK СЕРВЕР ДЛЯ NEWSKY (ПРИЙОМ ЗАЯВОК)
# ====================================================================
async def newsky_webhook(request):
    try:
        data = await request.json()
        print(f"📥 [WEBHOOK DATA]: {data}") # Лог для перевірки ключів!

        channel = client.get_channel(CHANNEL_ID) # Тут має бути ID твого каналу
        if not channel:
            return web.Response(text="Channel not found", status=500)

        # 1. Якщо Newsky надсилає вже готовий дизайн Discord
        if "embeds" in data and len(data["embeds"]) > 0:
            embed = discord.Embed.from_dict(data["embeds"][0])
            await channel.send(embed=embed)
            return web.Response(text="OK", status=200)

        # 2. Якщо потрібно зібрати дизайн самостійно
        pilot_data = data.get("pilot", {})
        pilot_name = pilot_data.get("fullname") or pilot_data.get("username") or "Unknown Pilot"
        
        embed = discord.Embed(
            title=f"👋 Pilot application: {pilot_name}",
            color=0x2b5293 
        )

        flights = pilot_data.get("flights", 0)
        hours = pilot_data.get("hours", 0.0)
        rating = pilot_data.get("rating", 0.0)
        embed.description = f"🔢 **{flights} flights** ㅤ ⏱️ **{hours}h** ㅤ 🟩 **{rating}**\n"

        # Літаки (динамічно)
        aircraft_list = pilot_data.get("most_active_aircraft", []) 
        if aircraft_list:
            embed.add_field(name="\u200b\n✈️ Most active on type:", value="", inline=False)
            for ac in aircraft_list[:3]:
                ac_type = ac.get("type", "Unknown")
                ac_flights = ac.get("flights", 0)
                ac_rating = ac.get("rating", 0.0)
                embed.add_field(name=ac_type, value=f"*flights:* {ac_flights}\n*rating:* {ac_rating}", inline=True)

        # Компанії (динамічно)
        airlines_list = pilot_data.get("previous_employment", [])
        if airlines_list:
            embed.add_field(name="\u200b\n💺 Previous employment:", value="", inline=False)
            for airline in airlines_list[:3]:
                al_name = airline.get("name", airline.get("airline", "Unknown Airline"))
                al_flights = airline.get("flights", 0)
                al_rating = airline.get("rating", 0.0)
                embed.add_field(name=al_name, value=f"*flights:* {al_flights}\n*rating:* {al_rating}", inline=False)

        await channel.send(embed=embed)
        return web.Response(text="OK", status=200)

    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return web.Response(text="Error", status=500)

async def start_web_server():
    app = web.Application()
    app.add_routes([web.post('/webhook', newsky_webhook)])
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web-сервер успішно запущено на порту {port}! (Шлях: /webhook)")
# ====================================================================

@tasks.loop(minutes=10)
async def check_charters_task():
    if not NEWSKY_SID:
        return
        
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        return
        
    ns_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
        "Accept": "application/json"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://newsky.app/api/charter/recent/pax", headers=ns_headers, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # 🔥 ВИКОРИСТОВУЄМО НОВИЙ ФАЙЛ ПАМ'ЯТІ 🔥
                    charters_state = load_charters_state()
                    updated = False
                    
                    for charter in data:
                        cid = str(charter.get("_id"))
                        dep_icao = charter.get("dep", {}).get("icao", "")
                        arr_icao = charter.get("arr", {}).get("icao", "")
                        
                        # Перевіряємо, чи є Україна (UK) у вильоті або прильоті
                        if dep_icao.startswith("UK") or arr_icao.startswith("UK"):
                            # Якщо ID ще немає в charters.json
                            if cid not in charters_state:
                                dep_str = format_airport_string(dep_icao, charter.get("dep", {}).get("name", ""))
                                arr_str = format_airport_string(arr_icao, charter.get("arr", {}).get("name", ""))
                                
                                exp_dt = datetime.fromisoformat(charter.get("activeTo", "").replace("Z", "+00:00"))
                                exp_str = exp_dt.strftime("%d %b, %H:%M UTC")
                                
                                quota = charter.get("quota", 0)
                                dist = charter.get("distance", 0)
                                
                                desc = (
                                    f"{dep_str} ➔ {arr_str}\n\n"
                                    f"📊 **Contract Info**\n"
                                    f"╰ 👫 Quota: **{quota} Pax**\n"
                                    f"╰ 📏 Dist: **{dist} nm**\n"
                                    f"╰ ⏳ Exp: **{exp_str}**"
                                )
                                
                                embed = discord.Embed(
                                    title="<:charter:1499317989483221063> New Charter Contract Available!", 
                                    description=desc, 
                                    color=0xffcc00 # Жовтий колір
                                )
                                
                                await channel.send(embed=embed)
                                
                                # Записуємо в charters.json
                                charters_state[cid] = {"notified": True}
                                updated = True
                                await asyncio.sleep(1) # пауза між відправками, щоб не спамити API Discord
                                
                    # Зберігаємо файл тільки якщо були знайдені нові чартери
                    if updated:
                        save_charters_state(charters_state)
    except Exception as e:
        print(f"Error checking charters: {e}")

# ====================================================================
# 🚀 НОВА СИСТЕМА: MASTER GITHUB SYNC (АВІАЛІНІЇ, ТУРИ, АЕРОПОРТИ)
# ====================================================================

async def fetch_airlines_data(session):
    ns_headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {"skip": 0, "count": 30, "needle": "", "sort": "stats.recentFlights", "order": -1}
    async with session.post("https://newsky.app/api/airline/search", headers=ns_headers, json=payload) as resp:
        if resp.status != 200: return None
        data = await resp.json()
        
    clean_airlines = []
    for airline in data.get("results", []):
        clean_airlines.append({
            "icao": airline.get("icao"),
            "fullname": airline.get("fullname"),
            "shortname": airline.get("shortname"),
            "countryCode": airline.get("countryCode"),
            "stats": airline.get("stats", {})
        })
    return json.dumps(clean_airlines, ensure_ascii=False, indent=4) if clean_airlines else None

async def fetch_awards_data(session):
    ns_headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload_awards = {"activeOnly": False, "count": 100, "needle": "", "order": 1, "showClosed": True, "skip": 0, "sort": "title"}
    
    airline_id = "695810be3dc76275ba8befa9"
    async with session.post(f"https://newsky.app/api/airline/{airline_id}/awards", headers=ns_headers, json=payload_awards) as resp:
        if resp.status != 200: return None, None
        awards_data = await resp.json()

    pilots_progress = {}
    payload_pilots = {"count": 100, "needle": "", "order": 1, "skip": 0, "sort": "createdAt"}

    for award in awards_data.get("results", []):
        award_id = str(award.get("_id"))
        award_title = award.get("title", "Unknown Award")
        total_legs = len(award.get("details", {}).get("legs", []))
        
        async with session.post(f"https://newsky.app/api/award/{award_id}/pilots", headers=ns_headers, json=payload_pilots) as p_resp:
            if p_resp.status != 200: continue
            pilots_data = await p_resp.json()
            
            for p_info in pilots_data.get("results", []):
                p_id = str(p_info.get("_id"))
                if p_id not in pilots_progress:
                    pilots_progress[p_id] = {"pilot_id": p_id, "fullname": p_info.get("fullname", "Unknown"), "awards": []}
                
                completed_legs = len(p_info.get("progress", {}).get("legs", []))
                pilots_progress[p_id]["awards"].append({
                    "award_id": award_id,
                    "award_title": award_title,
                    "status": "completed" if bool(p_info.get("completed")) else "in_progress",
                    "completed_date": p_info.get("completed"),
                    "progress_pct": p_info.get("progressPct", 0),
                    "legs_done": f"{completed_legs}/{total_legs}"
                })
        await asyncio.sleep(0.1)

    awards_str = json.dumps(awards_data, ensure_ascii=False, indent=4)
    pilots_str = json.dumps(list(pilots_progress.values()), ensure_ascii=False, indent=4)
    return awards_str, pilots_str

async def fetch_demand_data(session):
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.raw"}
    async with session.get(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE_PATH}", headers=gh_headers) as raw_resp:
        if raw_resp.status != 200: return None
        old_text = await raw_resp.text(encoding='utf-8')

    icaos = []
    for line in old_text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if "icao" in data: icaos.append(data["icao"])
            except: continue

    if not icaos: return None

    ns_headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"sid={NEWSKY_SID}" if not NEWSKY_SID.startswith("sid=") else NEWSKY_SID,
        "Accept": "application/json"
    }
    fresh_data = []
    for icao in icaos:
        async with session.get(f"https://newsky.app/api/airport/{icao}", headers=ns_headers, timeout=10) as ns_resp:
            if ns_resp.status == 200:
                data = await ns_resp.json()
                fresh_data.append(json.dumps(data, ensure_ascii=False, separators=(', ', ': ')))
        await asyncio.sleep(0.1)

    if not fresh_data: return None
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return f"UPDATED: {now_str}\n\n" + "\n\n---\n\n".join(fresh_data)

# ФУНКЦІЯ ЄДИНОГО КОМІТУ (З РОЗУМНИМИ ПОВТОРАМИ ТА ЛОГАМИ)
async def push_to_github_batch(session, files_dict, commit_msg, max_retries=3):
    if not files_dict or not GITHUB_TOKEN: return False
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    for attempt in range(1, max_retries + 1):
        try:
            # 1. Отримуємо SHA останнього коміту
            async with session.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/main", headers=gh_headers) as r:
                if r.status != 200: 
                    print(f"⚠️ GitHub Error (Крок 1) [Спроба {attempt}]: Статус {r.status}. {await r.text()}")
                    await asyncio.sleep(2)
                    continue
                latest_commit_sha = (await r.json())['object']['sha']

            # 2. Отримуємо SHA базового дерева
            async with session.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/commits/{latest_commit_sha}", headers=gh_headers) as r:
                if r.status != 200:
                    print(f"⚠️ GitHub Error (Крок 2) [Спроба {attempt}]: Статус {r.status}")
                    await asyncio.sleep(2)
                    continue
                base_tree_sha = (await r.json())['tree']['sha']

            # 3. Завантажуємо нові файли як Blobs
            tree_items = []
            for path, content in files_dict.items():
                async with session.post(f"https://api.github.com/repos/{GITHUB_REPO}/git/blobs", headers=gh_headers, json={"content": content, "encoding": "utf-8"}) as r:
                    if r.status not in [200, 201]:
                        print(f"⚠️ GitHub Error (Крок 3): Не вдалося створити Blob для {path}")
                        continue
                    blob_sha = (await r.json())['sha']
                    tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

            if not tree_items:
                print("❌ Жоден файл не вдалося відправити (Blob).")
                return False

            # 4. Створюємо нове дерево
            async with session.post(f"https://api.github.com/repos/{GITHUB_REPO}/git/trees", headers=gh_headers, json={"base_tree": base_tree_sha, "tree": tree_items}) as r:
                if r.status not in [200, 201]:
                    print(f"⚠️ GitHub Error (Крок 4) [Спроба {attempt}]: Статус {r.status}")
                    await asyncio.sleep(2)
                    continue
                new_tree_sha = (await r.json())['sha']

            # 5. Робимо коміт
            async with session.post(f"https://api.github.com/repos/{GITHUB_REPO}/git/commits", headers=gh_headers, json={"message": commit_msg, "tree": new_tree_sha, "parents": [latest_commit_sha]}) as r:
                if r.status not in [200, 201]:
                    print(f"⚠️ GitHub Error (Крок 5) [Спроба {attempt}]: Статус {r.status}")
                    await asyncio.sleep(2)
                    continue
                new_commit_sha = (await r.json())['sha']

            # 6. Оновлюємо гілку main
            async with session.patch(f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/main", headers=gh_headers, json={"sha": new_commit_sha}) as r:
                if r.status == 200:
                    return True
                else:
                    error_text = await r.text()
                    print(f"⚠️ GitHub Error (Крок 6) [Спроба {attempt}]: Відхилено оновлення гілки. Причина: {error_text}")
                    # Конфлікт версій! Чекаємо 2 секунди і йдемо на нове коло (щоб взяти новий SHA)
                    await asyncio.sleep(2)
                    continue

        except Exception as e:
            print(f"❌ Критична помилка у push_to_github_batch (Спроба {attempt}): {e}")
            await asyncio.sleep(2)

    print("❌ Всі 3 спроби відправити дані на GitHub вичерпано. Рейс не записано.")
    return False

# ГОЛОВНИЙ ДИСПЕТЧЕР
@tasks.loop()
async def master_github_sync_task():
    # Розраховуємо час до початку наступної години (XX:00:00)
    now = datetime.now(timezone.utc)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    sleep_seconds = (next_hour - now).total_seconds()
    
    print(f"💤 Диспетчер синхронізації чекає {int(sleep_seconds/60)} хв. до {next_hour.strftime('%H:%M')} UTC.")
    await asyncio.sleep(sleep_seconds)

    # --- МИ ПРОКИНУЛИСЬ РІВНО В 00 ХВИЛИН ---
    current_hour = datetime.now(timezone.utc).hour
    print(f"🚀 Запуск масової синхронізації (Година: {current_hour}:00 UTC)")
    
    files_to_push = {}

    async with GITHUB_DB_LOCK:
        async with aiohttp.ClientSession() as session:
            try:
                # 1. Авіакомпанії (Щогодини)
                airlines_content = await fetch_airlines_data(session)
                if airlines_content: files_to_push["FLIGHTS/airlines.json"] = airlines_content

                # 2. Тури та Пілоти (Щогодини)
                awards_content, pilots_content = await fetch_awards_data(session)
                if awards_content and pilots_content:
                    files_to_push["FLIGHTS/awards.json"] = awards_content
                    files_to_push["FLIGHTS/pilots_awards.json"] = pilots_content

                # 3. Аеропорти та Чартери (Раз на 6 годин: 0, 6, 12, 18)
                if current_hour % 6 == 0:
                    print("🌍 Прийшов час оновлювати аеропорти (6-годинний цикл)!")
                    demand_content = await fetch_demand_data(session)
                    if demand_content:
                        files_to_push[GITHUB_FILE_PATH] = demand_content
                        
                        # 🔥 ГЕНЕРУЄМО АНАЛІТИКУ ДО КОМІТУ 🔥
                        charter_results = await run_analytics_pipeline(session, demand_content=demand_content)
                        if charter_results:
                            files_to_push["newsky-charter-results.txt"] = charter_results

                    # 🥟 Top-pool + guaranteed bonus awards (той самий 6-годинний цикл)
                    top_bonus_files = await run_top_bonus_pipeline(session)
                    if top_bonus_files:
                        files_to_push.update(top_bonus_files)

                # 4. ФІНАЛЬНИЙ ПУШ ВСЬОГО ОДНИМ КОМІТОМ
                if files_to_push:
                    success = await push_to_github_batch(session, files_to_push, f"🤖 Master Sync: Hour {current_hour} UTC update")
                    if success:
                        print(f"✅ Успішний Batch-Commit! Оновлено файлів: {len(files_to_push)}")
                    else:
                        print("❌ Помилка під час масового запису на GitHub.")

            except Exception as e:
                print(f"❌ Помилка в Master Sync Task: {e}")

# ==========================================
# ANALYTICS PIPELINE: REPORT & CHARTERS
# ==========================================

async def run_analytics_pipeline(session, demand_content=None, ctx=None):
    if ctx: await ctx.send("⏳ Step 1: Preparing data and downloading scripts...")

    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.raw"}
    base_raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"

    # 1. Записуємо базу аеропортів ЛОКАЛЬНО для скриптів
    if demand_content:
        with open("newsky-airports.txt", "w", encoding="utf-8") as f:
            f.write(demand_content)
    else:
        # Якщо даних немає (ручний запуск без оновлення), качаємо з гітхабу
        async with session.get(f"{base_raw_url}/newsky-airports.txt", headers=gh_headers) as resp:
            if resp.status == 200:
                with open("newsky-airports.txt", "wb") as f:
                    f.write(await resp.read())
            else:
                if ctx: await ctx.send("❌ Error: Could not fetch newsky-airports.txt from GitHub.")
                return None

    # 2. Качаємо скрипти
    scripts = ["newsky_report.py", "newsky_charter_with_tops_v4.py"]
    for file_name in scripts:
        async with session.get(f"{base_raw_url}/{file_name}", headers=gh_headers) as resp:
            if resp.status == 200:
                with open(file_name, "wb") as f:
                    f.write(await resp.read())
            else:
                if ctx: await ctx.send(f"❌ Error downloading {file_name}: HTTP {resp.status}")
                return None

    if ctx: await ctx.send("⚙️ **Step 2: Running analytics (Report ➔ Charters)...**")

    # 3. Запускаємо обчислення
    try:
        subprocess.run([sys.executable, "newsky_report.py"], check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, "newsky_charter_with_tops_v4.py"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if ctx: await ctx.send(f"❌ **Script Error!**\n{e.stderr[-1000:]}")
        return None

    if not os.path.exists("newsky-charter-results.txt"):
        if ctx: await ctx.send("❌ File `newsky-charter-results.txt` was not created.")
        return None

    # 4. Читаємо готовий результат
    with open("newsky-charter-results.txt", "r", encoding="utf-8") as f:
        final_text = f.read()

    # 5. Прибираємо всі тимчасові файли
    cleanup_files = scripts + ["newsky-airports.txt", "newsky-airports-report.txt", "newsky-charter-results.txt"]
    for f in cleanup_files:
        if os.path.exists(f):
            os.remove(f)

    # 6. Повертаємо текст (щоб головний цикл закинув його в Batch-Commit)
    if ctx: await ctx.send("✅ **Analytics completed locally! Ready to push.**")
    return final_text

async def github_download_file(session, remote_path, local_path, required=True):
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.raw", "Cache-Control": "no-cache"}
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{remote_path}?t={int(time.time())}"
    async with session.get(url, headers=gh_headers) as resp:
        if resp.status == 200:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(await resp.read())
            return True
        if required:
            print(f"❌ Не вдалося скачати {remote_path}: HTTP {resp.status}")
        return False

async def github_list_directory(session, remote_dir):
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Cache-Control": "no-cache"}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{remote_dir}?t={int(time.time())}"
    async with session.get(url, headers=gh_headers) as resp:
        if resp.status != 200:
            print(f"⚠️ Не вдалося прочитати папку {remote_dir}: HTTP {resp.status}")
            return []
        data = await resp.json()
        return data if isinstance(data, list) else []

async def github_download_json_directory(session, remote_dir, local_dir):
    items = await github_list_directory(session, remote_dir)
    downloaded = 0
    for item in items:
        name = item.get("name", "")
        item_type = item.get("type", "")
        item_path = item.get("path", f"{remote_dir}/{name}")
        if item_type == "dir":
            downloaded += await github_download_json_directory(session, item_path, local_dir / name)
        elif item_type == "file" and name.lower().endswith(".json"):
            if await github_download_file(session, item_path, local_dir / name, required=False):
                downloaded += 1
        await asyncio.sleep(0.05)
    return downloaded

def read_text_safe(path):
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None

async def run_top_bonus_pipeline(session, ctx=None):
    """Builds top-pool and guaranteed bonus JSON using the site JS scripts.

    This runs inside the existing Railway bot during the 6-hour master sync.
    It does not create a second Railway service. It prepares a temporary
    working copy from GitHub, runs the checked-in Node scripts, and returns
    only changed generated files for the existing batch GitHub commit.
    """
    if not GITHUB_TOKEN:
        print("⚠️ Немає GITHUB_TOKEN: top/bonus sync пропущено.")
        return {}

    node_bin = shutil.which("node")
    if not node_bin:
        print("⚠️ Node.js не знайдено у Railway image: top/bonus sync пропущено. Додай Node у build environment.")
        return {}

    workdir = Path("/tmp/ucaa-top-bonus-sync")
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "scripts").mkdir(parents=True, exist_ok=True)
    (workdir / "COMPANY" / "TOP-POOLS").mkdir(parents=True, exist_ok=True)
    (workdir / "FLIGHTS").mkdir(parents=True, exist_ok=True)

    required_files = [
        "ADcoordinates.json",
        "scripts/update-top-pool.js",
        "scripts/update-guaranteed-bonuses.js",
        "COMPANY/livery-matching.json",
        "COMPANY/ucaa-livery-database.json",
        "COMPANY/guaranteed-bonuses.json",
        "FLIGHTS/manifest.json"
    ]
    optional_files = [
        "COMPANY/top-pool-current.json",
        "COMPANY/top-awards-log.json",
        "FLIGHTS/archive.json"
    ]

    for remote_path in required_files:
        ok = await github_download_file(session, remote_path, workdir / remote_path, required=True)
        if not ok:
            return {}
    for remote_path in optional_files:
        await github_download_file(session, remote_path, workdir / remote_path, required=False)

    flights_count = await github_download_json_directory(session, "FLIGHTS", workdir / "FLIGHTS")
    top_archives_count = await github_download_json_directory(session, "COMPANY/TOP-POOLS", workdir / "COMPANY" / "TOP-POOLS")
    print(f"📦 Top/bonus workspace готовий: FLIGHTS json={flights_count}, TOP-POOLS json={top_archives_count}")

    tracked_paths = [
        "COMPANY/top-pool-current.json",
        "COMPANY/top-awards-log.json",
        "COMPANY/guaranteed-bonuses.json"
    ]
    for top_file in (workdir / "COMPANY" / "TOP-POOLS").glob("*.json"):
        tracked_paths.append(str(top_file.relative_to(workdir)).replace("\\", "/"))
    before = {path: read_text_safe(workdir / path) for path in tracked_paths}

    env = os.environ.copy()
    if NEWSKY_API_KEY and not env.get("NEWSKY_AIRLINE_TOKEN"):
        env["NEWSKY_AIRLINE_TOKEN"] = NEWSKY_API_KEY

    try:
        subprocess.run([node_bin, "scripts/update-top-pool.js"], cwd=str(workdir), env=env, check=True, capture_output=True, text=True, timeout=180)
        subprocess.run([node_bin, "scripts/update-guaranteed-bonuses.js"], cwd=str(workdir), env=env, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as e:
        print(f"❌ Top/bonus JS помилка:\nSTDOUT:\n{e.stdout[-1500:]}\nSTDERR:\n{e.stderr[-1500:]}")
        return {}
    except subprocess.TimeoutExpired as e:
        print(f"❌ Top/bonus JS timeout: {e}")
        return {}

    for top_file in (workdir / "COMPANY" / "TOP-POOLS").glob("*.json"):
        rel_path = str(top_file.relative_to(workdir)).replace("\\", "/")
        if rel_path not in tracked_paths:
            tracked_paths.append(rel_path)

    files_to_push = {}
    for rel_path in tracked_paths:
        full_path = workdir / rel_path
        content = read_text_safe(full_path)
        if content is None:
            continue
        if content != before.get(rel_path):
            files_to_push[rel_path] = content

    if files_to_push:
        print(f"🥟 Top/bonus sync підготував файлів до коміту: {len(files_to_push)}")
    else:
        print("✅ Top/bonus sync: змін немає.")
    return files_to_push
	
# --- 🚀 ЗАПУСК ГОЛОВНОГО ЦИКЛУ ---
@client.event
async def on_ready():
    global MONITORING_STARTED
    if MONITORING_STARTED: return
    MONITORING_STARTED = True
    
    # Запускаємо єдиний розумний диспетчер замість трьох старих!
    if not master_github_sync_task.is_running():
        master_github_sync_task.start()
        print("🤖 Master GitHub Sync Dispatcher started!")

    print(f"✅ Bot online: {client.user}")
    print("🚀 MONITORING STARTED")
    client.loop.create_task(status_loop())
    client.loop.create_task(main_loop())
    client.loop.create_task(start_web_server())

client.run(DISCORD_TOKEN)

