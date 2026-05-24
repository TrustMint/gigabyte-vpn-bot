import asyncio
import logging
import uuid
import os
import json
import sys
import secrets
import shutil
import smtplib
import tempfile
import time
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Dict, Tuple, List, Optional, Any

import httpx
import qrcode
from dotenv import load_dotenv
from supabase import create_async_client, AsyncClient

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ErrorEvent,
    BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment, CallbackQuery,
    FSInputFile, InputMediaPhoto
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums.parse_mode import ParseMode

load_dotenv()

# ====================== НАСТРОЙКИ ======================
BOT_TOKEN: str = os.getenv("BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required")

SUPABASE_URL: str = os.getenv("SUPABASE_URL") or ""
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY") or ""

ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()]
ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL") or ""
SMTP_HOST: str = os.getenv("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT: int = int(os.getenv("SMTP_PORT") or 587)
SMTP_USER: str = os.getenv("SMTP_USER") or ""
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD") or ""

ARBITRUM_WALLET: str = os.getenv("ARBITRUM_WALLET") or ""
USDT_CONTRACT: str = os.getenv("USDT_CONTRACT") or ""
USDC_CONTRACT: str = os.getenv("USDC_CONTRACT") or ""
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY") or ""

COUNTRIES = [
    "🇺🇸 США", "🇬🇧 Великобритания", "🇩🇪 Германия", "🇫🇷 Франция",
    "🇨🇦 Канада", "🇯🇵 Япония", "🇦🇺 Австралия", "🇳🇱 Нидерланды",
    "🇸🇬 Сингапур", "🇨🇭 Швейцария", "🇸🇪 Швеция", "🇳🇴 Норвегия",
    "🇩🇰 Дания", "🇫🇮 Финляндия", "🇧🇪 Бельгия", "🇦🇹 Австрия",
    "🇮🇪 Ирландия", "🇮🇱 Израиль", "🇰🇷 Южная Корея", "🇧🇷 Бразилия"
]

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ====================== ГЕНЕРАТОРЫ ======================
def generate_payment_uid() -> str:
    date_str = datetime.now().strftime("%Y%m%d")
    random_part = secrets.token_hex(5).upper()
    return f"PAY-{date_str}-{random_part}"

def generate_promo_code() -> str:
    return f"GIFT-{secrets.token_hex(8).upper()}"

def generate_sub_id() -> str:
    return secrets.token_hex(8)

def generate_client_email(user_id: int) -> str:
    return f"user-{user_id}_{secrets.token_hex(8)}"

def generate_ticket_id() -> str:
    return f"TICKET-{secrets.token_hex(6).upper()}"

def generate_request_id() -> str:
    return f"REQ-{secrets.token_hex(6).upper()}"

# ====================== КЛАСС УПРАВЛЕНИЯ КУРСАМИ ======================
class PriceManager:
    def __init__(self):
        self.usd_cbr = 78.73
        self.usd_market = 78.73
        self.usd_effective = 78.73
        self.usdt_p2p = 78.50
        self.last_update = 0
        self.stars_usd_rate = 0.02

    async def update_rates(self):
        if time.time() - self.last_update < 3600:
            return
        cbr_rate = None
        market_rate = None
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    cbr_rate = float(data["Valute"]["USD"]["Value"])
                    self.usd_cbr = cbr_rate
                    logger.info(f"✅ Курс USD от ЦБ: {cbr_rate:.2f} ₽")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения курса ЦБ: {e}")
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    market_rate = float(data["rates"]["RUB"])
                    self.usd_market = market_rate
                    logger.info(f"✅ Рыночный курс USD: {market_rate:.2f} ₽")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения рыночного курса: {e}")
        if cbr_rate and market_rate:
            self.usd_effective = (cbr_rate + market_rate) / 2
        elif cbr_rate:
            self.usd_effective = cbr_rate
        elif market_rate:
            self.usd_effective = market_rate

        # Источник: CoinGecko (USDT/RUB)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub", timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    rate = data.get("tether", {}).get("rub")
                    if rate:
                        self.usdt_p2p = float(rate)
                        logger.info(f"✅ Курс USDT/RUB от CoinGecko: {self.usdt_p2p:.2f} ₽")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения курса USDT/RUB: {e}")

        self.last_update = time.time()

    async def get_stars_price(self, rub_amount: float) -> int:
        await self.update_rates()
        usd_amount = rub_amount / self.usd_effective
        stars = int(usd_amount / self.stars_usd_rate)
        stars = max(1, int(stars * 1.15))
        logger.info(f"💰 Конвертация {rub_amount} ₽ → {stars} Stars")
        return stars

    async def get_usdt_price_rub(self, usd_amount: float) -> float:
        await self.update_rates()
        rub_amount = usd_amount * self.usdt_p2p * 1.02
        return round(rub_amount, 2)

    async def get_rates_info(self) -> str:
        await self.update_rates()
        info = (
            f"📈 <b>Актуальные курсы валют</b>\n\n"
            f"🇷🇺 ЦБ РФ: <b>{self.usd_cbr:.2f}</b> ₽\n"
            f"🌐 Рыночный (exchangerate): <b>{self.usd_market:.2f}</b> ₽\n"
            f"⭐ <b>Эффективный курс (средний): {self.usd_effective:.2f} ₽</b>\n"
            f"₿ USDT/RUB (CoinGecko): <b>{self.usdt_p2p:.2f}</b> ₽\n"
            f"💎 Stars/USD: 1 Star = ${self.stars_usd_rate:.3f}\n\n"
            f"💰 Комиссия приёма крипты: +2%\n"
            f"⭐ Комиссия вывода Stars: +15%\n\n"
            f"<i>Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"
        )
        return info

price_manager = PriceManager()

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_user_identifier(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> str:
    if username:
        return f"@{username}"
    elif full_name:
        return full_name
    else:
        return str(user_id)

def get_user_identifier_by_data(username: Optional[str], full_name: Optional[str], user_id: int) -> str:
    if username:
        return f"@{username}"
    elif full_name:
        return full_name
    else:
        return str(user_id)

def get_user_display(user) -> str:
    if user.username:
        return f"@{user.username}"
    elif user.full_name:
        return user.full_name
    else:
        return f"ID: {user.id}"

def generate_wallet_qr(wallet_address: str, amount: Optional[float] = None, currency: Optional[str] = None) -> BufferedInputFile:
    if amount and currency:
        text = f"{wallet_address}\nСумма: {amount:.2f} {currency}"
    else:
        text = wallet_address
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return BufferedInputFile(bio.read(), filename="wallet_qr.png")

# ====================== БАЗА ДАННЫХ (SUPABASE) ======================
supabase: Optional[AsyncClient] = None

async def init_supabase() -> AsyncClient:
    global supabase
    if supabase is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("❌ SUPABASE_URL или SUPABASE_KEY не заданы в .env!")
        try:
            supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
            logger.info("✅ Подключение к Supabase установлено успешно.")
            await init_supabase_tables()
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Supabase: {e}")
            raise
    return supabase

async def init_supabase_tables():
    tables = ["users", "servers", "subscriptions", "payments", "tickets", "ticket_messages", "country_requests", "tariffs", "pending_confirmations", "promo_keys"]
    for table in tables:
        try:
            await supabase.table(table).select("*").limit(1).execute()
        except Exception:
            logger.warning(f"⚠️ Таблица {table} не существует. Создайте её в Supabase SQL Editor.")
    res = await supabase.table("tariffs").select("*").execute()
    if not res.data:
        await supabase.table("tariffs").insert([
            {"months": 0.033, "rub": 50},
            {"months": 1, "rub": 290},
            {"months": 3, "rub": 725},
            {"months": 6, "rub": 1305},
            {"months": 12, "rub": 2465}
        ]).execute()
        logger.info("✅ Базовые тарифы добавлены в Supabase.")
    servers = await supabase.table("servers").select("*").execute()
    if not servers.data:
        await supabase.table("servers").insert({
            "name": "🇫🇷 Франция",
            "ip": os.getenv("SERVER_IP", "185.193.89.183"),
            "panel_url": os.getenv("PANEL_URL"),
            "panel_login": os.getenv("PANEL_LOGIN"),
            "panel_pass": os.getenv("PANEL_PASS"),
            "inbound_id": int(os.getenv("INBOUND_ID", 0)),
            "client_port": int(os.getenv("CLIENT_PORT", 0)),
            "sub_port": int(os.getenv("SUB_PORT", 2096)),
            "sub_path": os.getenv("SUB_PATH"),
            "pbk": os.getenv("PBK"),
            "sni": os.getenv("SNI"),
            "short_id": os.getenv("SHORT_ID"),
            "fp": os.getenv("FP"),
            "is_active": True
        }).execute()
        logger.info("✅ Сервер из .env добавлен в таблицу servers.")
    logger.info("✅ Таблицы Supabase проверены.")

async def ensure_user_exists_supabase(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    await init_supabase()
    try:
        res = await supabase.table("users").select("*").eq("user_id", user_id).execute()
        if not res.data:
            await supabase.table("users").insert({
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "created_at": datetime.now().isoformat()
            }).execute()
        else:
            await supabase.table("users").update({
                "username": username,
                "full_name": full_name
            }).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка в ensure_user_exists_supabase: {e}")

async def load_servers_from_supabase() -> List[Dict]:
    await init_supabase()
    try:
        res = await supabase.table("servers").select("*").eq("is_active", True).execute()
        return res.data
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки серверов: {e}")
        return []

# ====================== 3X-UI API ======================
class XUIApi:
    def __init__(self, server: dict):
        self.server = server
        self.base_url = server["panel_url"].rstrip("/")
        self.username = server["panel_login"]
        self.password = server["panel_pass"]
        self.client = httpx.AsyncClient(timeout=15, verify=False)
        self.cookies = None

    async def login(self) -> bool:
        try:
            r = await self.client.post(
                f"{self.base_url}/login",
                json={"username": self.username, "password": self.password}
            )
            if r.status_code == 200 and r.json().get("success"):
                self.cookies = r.cookies
                logger.info(f"✅ Успешный вход в панель {self.server['name']}")
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка входа в панель {self.server['name']}: {e}")
        return False

    async def add_client(self, client: dict) -> bool:
        if not self.cookies and not await self.login():
            return False
        form = {"id": str(self.server["inbound_id"]), "settings": json.dumps({"clients": [client]})}
        try:
            r = await self.client.post(
                f"{self.base_url}/panel/api/inbounds/addClient",
                data=form,
                cookies=self.cookies,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            if r.status_code == 200 and r.json().get("success"):
                logger.info(f"✅ Клиент {client.get('email')} добавлен в панель {self.server['name']}")
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка добавления клиента: {e}")
        return False

    async def remove_client(self, client_uuid: str) -> bool:
        if not self.cookies and not await self.login():
            return False
        try:
            r = await self.client.post(
                f"{self.base_url}/panel/api/inbounds/delClient",
                json={"id": self.server["inbound_id"], "clientId": client_uuid},
                cookies=self.cookies,
                headers={"Content-Type": "application/json"}
            )
            if r.status_code == 200 and r.json().get("success", False):
                logger.info(f"✅ Клиент {client_uuid} удалён из панели {self.server['name']}")
                return True
            else:
                logger.error(f"❌ Ошибка удаления клиента {client_uuid}: {r.text}")
        except Exception as e:
            logger.error(f"❌ Ошибка удаления клиента: {e}")
        return False

    async def get_clients(self) -> List[dict]:
        if not self.cookies and not await self.login():
            return []
        try:
            r = await self.client.get(
                f"{self.base_url}/panel/api/inbounds/get/{self.server['inbound_id']}",
                cookies=self.cookies
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    inbound = data.get("obj", {})
                    settings_str = inbound.get("settings", "{}")
                    try:
                        settings = json.loads(settings_str)
                    except:
                        settings = {}
                    clients = settings.get("clients", [])
                    return clients
        except Exception as e:
            logger.error(f"❌ Ошибка получения клиентов из панели {self.server['name']}: {e}")
        return []

# ====================== СИНХРОНИЗАЦИЯ КЛИЕНТОВ ======================
async def sync_all_servers_with_supabase():
    logger.info("🔄 Запуск полной синхронизации клиентов...")
    servers = await load_servers_from_supabase()
    if not servers:
        logger.warning("⚠️ Нет активных серверов для синхронизации.")
        return

    for server in servers:
        server_id = server["id"]
        xui = XUIApi(server)
        panel_clients = await xui.get_clients()

        if not panel_clients:
            logger.warning(f"⚠️ Не удалось получить клиентов из панели {server['name']}.")
            continue

        for p_client in panel_clients:
            existing = await supabase.table("subscriptions").select("*").eq("client_uuid", p_client["id"]).eq("server_id", server_id).execute()
            if not existing.data:
                try:
                    await supabase.table("subscriptions").insert({
                        "user_id": None,
                        "server_id": server_id,
                        "client_uuid": p_client["id"],
                        "email": p_client.get("email"),
                        "sub_id": p_client.get("subId", generate_sub_id()),
                        "expiry_date": p_client.get("expiryTime"),
                        "status": "active",
                        "last_sync": int(datetime.now().timestamp())
                    }).execute()
                    logger.info(f"➕ Клиент {p_client['id']} добавлен из панели {server['name']} в БД.")
                except Exception as e:
                    logger.error(f"❌ Ошибка добавления клиента {p_client['id']}: {e}")

        db_clients = await supabase.table("subscriptions").select("*").eq("server_id", server_id).eq("status", "active").execute()
        for db_client in db_clients.data:
            found = any(c["id"] == db_client["client_uuid"] for c in panel_clients)
            if not found:
                logger.warning(f"🔄 Клиент {db_client['client_uuid']} не найден на панели {server['name']}. Восстанавливаем...")
                client_dict = {
                    "id": db_client["client_uuid"],
                    "flow": "xtls-rprx-vision",
                    "email": db_client["email"],
                    "limitIp": 2,
                    "totalGB": 0,
                    "expiryTime": db_client["expiry_date"],
                    "enable": True,
                    "tgId": str(db_client.get("user_id") or ""),
                    "subId": db_client["sub_id"],
                    "reset": 0
                }
                await xui.add_client(client_dict)
                await supabase.table("subscriptions").update({"last_sync": int(datetime.now().timestamp())}).eq("id", db_client["id"]).execute()
                logger.info(f"✅ Клиент {db_client['client_uuid']} успешно восстановлен на сервере {server['name']}")

    logger.info("✅ Полная синхронизация клиентов завершена.")

async def force_import_clients_from_panel(message: Message = None):
    await init_supabase()
    servers = await load_servers_from_supabase()
    if not servers:
        if message:
            await message.answer("❌ Нет активных серверов.")
        return
    total_imported = 0
    for server in servers:
        xui = XUIApi(server)
        panel_clients = await xui.get_clients()
        if not panel_clients:
            if message:
                await message.answer(f"❌ Не удалось получить клиентов с сервера {server['name']}")
            continue
        count = 0
        for p_client in panel_clients:
            existing = await supabase.table("subscriptions").select("*").eq("client_uuid", p_client["id"]).eq("server_id", server["id"]).execute()
            if not existing.data:
                try:
                    await supabase.table("subscriptions").insert({
                        "user_id": None,
                        "server_id": server["id"],
                        "client_uuid": p_client["id"],
                        "email": p_client.get("email"),
                        "sub_id": p_client.get("subId", generate_sub_id()),
                        "expiry_date": p_client.get("expiryTime"),
                        "status": "active",
                        "last_sync": int(datetime.now().timestamp())
                    }).execute()
                    count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка импорта клиента {p_client['id']}: {e}")
        total_imported += count
        if message:
            await message.answer(f"✅ С сервера {server['name']} импортировано {count} новых клиентов.")
    if message:
        await message.answer(f"🎉 Всего импортировано {total_imported} клиентов.")

async def sync_all_servers_periodically():
    while True:
        await asyncio.sleep(3600)
        await sync_all_servers_with_supabase()

# ====================== БЭКАП В SUPABASE STORAGE ======================
async def backup_database_to_supabase():
    await init_supabase()
    bucket_name = "database-backups"

    try:
        await supabase.storage.get_bucket(bucket_name)
    except Exception:
        logger.info(f"📦 Bucket '{bucket_name}' не найден, создаем...")
        await supabase.storage.create_bucket(bucket_name, public=False)

    all_tables = ["users", "servers", "subscriptions", "payments", "tickets", "ticket_messages", "country_requests", "tariffs", "pending_confirmations", "promo_keys"]
    backup_data = {}
    for table in all_tables:
        res = await supabase.table(table).select("*").execute()
        backup_data[table] = res.data

    with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False, encoding='utf-8') as tmp:
        json.dump(backup_data, tmp, indent=2, ensure_ascii=False)
        tmp_path = tmp.name

    try:
        with open(tmp_path, 'rb') as f:
            file_name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            await supabase.storage.from_(bucket_name).upload(file_name, f, {"content-type": "application/json"})
            logger.info(f"✅ Бэкап загружен в Supabase Storage: {file_name}")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки бэкапа в Supabase Storage: {e}")
    finally:
        os.unlink(tmp_path)

async def send_backup_to_admin():
    await init_supabase()
    bucket_name = "database-backups"
    try:
        files = await supabase.storage.from_(bucket_name).list()
        if not files:
            logger.warning("⚠️ Нет файлов в бакете для отправки.")
            return
        latest_file = sorted(files, key=lambda x: x['created_at'], reverse=True)[0]['name']
        file_data = await supabase.storage.from_(bucket_name).download(latest_file)

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_document(admin_id, BufferedInputFile(file_data, filename=latest_file), caption="📦 Ежедневный бэкап базы данных")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки бэкапа админу {admin_id}: {e}")

        if ADMIN_EMAIL and SMTP_USER and SMTP_PASSWORD:
            send_email_backup(ADMIN_EMAIL, latest_file, file_data)
    except Exception as e:
        logger.error(f"❌ Ошибка в send_backup_to_admin: {e}")

def send_email_backup(to_email: str, file_name: str, file_data: bytes):
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = f"Gigabyte Bot - Database Backup {datetime.now().strftime('%Y-%m-%d')}"
    body = "📦 Ежедневный бэкап базы данных бота Gigabyte."
    msg.attach(MIMEText(body, 'plain'))
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(file_data)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f"attachment; filename={file_name}")
    msg.attach(part)
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"✅ Бэкап отправлен на email {to_email}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки email: {e}")

async def daily_backup_task():
    while True:
        now = datetime.now()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        logger.info("🔄 Запуск ежедневного бэкапа...")
        await backup_database_to_supabase()
        await send_backup_to_admin()
        logger.info("✅ Ежедневный бэкап завершён.")

# ====================== ГЕНЕРАЦИЯ ССЫЛОК И ПОДПИСОК ======================
def generate_vless_link(server: dict, client_uuid: str) -> str:
    params = {
        "security": "reality", "fp": server["fp"], "pbk": server["pbk"],
        "sni": server["sni"], "sid": server["short_id"],
        "flow": "xtls-rprx-vision", "type": "tcp", "headerType": "none", "encryption": "none"
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"vless://{client_uuid}@{server['ip']}:{server['client_port']}?{query}#{server['name']}"

def generate_subscription_link(server: dict, sub_id: str) -> str:
    sub_port = server.get("sub_port", server.get("client_port", 443))
    return f"https://{server['ip']}:{sub_port}/{server['sub_path']}/{sub_id}"

def generate_config_for_connection(vless_link: str, sub_link: str) -> str:
    return (
        f"<b>🔗 VLESS-ссылка:</b>\n<code>{vless_link}</code>\n\n"
        f"<b>📡 Ссылка на подписку:</b>\n<code>{sub_link}</code>"
    )

async def create_subscription(user_id: int, server: dict, months: float, rub_amount: float, payment_id: Optional[int] = None) -> Optional[Tuple[str, str]]:
    client_uuid = str(uuid.uuid4())
    sub_id = generate_sub_id()
    email = generate_client_email(user_id)
    if months == -1:
        expiry = int((datetime.now() + timedelta(days=3650)).timestamp() * 1000)
    else:
        days = int(months * 30)
        expiry = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)

    payment_uid = None
    if payment_id:
        res = await supabase.table("payments").select("payment_uid").eq("id", payment_id).execute()
        if res.data:
            payment_uid = res.data[0]["payment_uid"]

    client_dict = {
        "id": client_uuid, "flow": "xtls-rprx-vision", "email": email,
        "limitIp": 2, "totalGB": 0, "expiryTime": expiry, "enable": True,
        "tgId": str(user_id), "subId": sub_id,
        "comment": f"Payment {payment_uid}" if payment_uid else "Admin created", "reset": 0
    }
    xui = XUIApi(server)
    if await xui.add_client(client_dict):
        await supabase.table("subscriptions").insert({
            "user_id": user_id,
            "server_id": server["id"],
            "client_uuid": client_uuid,
            "email": email,
            "sub_id": sub_id,
            "expiry_date": expiry,
            "status": "active",
            "last_sync": int(datetime.now().timestamp())
        }).execute()
        if payment_id:
            await supabase.table("payments").update({"status": "completed"}).eq("id", payment_id).execute()
            await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
        logger.info(f"✅ Подписка создана для {user_id} на сервере {server['name']}")
        return generate_vless_link(server, client_uuid), generate_subscription_link(server, sub_id)
    else:
        logger.error(f"❌ Не удалось создать подписку для {user_id} на сервере {server['name']}")
        return None

# ====================== ВЕРИФИКАЦИЯ КРИПТО ======================
async def verify_arbitrum_tx(tx_hash: str, currency: str, expected_usd: float, retries: int = 12) -> Tuple[bool, str]:
    if not ALCHEMY_API_KEY:
        return False, "Alchemy API ключ не настроен"
    contract = USDT_CONTRACT if currency == "USDT" else USDC_CONTRACT
    decimals = 6
    delays = [5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120, 150]
    last_reason = "Транзакция не найдена или не подтверждена"
    for attempt in range(retries):
        if attempt > 0:
            wait_time = delays[attempt - 1] if attempt - 1 < len(delays) else 150
            logger.info(f"⏳ Ожидание {wait_time} сек перед попыткой {attempt+1}/{retries} для TX {tx_hash}")
            await asyncio.sleep(wait_time)
        try:
            url = f"https://arb-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
            payload = {"jsonrpc": "2.0", "method": "eth_getTransactionReceipt", "params": [tx_hash], "id": 1}
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=payload, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                receipt = data.get("result")
                if not receipt:
                    continue
                if receipt.get("status") != "0x1":
                    last_reason = "Транзакция завершилась с ошибкой (status != 1)"
                    return False, last_reason
                logs = receipt.get("logs", [])
                for log in logs:
                    if log.get("address", "").lower() != contract.lower():
                        continue
                    topics = log.get("topics", [])
                    if len(topics) < 3:
                        continue
                    to_topic = topics[2]
                    if len(to_topic) >= 42:
                        to_address = "0x" + to_topic[-40:]
                    else:
                        continue
                    if to_address.lower() != ARBITRUM_WALLET.lower():
                        continue
                    value_hex = log.get("data", "0x0")
                    try:
                        value = int(value_hex, 16) / (10 ** decimals)
                    except:
                        continue
                    if abs(value - expected_usd) < 0.01:
                        logger.info(f"✅ Платёж подтверждён | TX: {tx_hash} | Сумма: {value:.6f} {currency}")
                        return True, "✅ Платёж подтверждён"
                    else:
                        last_reason = f"Неверная сумма: {value:.6f} {currency}"
                        return False, last_reason
                last_reason = "Перевод на наш кошелёк не обнаружен"
                return False, last_reason
        except Exception as e:
            logger.error(f"⚠️ Ошибка при проверке (попытка {attempt+1}): {e}")
            continue
    return False, last_reason

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ====================== FSM ======================
class BuyStates(StatesGroup):
    select_server = State()
    select_tariff = State()
    select_method = State()
    select_crypto_currency = State()
    waiting_crypto_payment = State()
    wait_crypto_hash = State()

class ExtendSubscriptionStates(StatesGroup):
    select_tariff = State()
    select_method = State()
    select_crypto_currency = State()
    waiting_crypto_payment = State()
    wait_crypto_hash = State()

class TicketStates(StatesGroup):
    waiting_question = State()
    waiting_reply = State()

class CountryRequestStates(StatesGroup):
    waiting_country = State()

class AdminPriceStates(StatesGroup):
    waiting_action = State()
    waiting_manual_input = State()

class AdminBroadcastStates(StatesGroup):
    waiting_message = State()

class AdminCreateSubStates(StatesGroup):
    waiting_user_id = State()
    waiting_months = State()

class AdminGenerateKeyStates(StatesGroup):
    waiting_months = State()

class ActivateKeyStates(StatesGroup):
    waiting_code = State()

class AdminCountryReplyStates(StatesGroup):
    waiting_reply_text = State()

class ResendHashState(StatesGroup):
    waiting_hash = State()

# ====================== БОТ ======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ====================== КЛАВИАТУРЫ ======================
def user_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🛒 Купить подписку")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="📱 Как подключиться")],
        [KeyboardButton(text="❓ Поддержка"), KeyboardButton(text="🌍 Запросить новую страну")],
        [KeyboardButton(text="🎫 Активировать ключ"), KeyboardButton(text="⭐ Купить звёзды")],
        [KeyboardButton(text="🗑 Удалить меня")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, persistent=True)

def admin_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📢 Сделать рассылку"), KeyboardButton(text="🎫 Тикеты поддержки"), KeyboardButton(text="🌍 Запросы на новую страну")],
        [KeyboardButton(text="🎫 Сгенерировать ключ"), KeyboardButton(text="📋 Список ключей"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="✨ Создать подписку (админ)"), KeyboardButton(text="💰 Изменить цены"), KeyboardButton(text="📈 Курс"), KeyboardButton(text="⭐ Баланс звезды")],
        [KeyboardButton(text="🔄 Синхронизировать серверы"), KeyboardButton(text="📥 Импорт клиентов из панели")],
        [KeyboardButton(text="👥 Управление пользователями")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, persistent=True)

def main_keyboard(is_admin_flag: bool = False) -> ReplyKeyboardMarkup:
    if is_admin_flag:
        return admin_keyboard()
    else:
        return user_keyboard()

def price_percent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="+10%"), KeyboardButton(text="+20%")],
            [KeyboardButton(text="+30%"), KeyboardButton(text="+50%")],
            [KeyboardButton(text="✏️ Ввести вручную")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )

def promo_months_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день", callback_data="promo_0.033"),
         InlineKeyboardButton(text="1 месяц", callback_data="promo_1"),
         InlineKeyboardButton(text="3 месяца", callback_data="promo_3")],
        [InlineKeyboardButton(text="6 месяцев", callback_data="promo_6"),
         InlineKeyboardButton(text="1 год", callback_data="promo_12"),
         InlineKeyboardButton(text="∞ Бессрочно", callback_data="promo_unlimited")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
    ])

# ====================== НАВИГАЦИЯ ======================
@router.message(F.text == "◀️ Назад")
async def back_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (BuyStates.select_server, ExtendSubscriptionStates.select_tariff, ActivateKeyStates.waiting_code, ResendHashState.waiting_hash):
        await state.clear()
        await message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(message.from_user.id)))
    elif current_state == BuyStates.select_tariff:
        await state.set_state(BuyStates.select_server)
        await show_servers(message)
    elif current_state == BuyStates.select_method:
        await state.set_state(BuyStates.select_tariff)
        await show_tariffs(message, state)
    elif current_state == BuyStates.select_crypto_currency:
        await state.set_state(BuyStates.select_method)
        await show_payment_methods(message)
    elif current_state == BuyStates.waiting_crypto_payment:
        await state.clear()
        await message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(message.from_user.id)))
    elif current_state == BuyStates.wait_crypto_hash:
        await state.set_state(BuyStates.select_crypto_currency)
        await show_crypto_currencies(message)
    elif current_state == ExtendSubscriptionStates.select_method:
        await state.set_state(ExtendSubscriptionStates.select_tariff)
        await show_tariffs(message, state)
    elif current_state == ExtendSubscriptionStates.select_crypto_currency:
        await state.set_state(ExtendSubscriptionStates.select_method)
        await show_payment_methods(message)
    elif current_state == ExtendSubscriptionStates.waiting_crypto_payment:
        await state.clear()
        await message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(message.from_user.id)))
    elif current_state == ExtendSubscriptionStates.wait_crypto_hash:
        await state.set_state(ExtendSubscriptionStates.select_crypto_currency)
        await show_crypto_currencies(message)
    else:
        await state.clear()
        await message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(message.from_user.id)))

@router.message(F.text == "❌ Отмена")
async def universal_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (BuyStates.wait_crypto_hash, ExtendSubscriptionStates.wait_crypto_hash):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отменить", callback_data="confirm_cancel"),
             InlineKeyboardButton(text="❌ Нет, продолжить", callback_data="cancel_cancel")]
        ])
        await message.answer("⚠️ <b>Вы уверены, что хотите отменить оплату?</b>\n\nВсе данные о заказе будут удалены.", parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await state.clear()
        await message.answer("✅ Действие отменено.", reply_markup=main_keyboard(is_admin(message.from_user.id)))

@router.callback_query(lambda c: c.data == "confirm_cancel")
async def confirm_cancel_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payment_id = data.get("payment_id")
    if payment_id:
        await supabase.table("payments").delete().eq("id", payment_id).execute()
        await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
    await state.clear()
    await callback.message.edit_text("✅ Оплата отменена, заказ удалён.")
    await callback.message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(callback.from_user.id)))
    await callback.answer()

@router.callback_query(lambda c: c.data == "cancel_cancel")
async def cancel_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("✅ Продолжаем ожидание хеша.", reply_markup=main_keyboard(is_admin(callback.from_user.id)))
    await callback.answer()

# ====================== /start ======================
@router.message(Command("start"))
async def cmd_start(message: Message):
    await load_tariffs()
    user_id = message.from_user.id
    await ensure_user_exists_supabase(user_id, message.from_user.username, message.from_user.full_name)
    await message.answer(
        "👋 <b>Добро пожаловать в Gigabyte</b>\n\n"
        "✨ Максимальная скорость\n"
        "🔒 Полная анонимность\n"
        "🛡️ Надёжная защита\n\n"
        "Выберите действие ниже 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(is_admin(user_id))
    )

# ====================== КНОПКА "КУПИТЬ ЗВЁЗДЫ" ======================
@router.message(F.text == "⭐ Купить звёзды")
async def buy_stars(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить Telegram Stars", url="https://t.me/PremiumBot")]
    ])
    await message.answer(
        "✨ <b>Пополнить баланс Telegram Stars</b>\n\n"
        "Вы можете приобрести звёзды у официального бота @PremiumBot.\n"
        "После покупки звёзд вернитесь и оплатите подписку через Telegram Stars.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

# ====================== БАЛАНС ЗВЁЗД ДЛЯ АДМИНА ======================
async def get_stars_balance(bot: Bot) -> dict:
    balance = await bot.get_star_transactions(limit=1)
    try:
        bal = await bot.get_my_star_balance()
        available = bal.amount
    except:
        available = 0
    total_earned = 0
    offset = 0
    limit = 100
    while True:
        txs = await bot.get_star_transactions(offset=offset, limit=limit)
        for tx in txs.transactions:
            if tx.amount > 0:
                total_earned += tx.amount
        if len(txs.transactions) < limit:
            break
        offset += len(txs.transactions)
    frozen = max(0, total_earned - available)
    return {"available": available, "frozen": frozen, "total": total_earned}

@router.message(F.text == "⭐ Баланс звезды")
async def admin_stars_balance(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Запрашиваю баланс звёзд у Telegram...")
    try:
        bal = await get_stars_balance(bot)
        text = (
            f"⭐ <b>Баланс звёзд бота</b>\n\n"
            f"💰 Всего заработано: <b>{bal['total']}</b> ⭐\n"
            f"❄️ Заморожено (в обработке): <b>{bal['frozen']}</b> ⭐\n"
            f"✅ Доступно к выводу: <b>{bal['available']}</b> ⭐\n\n"
            f"<i>Замороженные звёзды станут доступны через 21 день после получения.</i>"
        )
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True))
    except Exception as e:
        logger.error(f"Ошибка получения баланса звёзд: {e}")
        await message.answer(f"❌ Не удалось получить баланс звёзд: {e}", reply_markup=main_keyboard(True))

# ====================== ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ ======================
@router.message(Command("buy"))
async def cmd_buy(message: Message, state: FSMContext):
    await buy_start(message, state)

@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message):
    await cabinet_entry(message)

@router.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    res = await supabase.table("subscriptions").select("server_id, expiry_date, sub_id").eq("user_id", user_id).eq("status", "active").execute()
    if not res.data:
        await message.answer("📭 У вас нет активных подписок.", reply_markup=main_keyboard(is_admin(user_id)))
        return
    servers = await load_servers_from_supabase()
    server_map = {s["id"]: s for s in servers}
    for sub in res.data:
        server = server_map.get(sub["server_id"], {})
        expiry = datetime.fromtimestamp(sub["expiry_date"] / 1000).strftime("%d.%m.%Y %H:%M")
        server_name = server.get("name", "Сервер")
        text = f"🌍 <b>{server_name}</b>\n📅 Действует до: <code>{expiry}</code>\n🆔 <code>{sub['sub_id']}</code>"
        await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("extend"))
async def cmd_extend(message: Message, state: FSMContext):
    user_id = message.from_user.id
    res = await supabase.table("subscriptions").select("sub_id, server_id").eq("user_id", user_id).eq("status", "active").limit(1).execute()
    if not res.data:
        await message.answer("❌ У вас нет активных подписок для продления.", reply_markup=main_keyboard(is_admin(user_id)))
        return
    sub = res.data[0]
    servers = await load_servers_from_supabase()
    server = next((s for s in servers if s["id"] == sub["server_id"]), servers[0])
    await state.update_data(server=server, server_id=server["id"], sub_id=sub["sub_id"])
    await state.set_state(ExtendSubscriptionStates.select_tariff)
    await show_tariffs(message, state)

@router.message(Command("connect"))
async def cmd_connect(message: Message):
    await instructions_os(message)

@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    await support_start(message, state)

@router.message(Command("key"))
async def cmd_key(message: Message, state: FSMContext):
    await activate_key_start(message, state)

@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📋 <b>Доступные команды бота:</b>\n\n"
        "/start — Главное меню\n"
        "/buy — Купить подписку\n"
        "/cabinet — Личный кабинет\n"
        "/status — Статус подписок\n"
        "/extend — Продлить подписку\n"
        "/connect — Инструкции по подключению\n"
        "/support — Обратиться в поддержку\n"
        "/key — Активировать промокод\n"
        "/help — Показать эту справку\n\n"
        "Также доступны кнопки в меню ниже 👇"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(message.from_user.id)))

# ====================== ПРИВЕТСТВИЕ ======================
GREETING_WORDS = {"привет", "hi", "hello", "здравствуй", "добрый день", "доброе утро", "добрый вечер", "хай", "всем привет"}
@router.message(F.text.lower().in_(GREETING_WORDS))
async def greeting_handler(message: Message):
    await cmd_start(message)

# ====================== ПОКУПКА ======================
async def show_servers(message: Message):
    servers = await load_servers_from_supabase()
    if not servers:
        await message.answer("❌ Нет доступных серверов.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s["name"], callback_data=f"server_{s['id']}")] for s in servers
    ])
    await message.answer("🌍 <b>Выберите страну сервера</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

async def show_tariffs(message: Message, state: FSMContext):
    sorted_tariffs = sorted(TARIFFS.values(), key=lambda x: x["months"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 {t['label']} — {t['rub']} ₽", callback_data=f"tariff_{t['months']}")]
        for t in sorted_tariffs
    ])
    await message.answer("📦 <b>Выберите срок подписки</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

async def show_payment_methods(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="method_stars"),
         InlineKeyboardButton(text="₿ Криптовалюта", callback_data="method_crypto")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_tariff")]
    ])
    await message.answer("💵 <b>Выберите способ оплаты</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

async def show_crypto_currencies(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💚 USDT (Arbitrum)", callback_data="crypto_USDT"),
         InlineKeyboardButton(text="💙 USDC (Arbitrum)", callback_data="crypto_USDC")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_method")]
    ])
    await message.answer("🪙 <b>Выберите криптовалюту</b>\n\nСеть: Arbitrum One", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(lambda c: c.data == "back_to_tariff")
async def back_to_tariff_callback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state == BuyStates.select_method:
        await state.set_state(BuyStates.select_tariff)
        await show_tariffs(callback.message, state)
    elif current_state == ExtendSubscriptionStates.select_method:
        await state.set_state(ExtendSubscriptionStates.select_tariff)
        await show_tariffs(callback.message, state)
    await callback.answer()

@router.callback_query(lambda c: c.data == "back_to_method")
async def back_to_method_callback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (BuyStates.select_crypto_currency, ExtendSubscriptionStates.select_crypto_currency):
        new_state = BuyStates.select_method if current_state == BuyStates.select_crypto_currency else ExtendSubscriptionStates.select_method
        await state.set_state(new_state)
        await show_payment_methods(callback.message)
    elif current_state in (BuyStates.select_method, ExtendSubscriptionStates.select_method):
        await show_payment_methods(callback.message)
    await callback.answer()

@router.message(F.text == "🛒 Купить подписку")
async def buy_start(message: Message, state: FSMContext):
    await ensure_user_exists_supabase(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await state.set_state(BuyStates.select_server)
    await show_servers(message)

@router.callback_query(lambda c: c.data.startswith("server_"))
async def server_callback(callback: CallbackQuery, state: FSMContext):
    server_id = int(callback.data.split("_")[1])
    servers = await load_servers_from_supabase()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    await state.update_data(server=server, server_id=server_id)
    await state.set_state(BuyStates.select_tariff)
    await show_tariffs(callback.message, state)
    await callback.answer()

# ====================== ТАРИФЫ ======================
TARIFFS: Dict[float, dict] = {}

async def load_tariffs():
    await price_manager.update_rates()
    await init_supabase()
    res = await supabase.table("tariffs").select("*").order("months").execute()
    global TARIFFS
    TARIFFS = {}
    for t in res.data:
        m = t["months"]
        r = t["rub"]
        usd = round(r / price_manager.usd_effective, 2)
        stars = await price_manager.get_stars_price(r)
        if m == 0.033:
            label = "1 день"
        elif m < 1:
            label = f"{int(m * 30)} дней"
        elif m == 1:
            label = "1 месяц"
        elif m in (2, 3, 4):
            label = f"{int(m)} месяца"
        elif m == 12:
            label = "1 год"
        else:
            label = f"{int(m)} месяцев"
        TARIFFS[m] = {"months": m, "rub": r, "usd": usd, "stars": stars, "label": label}
    logger.info(f"✅ Загружено {len(TARIFFS)} тарифов из Supabase.")

@router.callback_query(lambda c: c.data.startswith("tariff_"))
async def tariff_callback(callback: CallbackQuery, state: FSMContext):
    months = float(callback.data.split("_")[1])
    if months not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    tariff = TARIFFS[months]
    current_state = await state.get_state()
    await state.update_data(months=months, rub=tariff["rub"], usd=tariff["usd"], stars=tariff["stars"])
    if current_state == AdminCreateSubStates.waiting_months:
        data = await state.get_data()
        target_user_id = data["target_user_id"]
        await ensure_user_exists_supabase(target_user_id)
        server = (await load_servers_from_supabase())[0]
        result = await create_subscription(target_user_id, server, months, 0, None)
        if result:
            vless, sub = result
            user_res = await supabase.table("users").select("username, full_name").eq("user_id", target_user_id).execute()
            u = user_res.data[0] if user_res.data else {}
            user_identifier = get_user_identifier(target_user_id, u.get("username"), u.get("full_name"))
            config_text = generate_config_for_connection(vless, sub)
            await callback.message.edit_text(
                f"🎉 <b>Подписка успешно создана для пользователя {user_identifier}</b>\n\n{config_text}",
                parse_mode=ParseMode.HTML
            )
            try:
                await bot.send_message(target_user_id, f"🎉 <b>Администратор выдал вам подписку!</b>\n\n{config_text}\n\nСпасибо за доверие!", parse_mode=ParseMode.HTML)
            except:
                pass
        else:
            await callback.message.edit_text("❌ Ошибка создания подписки.")
        await state.clear()
        await callback.answer()
        return
    if current_state == BuyStates.select_tariff:
        await state.set_state(BuyStates.select_method)
    elif current_state == ExtendSubscriptionStates.select_tariff:
        await state.set_state(ExtendSubscriptionStates.select_method)
    else:
        await callback.answer("Ошибка состояния", show_alert=True)
        return
    await show_payment_methods(callback.message)
    await callback.answer()

# ====================== МЕТОДЫ ОПЛАТЫ ======================
@router.callback_query(lambda c: c.data.startswith("method_"))
async def method_callback(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    data = await state.get_data()
    user_id = callback.from_user.id
    await ensure_user_exists_supabase(user_id, callback.from_user.username, callback.from_user.full_name)
    current_state = await state.get_state()
    is_extend = current_state == ExtendSubscriptionStates.select_method
    if method == "stars":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплатить", callback_data="stars_pay_confirm")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_method")]
        ])
        await callback.message.edit_text(
            f"⭐ <b>Оплата Telegram Stars</b>\n\n"
            f"Стоимость: <b>{data['stars']} Stars</b>\n\n"
            f"Нажмите «Оплатить» для создания счёта.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
        await callback.answer()
        return
    elif method == "crypto":
        await state.update_data(method="crypto")
        await state.set_state(ExtendSubscriptionStates.select_crypto_currency if is_extend else BuyStates.select_crypto_currency)
        await show_crypto_currencies(callback.message)
        await callback.answer()
    else:
        await callback.answer("Неизвестный способ оплаты", show_alert=True)

@router.callback_query(lambda c: c.data == "stars_pay_confirm")
async def stars_pay_confirm_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    current_state = await state.get_state()
    is_extend = current_state == ExtendSubscriptionStates.select_method
    stars = data["stars"]
    payment_uid = generate_payment_uid()
    pay_res = await supabase.table("payments").insert({
        "payment_uid": payment_uid,
        "user_id": user_id,
        "amount_rub": data["rub"],
        "method": "stars",
        "status": "pending_stars",
        "created_at": datetime.now().isoformat()
    }).execute()
    payment_id = pay_res.data[0]["id"]
    confirm_type = "extend_stars" if is_extend else "stars"
    await supabase.table("pending_confirmations").upsert({
        "user_id": user_id,
        "payment_id": payment_id,
        "confirm_type": confirm_type,
        "data": json.dumps(data),
        "created_at": datetime.now().isoformat()
    }).execute()
    title = "Продление подписки Gigabyte" if is_extend else "Оплата подписки Gigabyte"
    description = f"Продление на {TARIFFS[data['months']]['label']}. Стоимость: {stars} Stars." if is_extend else f"Подписка на {TARIFFS[data['months']]['label']}. Стоимость: {stars} Stars."
    payload = f"extend_{data['months']}_{data['rub']}" if is_extend else f"sub_{data['months']}_{data['rub']}"
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Подписка" if not is_extend else "Продление", amount=stars)],
        start_parameter="vpn_extend" if is_extend else "vpn_subscription",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ Оплатить", pay=True)]])
    )
    await state.clear()
    await callback.answer()

# ====================== ВЫБОР КРИПТОВАЛЮТЫ ======================
@router.callback_query(lambda c: c.data.startswith("crypto_"))
async def crypto_currency_selected(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1]
    data = await state.get_data()
    user_id = callback.from_user.id
    current_state = await state.get_state()
    is_extend = current_state == ExtendSubscriptionStates.select_crypto_currency

    if "usd" not in data or "rub" not in data:
        await callback.message.edit_text("❌ Ошибка: данные о сумме оплаты не найдены. Попробуйте начать покупку заново.")
        await state.clear()
        await callback.answer()
        return

    payment_uid = generate_payment_uid()
    pay_res = await supabase.table("payments").insert({
        "payment_uid": payment_uid,
        "user_id": user_id,
        "amount_usd": data["usd"],
        "amount_rub": data["rub"],
        "method": "crypto",
        "currency": currency,
        "status": "pending_crypto",
        "created_at": datetime.now().isoformat()
    }).execute()
    payment_id = pay_res.data[0]["id"]
    confirm_type = "extend_crypto" if is_extend else "crypto"
    await supabase.table("pending_confirmations").upsert({
        "user_id": user_id,
        "payment_id": payment_id,
        "confirm_type": confirm_type,
        "data": json.dumps(data),
        "created_at": datetime.now().isoformat()
    }).execute()

    await state.update_data(payment_id=payment_id, crypto_currency=currency)

    contract = USDT_CONTRACT if currency == "USDT" else USDC_CONTRACT
    amount_crypto = data["usd"]
    rub_amount = data["rub"]
    message_text = (
        f"₿ <b>Оплата криптовалютой ({currency})</b>\n\n"
        f"🔹 Сумма к оплате: <b><code>{amount_crypto:.2f}</code> {currency}</b>\n"
        f"🔹 В рублях: ≈ <b>{rub_amount:.0f} ₽</b>\n\n"
        f"🌐 Сеть: <b>Arbitrum One</b>\n"
        f"📄 Контракт токена:\n<code>{contract}</code>\n\n"
        f"👛 Кошелёк получателя:\n<code>{ARBITRUM_WALLET}</code>\n\n"
        f"<i>После перевода нажмите «✅ Я оплатил» и пришлите TXID.</i>"
    )
    qr_file = generate_wallet_qr(ARBITRUM_WALLET, amount_crypto, currency)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"pay_confirm_{payment_id}"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_crypto_payment")]
    ])
    await callback.message.delete()
    await callback.message.answer_photo(
        photo=qr_file,
        caption=message_text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

    if is_extend:
        await state.set_state(ExtendSubscriptionStates.waiting_crypto_payment)
    else:
        await state.set_state(BuyStates.waiting_crypto_payment)
    await callback.answer()

@router.callback_query(lambda c: c.data == "cancel_crypto_payment")
async def cancel_crypto_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payment_id = data.get("payment_id")
    if payment_id:
        await supabase.table("payments").delete().eq("id", payment_id).execute()
        await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ Оплата отменена.", reply_markup=main_keyboard(is_admin(callback.from_user.id)))
    await callback.answer()

# ====================== ОБРАБОТЧИКИ КНОПОК "Я ОПЛАТИЛ" ======================
@router.callback_query(lambda c: c.data.startswith("pay_confirm_"))
async def pay_confirm(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split("_")[2])
    res = await supabase.table("payments").select("user_id, status").eq("id", payment_id).execute()
    if not res.data or res.data[0]["status"] != "pending_crypto":
        await callback.answer("Платёж уже обработан или не найден.", show_alert=True)
        return
    await supabase.table("payments").update({"status": "awaiting_hash"}).eq("id", payment_id).execute()
    confirm_row = await supabase.table("pending_confirmations").select("confirm_type, data").eq("payment_id", payment_id).execute()
    if not confirm_row.data:
        await callback.answer("Ошибка: данные не найдены.", show_alert=True)
        return
    confirm_type = confirm_row.data[0]["confirm_type"]
    data = json.loads(confirm_row.data[0]["data"])

    if confirm_type == "extend_crypto":
        await state.set_state(ExtendSubscriptionStates.wait_crypto_hash)
    else:
        await state.set_state(BuyStates.wait_crypto_hash)
    await state.update_data(data, payment_id=payment_id, crypto_currency=data.get("crypto_currency", "USDT"))

    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
    await callback.message.answer(
        "📎 <b>Отправьте TXID (хеш транзакции)</b>\n\n"
        "📍 <b>Где взять TXID?</b>\n"
        "• В вашем криптокошельке после отправки транзакции\n"
        "• В обозревателе Arbitrum (arbiscan.io) по вашему адресу\n\n"
        "📝 <b>Пример хеша:</b>\n"
        "<code>0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb4a1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0</code>\n\n"
        "⚠️ <b>Важно:</b> Отправьте именно TXID транзакции, которой вы отправили средства на наш кошелёк.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    await callback.message.delete()
    await callback.answer()

# ====================== ОБРАБОТКА TXID ======================
@router.message(BuyStates.wait_crypto_hash, F.text)
@router.message(ExtendSubscriptionStates.wait_crypto_hash, F.text)
async def process_crypto_hash(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        data = await state.get_data()
        payment_id = data.get("payment_id")
        if payment_id:
            await supabase.table("payments").delete().eq("id", payment_id).execute()
            await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
        await state.clear()
        await message.answer("❌ Оплата отменена.", reply_markup=main_keyboard(is_admin(message.from_user.id)))
        return

    tx_hash = message.text.strip()
    if len(tx_hash) < 64 or not tx_hash.startswith("0x"):
        await message.answer(
            "❌ <b>Неверный формат TXID</b>\n\n"
            "Хеш транзакции должен:\n"
            "• Начинаться с <code>0x</code>\n"
            "• Содержать 66 символов\n\n"
            "Пожалуйста, проверьте и отправьте снова.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
        )
        return

    data = await state.get_data()
    user_id = message.from_user.id
    payment_id = data.get("payment_id")

    pay_res = await supabase.table("payments").select("status").eq("id", payment_id).execute()
    if not pay_res.data or pay_res.data[0]["status"] != "awaiting_hash":
        await message.answer("❌ Этот платёж уже обработан или не ожидает хеша.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return

    waiting_msg = await message.answer("⏳ Проверяем транзакцию... Пожалуйста, подождите.")
    success, reason = await verify_arbitrum_tx(tx_hash, data["crypto_currency"], data["usd"])
    await waiting_msg.delete()

    if not success:
        await supabase.table("payments").update({"status": "pending_crypto"}).eq("id", payment_id).execute()
        await message.answer(
            f"❌ {reason}\n\n"
            "Проверьте корректность TXID и попробуйте снова.\n"
            "Если проблема повторяется, обратитесь в поддержку.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(is_admin(user_id))
        )
        return

    dup = await supabase.table("payments").select("id").eq("tx_hash", tx_hash).execute()
    if dup.data:
        await message.answer("❌ Этот TXID уже использован для другого платежа.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return

    await supabase.table("payments").update({"tx_hash": tx_hash, "status": "confirmed"}).eq("id", payment_id).execute()

    if await state.get_state() == ExtendSubscriptionStates.wait_crypto_hash:
        sub_id = data.get("sub_id")
        months = data["months"]
        days = int(months * 30)
        await supabase.table("subscriptions").update({"expiry_date": supabase.raw(f"expiry_date + {days * 24 * 3600 * 1000}")}).eq("sub_id", sub_id).execute()
        await supabase.table("payments").update({"status": "completed"}).eq("id", payment_id).execute()
        await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
        await message.answer(
            "✅ <b>Подписка успешно продлена!</b>\n\n"
            "Ваша защита активна. Приятного использования! 🚀",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(is_admin(user_id))
        )
    else:
        result = await create_subscription(user_id, data["server"], data["months"], data["rub"], payment_id)
        if result:
            vless, sub = result
            config_text = generate_config_for_connection(vless, sub)
            await message.answer(
                "🎉 <b>Оплата успешно подтверждена!</b>\n\n"
                f"{config_text}\n\n"
                "<i>Спасибо, что выбрали Gigabyte. Надёжная защита гарантирована.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(is_admin(user_id))
            )
        else:
            # Уведомление админу о сбое
            user_info = f"ID: <code>{user_id}</code>\nUsername: {get_user_identifier(user_id, message.from_user.username, message.from_user.full_name)}"
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"🚨 <b>СБОЙ СОЗДАНИЯ ПОДПИСКИ ПОСЛЕ ОПЛАТЫ</b>\n\n"
                        f"Пользователь: {user_info}\n"
                        f"Сумма: {data['rub']} ₽ ({data['usd']} {data.get('crypto_currency', 'USDT')})\n"
                        f"Метод: Криптовалюта\n"
                        f"Транзакция: <code>{tx_hash}</code>\n\n"
                        f"Не удалось добавить клиента в панель 3x-ui. Проверьте логи сервера.",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
            await message.answer("❌ Ошибка создания подписки. Администратор уведомлён. Мы свяжемся с вами в ближайшее время.", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(user_id)))
    await state.clear()

@router.message(BuyStates.wait_crypto_hash)
@router.message(ExtendSubscriptionStates.wait_crypto_hash)
async def wait_crypto_hash_fallback(message: Message, state: FSMContext):
    await process_crypto_hash(message, state)

# ====================== TELEGRAM STARS ======================
@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user_exists_supabase(user_id, message.from_user.username, message.from_user.full_name)
    conf_res = await supabase.table("pending_confirmations").select("*").eq("user_id", user_id).in_("confirm_type", ["stars", "extend_stars"]).order("created_at", desc=True).limit(1).execute()
    if not conf_res.data:
        await message.answer("❌ Не найден ожидающий платёж.", reply_markup=main_keyboard(is_admin(user_id)))
        return
    row = conf_res.data[0]
    payment_id = row["payment_id"]
    data = json.loads(row["data"])
    is_extend = row["confirm_type"] == "extend_stars"

    pay_res = await supabase.table("payments").select("status").eq("id", payment_id).execute()
    if pay_res.data and pay_res.data[0]["status"] == "completed":
        await state.clear()
        return

    if is_extend:
        months = data["months"]
        days = int(months * 30)
        await supabase.table("payments").update({"status": "completed", "tx_hash": message.successful_payment.provider_payment_charge_id}).eq("id", payment_id).execute()
        await supabase.table("subscriptions").update({"expiry_date": supabase.raw(f"expiry_date + {days * 24 * 3600 * 1000}")}).eq("sub_id", data.get("sub_id")).execute()
        await supabase.table("pending_confirmations").delete().eq("user_id", user_id).execute()
        await message.answer("✅ <b>Подписка успешно продлена!</b>\n\nВаша защита активна. Приятного использования! 🚀", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(user_id)))
    else:
        result = await create_subscription(user_id, data["server"], data["months"], data["rub"], payment_id)
        if result:
            vless, sub = result
            config_text = generate_config_for_connection(vless, sub)
            await supabase.table("payments").update({"status": "completed", "tx_hash": message.successful_payment.provider_payment_charge_id}).eq("id", payment_id).execute()
            await supabase.table("pending_confirmations").delete().eq("user_id", user_id).execute()
            await message.answer(
                "🎉 <b>Оплата Telegram Stars подтверждена!</b>\n\n"
                f"{config_text}\n\n"
                "<i>Спасибо, что выбрали Gigabyte!</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(is_admin(user_id))
            )
        else:
            # Уведомление админу о сбое
            user_info = f"ID: <code>{user_id}</code>\nUsername: {get_user_identifier(user_id, message.from_user.username, message.from_user.full_name)}"
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"🚨 <b>СБОЙ СОЗДАНИЯ ПОДПИСКИ ПОСЛЕ ОПЛАТЫ STARS</b>\n\n"
                        f"Пользователь: {user_info}\n"
                        f"Сумма: {data['rub']} ₽ ({data['stars']} Stars)\n"
                        f"Метод: Telegram Stars\n"
                        f"ID транзакции: <code>{message.successful_payment.provider_payment_charge_id}</code>\n\n"
                        f"Не удалось добавить клиента в панель 3x-ui. Проверьте логи сервера.",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
            await message.answer("❌ Ошибка создания подписки. Администратор уведомлён. Мы свяжемся с вами в ближайшее время.", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(user_id)))
    await state.clear()

# ====================== АКТИВАЦИЯ КЛЮЧА ======================
@router.message(F.text == "🎫 Активировать ключ")
async def activate_key_start(message: Message, state: FSMContext):
    await state.set_state(ActivateKeyStates.waiting_code)
    await message.answer(
        "🔑 <b>Активация промокода</b>\n\n"
        "Введите промокод в формате:\n"
        "<code>GIFT-ABCD1234EFGH5678</code>\n\n"
        "Промокод можно получить у администратора или в рамках акций.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(is_admin(message.from_user.id))
    )

@router.message(ActivateKeyStates.waiting_code)
async def process_activate_key(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    await ensure_user_exists_supabase(user_id, message.from_user.username, message.from_user.full_name)
    key_res = await supabase.table("promo_keys").select("months, used").eq("code", code).execute()
    if not key_res.data:
        await message.answer("❌ Неверный или несуществующий ключ.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return
    months, used = key_res.data[0]["months"], key_res.data[0]["used"]
    if used:
        await message.answer("❌ Этот ключ уже был использован.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return
    await supabase.table("promo_keys").update({"used": True, "used_by": user_id, "used_at": datetime.now().isoformat()}).eq("code", code).execute()
    servers = await load_servers_from_supabase()
    server = servers[0] if servers else None
    if not server:
        await message.answer("❌ Нет доступных серверов.")
        await state.clear()
        return
    result = await create_subscription(user_id, server, months, 0, None)
    if result:
        vless, sub = result
        config_text = generate_config_for_connection(vless, sub)
        await message.answer(
            f"🎉 <b>Ключ успешно активирован!</b>\n\n"
            f"{config_text}\n\n"
            f"<i>Срок действия: {'бессрочно' if months == -1 else TARIFFS[months]['label']}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(is_admin(user_id))
        )
    else:
        await message.answer("❌ Ошибка активации ключа. Обратитесь в поддержку.", reply_markup=main_keyboard(is_admin(user_id)))
    await state.clear()

# ====================== ГЕНЕРАЦИЯ КЛЮЧЕЙ АДМИНОМ ======================
@router.message(F.text == "🎫 Сгенерировать ключ")
async def admin_generate_key_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminGenerateKeyStates.waiting_months)
    await message.answer("🎫 <b>Выберите срок действия ключа</b>", parse_mode=ParseMode.HTML, reply_markup=promo_months_keyboard())

@router.callback_query(lambda c: c.data.startswith("promo_"))
async def admin_generate_key_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    action = callback.data.split("_")[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("✅ Генерация ключа отменена.")
        await callback.answer()
        return
    months = -1 if action == "unlimited" else float(action)
    label = "бессрочно" if months == -1 else TARIFFS[months]['label']
    code = generate_promo_code()
    await supabase.table("promo_keys").insert({
        "code": code,
        "months": months,
        "created_by": callback.from_user.id
    }).execute()
    await callback.message.edit_text(
        f"✅ <b>Ключ сгенерирован</b>\n\n"
        f"📝 <b>Код:</b> <code>{code}</code>\n"
        f"📅 <b>Срок:</b> {label}\n\n"
        f"Отправьте этот код пользователю.",
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await callback.answer()

@router.message(F.text == "📋 Список ключей")
async def admin_list_keys(message: Message):
    if not is_admin(message.from_user.id):
        return
    res = await supabase.table("promo_keys").select("code, months, used, used_by, created_at, users(username, full_name)").order("created_at", desc=True).limit(20).execute()
    if not res.data:
        await message.answer("Нет сгенерированных ключей.", reply_markup=main_keyboard(True))
        return
    text = "🔑 <b>Последние ключи:</b>\n\n"
    for k in res.data:
        used_by = k.get("used_by")
        user = k.get("users", {})
        if k["used"] and used_by:
            user_identifier = get_user_identifier(used_by, user.get("username"), user.get("full_name"))
            status = f"✅ Использован (пользователь {user_identifier})"
        elif k["used"]:
            status = "✅ Использован"
        else:
            status = "🟢 Активен"
        months = k["months"]
        if months == -1:
            months_str = "бессрочно"
        elif months in TARIFFS:
            months_str = TARIFFS[months]['label']
        else:
            months_str = f"{months} мес"
        text += f"📌 <code>{k['code']}</code> | {months_str} | {status}\n"
    await message.answer(text[:4000], parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True))

# ====================== ЛИЧНЫЙ КАБИНЕТ ======================
async def cabinet_entry(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активные подписки", callback_data="cabinet_active")],
        [InlineKeyboardButton(text="⏳ Ожидающие платежи", callback_data="cabinet_pending"),
         InlineKeyboardButton(text="📜 История платежей", callback_data="cabinet_history")]
    ])
    await message.answer("👤 <b>Личный кабинет</b>\n\nВыберите раздел:", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.message(F.text == "👤 Личный кабинет")
async def cabinet(message: Message):
    await cabinet_entry(message)

@router.callback_query(lambda c: c.data.startswith("cabinet_"))
async def cabinet_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    section = callback.data.split("_")[1]
    if section == "active":
        subs = await supabase.table("subscriptions").select("server_id, expiry_date, sub_id").eq("user_id", user_id).eq("status", "active").execute()
        if not subs.data:
            await callback.message.edit_text("📭 У вас пока нет активных подписок.")
            await callback.answer()
            return
        await callback.message.delete()
        servers = await load_servers_from_supabase()
        server_map = {s["id"]: s for s in servers}
        for sub in subs.data:
            server = server_map.get(sub["server_id"], {})
            expiry = datetime.fromtimestamp(sub["expiry_date"] / 1000).strftime("%d.%m.%Y %H:%M")
            server_name = server.get("name", "Сервер")
            text = f"🌍 <b>{server_name}</b>\n📅 Действует до: <code>{expiry}</code>\n🆔 <code>{sub['sub_id']}</code>\n\n📡 <b>Ссылка на подписку:</b>\n<code>{generate_subscription_link(server, sub['sub_id'])}</code>"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data=f"extend_{sub['sub_id']}")]
            ])
            await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        await callback.answer()
    elif section == "pending":
        pending = await supabase.table("payments").select("id, payment_uid, amount_rub, currency, method, created_at, status").eq("user_id", user_id).in_("status", ["pending_crypto", "awaiting_hash"]).execute()
        if not pending.data:
            await callback.message.edit_text("⏳ У вас нет ожидающих платежей.")
            await callback.answer()
            return
        await callback.message.delete()
        for p in pending.data:
            dt = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y %H:%M")
            status_display = "⏳ Ожидает TXID" if p["status"] == "awaiting_hash" else "💰 Ожидает оплаты"
            text = f"💸 <code>{p['payment_uid']}</code>\n💰 {p['amount_rub']} ₽ • {p['method'].upper()}\n📅 {dt}\n📊 Статус: {status_display}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📎 Отправить хеш", callback_data=f"resend_hash_{p['id']}"),
                 InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_payment_{p['id']}")]
            ])
            await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        await callback.answer()
    elif section == "history":
        hist = await supabase.table("payments").select("payment_uid, amount_rub, amount_usd, method, currency, status, created_at").eq("user_id", user_id).eq("status", "completed").order("created_at", desc=True).execute()
        if not hist.data:
            await callback.message.edit_text("📭 История платежей пуста.")
            await callback.answer()
            return
        await callback.message.delete()
        status_map = {"completed": "✅ Завершён"}
        method_map = {"crypto": "₿ Криптовалюта", "stars": "⭐ Telegram Stars"}
        for p in hist.data:
            dt_str = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y %H:%M")
            text = f"<b>🧾 <code>{p['payment_uid']}</code></b>\n"
            if p["method"] == "crypto" and p["currency"] and p["amount_usd"]:
                text += f"💰 Сумма: <b>{p['amount_usd']} {p['currency']}</b>\n"
                text += f"💵 Эквивалент: ≈ {p['amount_rub']:.0f} ₽\n"
            else:
                text += f"💰 Сумма: <b>{p['amount_rub']:.0f} ₽</b>\n"
            text += f"💳 Способ: {method_map.get(p['method'], p['method'].upper())}\n"
            text += f"📅 Дата: {dt_str}\n"
            text += f"📊 Статус: {status_map.get(p['status'], p['status'])}"
            await callback.message.answer(text, parse_mode=ParseMode.HTML)
        await callback.answer()

# ====================== ПОВТОРНАЯ ОТПРАВКА ХЕША ======================
@router.callback_query(lambda c: c.data.startswith("resend_hash_"))
async def resend_hash_callback(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split("_")[2])
    pay = await supabase.table("payments").select("user_id, status").eq("id", payment_id).execute()
    if not pay.data or pay.data[0]["status"] not in ("pending_crypto", "awaiting_hash"):
        await callback.answer("Этот платёж не требует отправки хеша.", show_alert=True)
        return
    if pay.data[0]["user_id"] != callback.from_user.id:
        await callback.answer("Это не ваш платёж.", show_alert=True)
        return
    await state.update_data(payment_id=payment_id)
    await state.set_state(ResendHashState.waiting_hash)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
    await callback.message.answer(
        "📎 <b>Отправьте TXID (хеш транзакции)</b>\n\n"
        "📍 <b>Где взять TXID?</b>\n"
        "• В вашем криптокошельке после отправки транзакции\n"
        "• В обозревателе Arbitrum (arbiscan.io) по вашему адресу\n\n"
        "📝 <b>Пример хеша:</b>\n"
        "<code>0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb4a1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    await callback.answer()

@router.message(ResendHashState.waiting_hash)
async def process_resend_hash(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await universal_cancel(message, state)
        return
    tx_hash = message.text.strip()
    if len(tx_hash) < 64 or not tx_hash.startswith("0x"):
        await message.answer(
            "❌ <b>Неверный формат TXID</b>\n\n"
            "Хеш транзакции должен начинаться с 0x и содержать 66 символов.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
        )
        return
    data = await state.get_data()
    payment_id = data.get("payment_id")
    user_id = message.from_user.id
    pay = await supabase.table("payments").select("amount_usd, currency, status").eq("id", payment_id).eq("user_id", user_id).execute()
    if not pay.data or pay.data[0]["status"] not in ("pending_crypto", "awaiting_hash"):
        await message.answer("❌ Платёж уже обработан.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return
    expected_usd = pay.data[0]["amount_usd"]
    currency = pay.data[0]["currency"]
    await supabase.table("payments").update({"status": "awaiting_hash"}).eq("id", payment_id).execute()
    waiting_msg = await message.answer("⏳ Проверяем транзакцию...")
    success, reason = await verify_arbitrum_tx(tx_hash, currency, expected_usd)
    await waiting_msg.delete()
    if not success:
        await supabase.table("payments").update({"status": "pending_crypto"}).eq("id", payment_id).execute()
        await message.answer(f"❌ {reason}\n\nПопробуйте снова.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return
    dup = await supabase.table("payments").select("id").eq("tx_hash", tx_hash).execute()
    if dup.data:
        await message.answer("❌ Этот TXID уже использован.", reply_markup=main_keyboard(is_admin(user_id)))
        await state.clear()
        return
    await supabase.table("payments").update({"tx_hash": tx_hash, "status": "confirmed"}).eq("id", payment_id).execute()
    conf = await supabase.table("pending_confirmations").select("confirm_type, data").eq("payment_id", payment_id).execute()
    if conf.data:
        confirm_type = conf.data[0]["confirm_type"]
        pay_data = json.loads(conf.data[0]["data"])
        if confirm_type == "extend_crypto":
            sub_id = pay_data.get("sub_id")
            months = pay_data["months"]
            days = int(months * 30)
            await supabase.table("subscriptions").update({"expiry_date": supabase.raw(f"expiry_date + {days * 24 * 3600 * 1000}")}).eq("sub_id", sub_id).execute()
            await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
            await message.answer("✅ <b>Подписка успешно продлена!</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(user_id)))
        else:
            result = await create_subscription(user_id, pay_data["server"], pay_data["months"], pay_data["rub"], payment_id)
            if result:
                vless, sub = result
                config_text = generate_config_for_connection(vless, sub)
                await message.answer(
                    "🎉 <b>Оплата успешно подтверждена!</b>\n\n"
                    f"{config_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(is_admin(user_id))
                )
            else:
                await message.answer("❌ Ошибка создания подписки.", reply_markup=main_keyboard(is_admin(user_id)))
    else:
        await supabase.table("payments").update({"status": "completed"}).eq("id", payment_id).execute()
        await message.answer("✅ Платёж подтверждён, но подписка не была создана автоматически. Обратитесь в поддержку.", reply_markup=main_keyboard(is_admin(user_id)))
    await state.clear()

@router.callback_query(lambda c: c.data.startswith("delete_payment_"))
async def delete_payment_callback(callback: CallbackQuery):
    payment_id = int(callback.data.split("_")[2])
    await supabase.table("payments").delete().eq("id", payment_id).execute()
    await supabase.table("pending_confirmations").delete().eq("payment_id", payment_id).execute()
    await callback.message.edit_text("✅ Платёж удалён.")
    await callback.answer()

# ====================== ПРОДЛЕНИЕ ======================
@router.callback_query(lambda c: c.data.startswith("extend_"))
async def extend_subscription(callback: CallbackQuery, state: FSMContext):
    sub_id = callback.data.split("_")[1]
    sub_res = await supabase.table("subscriptions").select("user_id, server_id").eq("sub_id", sub_id).eq("status", "active").execute()
    if not sub_res.data or sub_res.data[0]["user_id"] != callback.from_user.id:
        await callback.answer("Подписка не найдена", show_alert=True)
        return
    server_id = sub_res.data[0]["server_id"]
    servers = await load_servers_from_supabase()
    server = next((s for s in servers if s["id"] == server_id), servers[0])
    await state.update_data(server=server, server_id=server_id, sub_id=sub_id)
    await state.set_state(ExtendSubscriptionStates.select_tariff)
    await show_tariffs(callback.message, state)
    await callback.answer()

# ====================== ИНСТРУКЦИИ С ИЗОБРАЖЕНИЯМИ ======================
@router.message(F.text == "📱 Как подключиться")
async def instructions_os(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Android", callback_data="os_android"), InlineKeyboardButton(text="🍏 iOS", callback_data="os_ios")],
        [InlineKeyboardButton(text="💻 Windows", callback_data="os_windows"), InlineKeyboardButton(text="🍎 Mac", callback_data="os_mac")]
    ])
    await message.answer("📱 <b>Выберите вашу операционную систему</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(lambda c: c.data.startswith("os_"))
async def os_instructions_callback(callback: CallbackQuery):
    os_type = callback.data.split("_")[1]
    texts = {
        "android": (
            "📱 <b>Подключение на Android</b>\n\n"
            "1️⃣ <b>Установите приложение</b>\n"
            "   • Скачайте <code>Nekobox</code> или <code>v2rayNG</code> из Google Play\n"
            "   • Или скачайте с официального сайта\n\n"
            "2️⃣ <b>Импортируйте конфигурацию</b>\n"
            "   • Скопируйте VLESS-ссылку из личного кабинета\n"
            "   • Откройте приложение → Нажмите «+» → «Импорт из буфера»\n\n"
            "3️⃣ <b>Подключитесь</b>\n"
            "   • Выберите добавленный сервер\n"
            "   • Нажмите на кнопку подключения (треугольник/зонтик)\n"
            "   • При запросе разрешения VPN нажмите «ОК»\n\n"
            "✅ <b>Готово!</b> Вы защищены."
        ),
        "ios": (
            "🍏 <b>Подключение на iOS</b>\n\n"
            "1️⃣ <b>Установите приложение</b>\n"
            "   • Скачайте <code>Shadowrocket</code> (платное) или <code>Streisand</code> (бесплатное)\n"
            "   • Из App Store\n\n"
            "2️⃣ <b>Импортируйте конфигурацию</b>\n"
            "   • Скопируйте VLESS-ссылку из личного кабинета\n"
            "   • Откройте приложение → Нажмите «+» → «Импорт из буфера»\n\n"
            "3️⃣ <b>Подключитесь</b>\n"
            "   • Нажмите на переключатель подключения\n"
            "   • При первом запуске разрешите добавление VPN-конфигурации\n\n"
            "✅ <b>Готово!</b> Ваш трафик зашифрован."
        ),
        "windows": (
            "💻 <b>Подключение на Windows</b>\n\n"
            "1️⃣ <b>Установите v2rayN</b>\n"
            "   • Скачайте с официального GitHub: github.com/2dust/v2rayN\n"
            "   • Распакуйте архив и запустите v2rayN.exe\n\n"
            "2️⃣ <b>Импортируйте конфигурацию</b>\n"
            "   • Скопируйте VLESS-ссылку из личного кабинета\n"
            "   • В v2rayN: Серверы → Импорт из буфера обмена\n\n"
            "3️⃣ <b>Настройте и подключитесь</b>\n"
            "   • Убедитесь, что выбран режим «Системный прокси»\n"
            "   • Нажмите «Enter» на сервере или кнопку подключения\n\n"
            "✅ <b>Готово!</b> Включен безопасный доступ."
        ),
        "mac": (
            "🍎 <b>Подключение на Mac</b>\n\n"
            "1️⃣ <b>Установите V2RayX или Nekoray</b>\n"
            "   • V2RayX: github.com/Cenmrev/V2RayX\n"
            "   • Nekoray: github.com/MatsuriDayo/nekoray\n\n"
            "2️⃣ <b>Импортируйте конфигурацию</b>\n"
            "   • Скопируйте VLESS-ссылку из личного кабинета\n"
            "   • В приложении: Нажмите «Import» → «Import from clipboard»\n\n"
            "3️⃣ <b>Подключитесь</b>\n"
            "   • Выберите добавленный сервер\n"
            "   • Включите переключатель «System Proxy»\n\n"
            "✅ <b>Готово!</b> Безопасный доступ активирован."
        )
    }

    caption = texts.get(os_type, "Инструкция в разработке")

    # Собираем все существующие изображения для данной ОС
    media_files = []
    base_path = f"instructions/{os_type}"
    # Основной файл (например, instructions/android.jpg)
    if os.path.exists(f"{base_path}.jpg"):
        media_files.append(f"{base_path}.jpg")
    # Дополнительные файлы с индексами (например, instructions/android_1.jpg, android_2.jpg, ...)
    idx = 1
    while os.path.exists(f"{base_path}_{idx}.jpg"):
        media_files.append(f"{base_path}_{idx}.jpg")
        idx += 1

    if not media_files:
        # Если нет ни одного изображения, отправляем только текст
        await callback.message.answer(caption, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    if len(media_files) == 1:
        # Одно изображение – отправляем как фото с подписью
        await callback.message.answer_photo(
            FSInputFile(media_files[0]),
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    else:
        # Несколько изображений – формируем медиагруппу
        media_group = []
        for i, file_path in enumerate(media_files):
            if i == 0:
                media_group.append(
                    InputMediaPhoto(media=FSInputFile(file_path), caption=caption, parse_mode=ParseMode.HTML)
                )
            else:
                media_group.append(InputMediaPhoto(media=FSInputFile(file_path)))
        await callback.message.answer_media_group(media=media_group)

    await callback.answer()

# ====================== ПОДДЕРЖКА (ТИКЕТЫ) ======================
@router.message(F.text == "❓ Поддержка")
async def support_start(message: Message, state: FSMContext):
    await ensure_user_exists_supabase(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await state.set_state(TicketStates.waiting_question)
    await message.answer("✍️ <b>Опишите вашу проблему</b>\n\nМы ответим в ближайшее время.", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(is_admin(message.from_user.id)))

@router.message(TicketStates.waiting_question)
async def save_ticket(message: Message, state: FSMContext):
    ticket_id = generate_ticket_id()
    user_id = message.from_user.id
    question = message.text
    created_at = datetime.now().isoformat()
    await supabase.table("tickets").insert({
        "user_id": user_id,
        "status": "open",
        "created_at": created_at,
        "ticket_id": ticket_id
    }).execute()
    await supabase.table("ticket_messages").insert({
        "ticket_id": ticket_id,
        "sender_id": user_id,
        "message_text": question,
        "created_at": created_at,
        "is_admin": False
    }).execute()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"ticket_reply_{ticket_id}"),
         InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"ticket_close_{ticket_id}")]
    ])
    await message.answer(f"✅ <b>Тикет <code>{ticket_id}</code> создан</b>\n\nВы можете отправить дополнительные сообщения или закрыть тикет.", parse_mode=ParseMode.HTML, reply_markup=kb)
    user_display = get_user_display(message.from_user)
    for admin_id in ADMIN_IDS:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"ticket_reply_{ticket_id}"),
             InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"ticket_close_{ticket_id}")]
        ])
        await bot.send_message(admin_id, f"🆕 Новый тикет <code>{ticket_id}</code>\nОт: {user_display}\n\n{question}", parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    await state.clear()

@router.callback_query(lambda c: c.data.startswith("ticket_reply_"))
async def ticket_reply_callback(callback: CallbackQuery, state: FSMContext):
    ticket_id = callback.data.split("_")[2]
    ticket = await supabase.table("tickets").select("status").eq("ticket_id", ticket_id).execute()
    if not ticket.data or ticket.data[0]["status"] != "open":
        await callback.answer("Тикет уже закрыт.", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(TicketStates.waiting_reply)
    await callback.message.answer("✏️ Введите ваш ответ:")
    await callback.answer()

@router.message(TicketStates.waiting_reply)
async def process_ticket_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        await message.answer("Ошибка: тикет не найден.")
        await state.clear()
        return

    sender_id = message.from_user.id
    is_admin_sender = is_admin(sender_id)
    reply_text = message.text
    created_at = datetime.now().isoformat()
    await supabase.table("ticket_messages").insert({
        "ticket_id": ticket_id,
        "sender_id": sender_id,
        "message_text": reply_text,
        "created_at": created_at,
        "is_admin": is_admin_sender
    }).execute()
    ticket = await supabase.table("tickets").select("user_id").eq("ticket_id", ticket_id).execute()
    if not ticket.data:
        await message.answer("Ошибка: тикет не найден.")
        await state.clear()
        return
    user_id = ticket.data[0]["user_id"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"ticket_reply_{ticket_id}"),
         InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"ticket_close_{ticket_id}")]
    ])
    user_display = get_user_identifier_by_data(message.from_user.username, message.from_user.full_name, message.from_user.id)

    if is_admin_sender:
        await bot.send_message(user_id, f"📬 <b>Ответ на тикет <code>{ticket_id}</code></b>\n\n{reply_text}", parse_mode=ParseMode.HTML, reply_markup=kb)
        await message.answer("✅ Ваш ответ отправлен пользователю.")
    else:
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, f"📬 <b>Новое сообщение в тикете <code>{ticket_id}</code></b>\nОт: {user_display}\n\n{reply_text}", parse_mode=ParseMode.HTML, reply_markup=kb)
        await message.answer("✅ Ваше сообщение отправлено администраторам.")
    await state.clear()

@router.callback_query(lambda c: c.data.startswith("ticket_close_"))
async def ticket_close_callback(callback: CallbackQuery):
    ticket_id = callback.data.split("_")[2]
    await supabase.table("tickets").update({"status": "closed"}).eq("ticket_id", ticket_id).execute()
    ticket = await supabase.table("tickets").select("user_id").eq("ticket_id", ticket_id).execute()
    if ticket.data:
        user_id = ticket.data[0]["user_id"]
        await bot.send_message(user_id, f"🔒 <b>Тикет <code>{ticket_id}</code> закрыт</b>\n\nСпасибо за обращение!", parse_mode=ParseMode.HTML)
    await callback.message.edit_text(f"✅ <b>Тикет <code>{ticket_id}</code> закрыт</b>", parse_mode=ParseMode.HTML)
    await callback.answer()

# ====================== ЗАПРОС НОВОЙ СТРАНЫ ======================
@router.message(F.text == "🌍 Запросить новую страну")
async def request_country(message: Message, state: FSMContext):
    await ensure_user_exists_supabase(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await state.set_state(CountryRequestStates.waiting_country)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=c, callback_data=f"country_{c}")] for c in COUNTRIES])
    await message.answer("🌏 <b>Выберите страну или напишите свою</b>", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(lambda c: c.data.startswith("country_"))
async def country_callback(callback: CallbackQuery, state: FSMContext):
    country = callback.data.split("_", 1)[1]
    user_id = callback.from_user.id
    request_id = generate_request_id()
    created_at = datetime.now().isoformat()
    await supabase.table("country_requests").insert({
        "user_id": user_id,
        "country": country,
        "status": "open",
        "created_at": created_at,
        "request_id": request_id
    }).execute()
    await callback.message.edit_text("✅ Запрос отправлен администратору. Спасибо!")
    user_display = get_user_display(callback.from_user)
    for admin_id in ADMIN_IDS:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"country_reply_{request_id}")]
        ])
        await bot.send_message(admin_id, f"🌍 <b>Запрос новой страны</b>\nОт: {user_display}\n🌎 Страна: {country}\n🆔 <code>{request_id}</code>", parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    await state.clear()
    await callback.answer()

@router.message(CountryRequestStates.waiting_country)
async def custom_country(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await universal_cancel(message, state)
        return
    country = message.text
    user_id = message.from_user.id
    request_id = generate_request_id()
    created_at = datetime.now().isoformat()
    await supabase.table("country_requests").insert({
        "user_id": user_id,
        "country": country,
        "status": "open",
        "created_at": created_at,
        "request_id": request_id
    }).execute()
    await message.answer("✅ Запрос отправлен! Спасибо.", reply_markup=main_keyboard(is_admin(message.from_user.id)))
    user_display = get_user_display(message.from_user)
    for admin_id in ADMIN_IDS:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"country_reply_{request_id}")]
        ])
        await bot.send_message(admin_id, f"🌍 <b>Запрос новой страны</b>\nОт: {user_display}\n🌎 Страна: {country}\n🆔 <code>{request_id}</code>", parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    await state.clear()

@router.callback_query(lambda c: c.data.startswith("country_reply_"))
async def country_reply_callback(callback: CallbackQuery, state: FSMContext):
    request_id = callback.data.split("_")[2]
    req = await supabase.table("country_requests").select("user_id, status").eq("request_id", request_id).execute()
    if not req.data or req.data[0]["status"] != "open":
        await callback.answer("Запрос уже обработан или не найден.", show_alert=True)
        return
    user_id = req.data[0]["user_id"]
    await state.update_data(request_id=request_id, user_id=user_id)
    await state.set_state(AdminCountryReplyStates.waiting_reply_text)
    await callback.message.answer("✏️ Введите ответ для пользователя:")
    await callback.answer()

@router.message(AdminCountryReplyStates.waiting_reply_text)
async def process_country_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    request_id = data["request_id"]
    user_id = data["user_id"]
    reply_text = message.text
    await supabase.table("country_requests").update({"status": "closed"}).eq("request_id", request_id).execute()
    await bot.send_message(user_id, f"📬 <b>Ответ на запрос новой страны</b>\n\n{reply_text}", parse_mode=ParseMode.HTML)
    await message.answer("✅ Ответ отправлен пользователю.", reply_markup=main_keyboard(True))
    await state.clear()

# ====================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (АДМИН) ======================
@router.message(F.text == "👥 Управление пользователями")
async def admin_users_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = await supabase.table("users").select("user_id, username, full_name, created_at").order("created_at", desc=True).execute()
    if not users.data:
        await message.answer("Нет пользователей.", reply_markup=main_keyboard(True))
        return
    for u in users.data:
        uid = u["user_id"]
        # Активные подписки
        subs = await supabase.table("subscriptions").select("server_id, expiry_date, sub_id").eq("user_id", uid).eq("status", "active").execute()
        sub_count = len(subs.data)
        # Платежи
        payments = await supabase.table("payments").select("amount_rub, method, status, created_at").eq("user_id", uid).order("created_at", desc=True).limit(5).execute()
        total_paid = sum(p["amount_rub"] for p in payments.data if p["status"] == "completed")
        # Дата регистрации
        reg_date = datetime.fromisoformat(u["created_at"]).strftime("%d.%m.%Y") if u.get("created_at") else "неизвестно"
        user_identifier = get_user_identifier(uid, u.get("username"), u.get("full_name"))
        text = (
            f"🆔 <b>Пользователь:</b> {user_identifier}\n"
            f"📌 <b>Telegram ID:</b> <code>{uid}</code>\n"
            f"📅 <b>Регистрация:</b> {reg_date}\n"
            f"📊 <b>Активных подписок:</b> {sub_count}\n"
            f"💰 <b>Всего оплачено:</b> {total_paid:.0f} ₽\n"
        )
        if subs.data:
            servers = await load_servers_from_supabase()
            server_map = {s["id"]: s for s in servers}
            text += "\n<b>📋 Подписки:</b>\n"
            for sub in subs.data[:3]:
                server = server_map.get(sub["server_id"], {})
                server_name = server.get("name", "Сервер")
                expiry = datetime.fromtimestamp(sub["expiry_date"] / 1000).strftime("%d.%m.%Y")
                text += f"  • {server_name} до {expiry} (<code>{sub['sub_id'][:8]}...</code>)\n"
        if payments.data:
            text += "\n<b>💳 Последние платежи:</b>\n"
            for p in payments.data[:3]:
                dt = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y")
                status_icon = "✅" if p["status"] == "completed" else "⏳"
                text += f"  • {dt}: {p['amount_rub']:.0f} ₽ ({p['method']}) {status_icon}\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"delete_user_{uid}")]
        ])
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await message.answer("Показаны все пользователи.", reply_markup=main_keyboard(True))

@router.callback_query(lambda c: c.data.startswith("delete_user_"))
async def delete_user_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split("_")[2])
    subs = await supabase.table("subscriptions").select("client_uuid, server_id").eq("user_id", user_id).execute()
    servers = await load_servers_from_supabase()
    server_map = {s["id"]: s for s in servers}
    deleted_count = 0
    for sub in subs.data:
        server = server_map.get(sub["server_id"])
        if server:
            xui = XUIApi(server)
            if await xui.remove_client(sub["client_uuid"]):
                deleted_count += 1
    await supabase.table("subscriptions").delete().eq("user_id", user_id).execute()
    await supabase.table("payments").delete().eq("user_id", user_id).execute()
    await supabase.table("tickets").delete().eq("user_id", user_id).execute()
    await supabase.table("country_requests").delete().eq("user_id", user_id).execute()
    await supabase.table("users").delete().eq("user_id", user_id).execute()
    await supabase.table("pending_confirmations").delete().eq("user_id", user_id).execute()
    await callback.message.edit_text(f"✅ Пользователь удалён, удалено {deleted_count} подписок в панели.")
    await callback.answer()

# ====================== УДАЛЕНИЕ СЕБЯ ПОЛЬЗОВАТЕЛЕМ ======================
@router.message(F.text == "🗑 Удалить меня")
async def user_delete_self(message: Message):
    user_id = message.from_user.id
    if is_admin(user_id):
        await message.answer("❌ Администратор не может удалить себя через эту кнопку.", reply_markup=main_keyboard(True))
        return

    # Собираем данные пользователя для показа
    user_res = await supabase.table("users").select("*").eq("user_id", user_id).execute()
    if not user_res.data:
        await message.answer("❌ Пользователь не найден.")
        return
    user_info = user_res.data[0]
    subs = await supabase.table("subscriptions").select("*").eq("user_id", user_id).execute()
    payments = await supabase.table("payments").select("amount_rub, status").eq("user_id", user_id).execute()
    total_paid = sum(p["amount_rub"] for p in payments.data if p["status"] == "completed")
    reg_date = datetime.fromisoformat(user_info["created_at"]).strftime("%d.%m.%Y %H:%M") if user_info.get("created_at") else "неизвестно"

    text = (
        "⚠️ <b>ВНИМАНИЕ! Удаление аккаунта</b>\n\n"
        "Вы собираетесь <b>безвозвратно удалить</b> все ваши данные из бота. Это действие <u>нельзя отменить</u>.\n\n"
        "<b>Будут удалены:</b>\n"
        "• Все ваши активные подписки (доступ к VPN прекратится)\n"
        "• История всех платежей\n"
        "• Ваши тикеты и сообщения в поддержку\n"
        "• Все ваши персональные данные\n\n"
        "<b>Ваши данные:</b>\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"👤 Username: {get_user_identifier(user_id, message.from_user.username, message.from_user.full_name)}\n"
        f"📅 Дата регистрации: {reg_date}\n"
        f"📊 Активных подписок: {len(subs.data)}\n"
        f"💰 Всего оплачено: {total_paid:.0f} ₽\n\n"
        "<b>Вы уверены, что хотите удалить аккаунт?</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить навсегда", callback_data="confirm_self_delete"),
         InlineKeyboardButton(text="❌ Нет, отмена", callback_data="cancel_self_delete")]
    ])
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(lambda c: c.data == "confirm_self_delete")
async def confirm_self_delete(callback: CallbackQuery):
    user_id = callback.from_user.id
    if is_admin(user_id):
        await callback.answer("Администратор не может удалить себя.", show_alert=True)
        return

    # Удаляем подписки из панели
    subs = await supabase.table("subscriptions").select("client_uuid, server_id").eq("user_id", user_id).execute()
    servers = await load_servers_from_supabase()
    server_map = {s["id"]: s for s in servers}
    deleted_count = 0
    for sub in subs.data:
        server = server_map.get(sub["server_id"])
        if server:
            xui = XUIApi(server)
            if await xui.remove_client(sub["client_uuid"]):
                deleted_count += 1

    # Удаляем все записи из БД
    await supabase.table("subscriptions").delete().eq("user_id", user_id).execute()
    await supabase.table("payments").delete().eq("user_id", user_id).execute()
    await supabase.table("tickets").delete().eq("user_id", user_id).execute()
    await supabase.table("country_requests").delete().eq("user_id", user_id).execute()
    await supabase.table("pending_confirmations").delete().eq("user_id", user_id).execute()
    await supabase.table("users").delete().eq("user_id", user_id).execute()

    user_identifier = get_user_identifier(user_id, callback.from_user.username, callback.from_user.full_name)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"ℹ️ <b>Пользователь удалил свой аккаунт</b>\n\n"
                f"👤 Пользователь: {user_identifier}\n"
                f"🆔 Telegram ID: <code>{user_id}</code>\n"
                f"📊 Удалено подписок в панели: {deleted_count}",
                parse_mode=ParseMode.HTML
            )
        except:
            pass

    await callback.message.edit_text("✅ Ваш аккаунт и все связанные данные были полностью удалены. До свидания!")
    await callback.answer()

@router.callback_query(lambda c: c.data == "cancel_self_delete")
async def cancel_self_delete(callback: CallbackQuery):
    await callback.message.edit_text("❌ Удаление аккаунта отменено.")
    await callback.message.answer("👋 Главное меню", reply_markup=main_keyboard(is_admin(callback.from_user.id)))
    await callback.answer()

# ====================== АДМИН-ПАНЕЛЬ ======================
@router.message(F.text == "📈 Курс")
async def admin_show_rates(message: Message):
    if not is_admin(message.from_user.id):
        return
    rates_info = await price_manager.get_rates_info()
    await message.answer(rates_info, parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True))

@router.message(F.text == "💰 Изменить цены")
async def admin_edit_prices(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminPriceStates.waiting_action)
    await message.answer("💰 <b>Редактирование цен</b>\n\nВыберите действие:", parse_mode=ParseMode.HTML, reply_markup=price_percent_keyboard())

@router.message(AdminPriceStates.waiting_action)
async def save_new_prices(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "◀️ Назад":
        await universal_cancel(message, state)
        return
    try:
        if message.text.startswith("+"):
            percent = int(message.text[1:].replace("%", ""))
            current = await supabase.table("tariffs").select("months, rub").execute()
            for t in current.data:
                new_rub = round(t["rub"] * (1 + percent / 100))
                await supabase.table("tariffs").update({"rub": new_rub}).eq("months", t["months"]).execute()
        elif message.text == "✏️ Ввести вручную":
            await message.answer(
                "💰 <b>Введите цену за 1 месяц</b>\n\n"
                "Введите одно число — цену в рублях за <b>1 месяц</b>. Остальные тарифы будут рассчитаны автоматически по бизнес-логике:\n"
                "• 1 день = 12.5% от цены месяца\n"
                "• 3 месяца = цена месяца × 2.5\n"
                "• 6 месяцев = цена месяца × 4.5\n"
                "• 1 год = цена месяца × 8.5\n\n"
                "Пример: введите <code>290</code>",
                parse_mode=ParseMode.HTML
            )
            await state.set_state(AdminPriceStates.waiting_manual_input)
            return
        else:
            lines = message.text.strip().splitlines()
            if len(lines) == 1 and ":" not in lines[0]:
                price_1m = float(lines[0])
                tariffs_to_update = [
                    (0.033, round(price_1m * 0.125)),
                    (1, price_1m),
                    (3, round(price_1m * 2.5)),
                    (6, round(price_1m * 4.5)),
                    (12, round(price_1m * 8.5))
                ]
                for months, rub in tariffs_to_update:
                    existing = await supabase.table("tariffs").select("months").eq("months", months).execute()
                    if existing.data:
                        await supabase.table("tariffs").update({"rub": rub}).eq("months", months).execute()
                    else:
                        await supabase.table("tariffs").insert({"months": months, "rub": rub}).execute()
            else:
                for line in lines:
                    if ":" in line:
                        m, r = line.split(":")
                        months = float(m)
                        rub = float(r)
                        existing = await supabase.table("tariffs").select("months").eq("months", months).execute()
                        if existing.data:
                            await supabase.table("tariffs").update({"rub": rub}).eq("months", months).execute()
                        else:
                            await supabase.table("tariffs").insert({"months": months, "rub": rub}).execute()
        await load_tariffs()
        await message.answer("✅ <b>Цены успешно обновлены!</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=price_percent_keyboard())
    await state.clear()

@router.message(AdminPriceStates.waiting_manual_input)
async def manual_prices_input(message: Message, state: FSMContext):
    await save_new_prices(message, state)

@router.message(F.text == "📢 Сделать рассылку")
async def admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminBroadcastStates.waiting_message)
    await message.answer("📢 <b>Создание рассылки</b>\n\nВведите текст сообщения для рассылки всем пользователям:", parse_mode=ParseMode.HTML)

@router.message(AdminBroadcastStates.waiting_message)
async def admin_do_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Отмена":
        await universal_cancel(message, state)
        return
    users = await supabase.table("users").select("user_id").execute()
    count = 0
    for u in users.data:
        try:
            await bot.send_message(u["user_id"], message.text, parse_mode=ParseMode.HTML)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ <b>Рассылка завершена</b>\n\n📨 Отправлено: {count} пользователям", parse_mode=ParseMode.HTML, reply_markup=main_keyboard(True))
    await state.clear()

@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    users_cnt = await supabase.table("users").select("*", count="exact").execute()
    active_cnt = await supabase.table("subscriptions").select("*", count="exact").eq("status", "active").execute()
    rev_res = await supabase.table("payments").select("amount_rub").eq("status", "completed").execute()
    total_rev = sum(p["amount_rub"] for p in rev_res.data) if rev_res.data else 0
    pending_cnt = await supabase.table("payments").select("*", count="exact").in_("status", ["pending_crypto", "awaiting_hash"]).execute()
    tickets_cnt = await supabase.table("tickets").select("*", count="exact").eq("status", "open").execute()
    await message.answer(
        f"📊 <b>Статистика Gigabyte</b>\n\n"
        f"👥 <b>Пользователей:</b> {users_cnt.count}\n"
        f"✅ <b>Активных подписок:</b> {active_cnt.count}\n"
        f"💰 <b>Общая выручка:</b> {total_rev:.0f} ₽\n"
        f"⏳ <b>Ожидающих платежей:</b> {pending_cnt.count}\n"
        f"🎫 <b>Открытых тикетов:</b> {tickets_cnt.count}\n\n"
        f"<i>Актуально на {datetime.now().strftime('%d.%m.%Y %H:%M')}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(True)
    )

@router.message(F.text == "✨ Создать подписку (админ)")
async def admin_create_subscription_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminCreateSubStates.waiting_user_id)
    await message.answer("👤 <b>Создание подписки</b>\n\nВведите Telegram ID пользователя:", parse_mode=ParseMode.HTML)

@router.message(AdminCreateSubStates.waiting_user_id)
async def admin_create_subscription_get_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
    except:
        await message.answer("❌ Неверный user_id. Введите числовой ID.")
        return
    user = await supabase.table("users").select("user_id").eq("user_id", uid).execute()
    if not user.data:
        await message.answer("❌ Пользователь не найден в базе.")
        return
    await state.update_data(target_user_id=uid)
    await show_tariffs(message, state)
    await state.set_state(AdminCreateSubStates.waiting_months)

# ====================== ТИКЕТЫ И ЗАПРОСЫ В ГЛАВНОМ МЕНЮ АДМИНА ======================
@router.message(F.text == "🎫 Тикеты поддержки")
async def admin_tickets_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    tickets = await supabase.table("tickets").select("ticket_id, user_id").eq("status", "open").order("id", desc=True).execute()
    if not tickets.data:
        await message.answer("📭 Нет открытых тикетов.", reply_markup=main_keyboard(True))
        return
    for t in tickets.data:
        ticket_id = t["ticket_id"]
        user_id = t["user_id"]
        msg = await supabase.table("ticket_messages").select("message_text").eq("ticket_id", ticket_id).order("id").limit(1).execute()
        first_msg = msg.data[0]["message_text"] if msg.data else "Нет сообщений"
        user = await supabase.table("users").select("username, full_name").eq("user_id", user_id).execute()
        u = user.data[0] if user.data else {}
        user_identifier = get_user_identifier(user_id, u.get("username"), u.get("full_name"))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"ticket_reply_{ticket_id}"),
             InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"ticket_close_{ticket_id}")]
        ])
        await message.answer(f"🎫 <b><code>{ticket_id}</code></b>\n👤 От: {user_identifier}\n\n📝 {first_msg[:300]}", parse_mode=ParseMode.HTML, reply_markup=kb)
    await message.answer("Все открытые тикеты показаны выше.", reply_markup=main_keyboard(True))

@router.message(F.text == "🌍 Запросы на новую страну")
async def admin_country_requests(message: Message):
    if not is_admin(message.from_user.id):
        return
    reqs = await supabase.table("country_requests").select("request_id, user_id, country").eq("status", "open").order("id", desc=True).execute()
    if not reqs.data:
        await message.answer("🌍 Нет открытых запросов на новые страны.", reply_markup=main_keyboard(True))
        return
    for r in reqs.data:
        user = await supabase.table("users").select("username, full_name").eq("user_id", r["user_id"]).execute()
        u = user.data[0] if user.data else {}
        user_identifier = get_user_identifier(r["user_id"], u.get("username"), u.get("full_name"))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"country_reply_{r['request_id']}")]
        ])
        await message.answer(f"🌍 <b>Запрос новой страны</b>\n👤 От: {user_identifier}\n🌎 Страна: {r['country']}\n🆔 <code>{r['request_id']}</code>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await message.answer("Все открытые запросы показаны выше.", reply_markup=main_keyboard(True))

@router.message(F.text == "🔄 Синхронизировать серверы")
async def cmd_sync_servers(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Запускаю полную синхронизацию клиентов... Это может занять некоторое время.")
    try:
        await sync_all_servers_with_supabase()
        await message.answer("✅ Синхронизация клиентов успешно завершена!")
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации: {e}")
        await message.answer(f"❌ Ошибка синхронизации: {e}")

@router.message(F.text == "📥 Импорт клиентов из панели")
async def cmd_force_import_clients(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Запускаю импорт клиентов из панелей в базу данных...")
    await force_import_clients_from_panel(message)

# ====================== FALLBACK ======================
@router.message()
async def unknown_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in (BuyStates.waiting_crypto_payment, ExtendSubscriptionStates.waiting_crypto_payment):
        await message.answer("Пожалуйста, используйте кнопки «✅ Я оплатил» или «❌ Отмена».")
        return
    if current_state in (BuyStates.wait_crypto_hash, ExtendSubscriptionStates.wait_crypto_hash, ResendHashState.waiting_hash):
        await message.answer("⏳ Ожидание хеша транзакции. Пожалуйста, отправьте TXID или нажмите «❌ Отмена».")
        return
    if await state.get_state() is None:
        await message.answer("❓ Неизвестная команда. Используйте меню.", reply_markup=main_keyboard(is_admin(message.from_user.id)))

@router.errors()
async def error_handler(event: ErrorEvent):
    logger.error(f"❌ Критическая ошибка: {event.exception}", exc_info=True)

# ====================== ЗАПУСК ======================
async def main():
    await init_supabase()
    await load_tariffs()
    asyncio.create_task(daily_backup_task())
    asyncio.create_task(sync_all_servers_periodically())
    logger.info("🚀 Бот Gigabyte запущен и готов к работе (Supabase)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
