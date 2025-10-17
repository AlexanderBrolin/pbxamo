#!/usr/bin/env python3
"""
Полная интеграция FreePBX 17 с AmoCRM
Поддержка: звонки, записи разговоров, webhook
"""

import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import Optional, Dict
import json
import os
from pathlib import Path
from aiohttp import web
import base64

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/freepbx-amocrm.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class Config:
    """Загрузка конфигурации"""
    
    def __init__(self, config_path: str = "/opt/freepbx-amocrm/config.json"):
        with open(config_path, 'r') as f:
            self.data = json.load(f)
    
    def get(self, *keys):
        """Получение значения по пути ключей"""
        value = self.data
        for key in keys:
            value = value.get(key)
            if value is None:
                return None
        return value


class AmoCRMAPI:
    """Расширенный клиент AmoCRM с поддержкой файлов"""
    
    def __init__(self, config: Config):
        self.subdomain = config.get("amocrm", "subdomain")
        self.base_url = f"https://{self.subdomain}.amocrm.ru"
        self.client_id = config.get("amocrm", "client_id")
        self.client_secret = config.get("amocrm", "client_secret")
        self.redirect_uri = config.get("amocrm", "redirect_uri")
        self.access_token = None
        self.refresh_token = None
        self.token_file = "/opt/freepbx-amocrm/tokens.json"
        
    async def load_tokens(self):
        """Загрузка токенов из файла"""
        if os.path.exists(self.token_file):
            with open(self.token_file, 'r') as f:
                tokens = json.load(f)
                self.access_token = tokens.get("access_token")
                self.refresh_token = tokens.get("refresh_token")
                logger.info("Токены загружены из файла")
                
    async def save_tokens(self):
        """Сохранение токенов в файл"""
        with open(self.token_file, 'w') as f:
            json.dump({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "updated_at": datetime.now().isoformat()
            }, f)
        os.chmod(self.token_file, 0o600)
        
    async def get_auth_code_url(self):
        """Получение URL для авторизации"""
        return (f"{self.base_url}/oauth?"
                f"client_id={self.client_id}&"
                f"redirect_uri={self.redirect_uri}&"
                f"mode=post_message&"
                f"state=amocrm_auth")
    
    async def exchange_code(self, code: str):
        """Обмен кода на токены"""
        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri
            }
            
            async with session.post(
                f"{self.base_url}/oauth2/access_token",
                json=data
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Ошибка авторизации: {text}")
                    
                result = await resp.json()
                self.access_token = result["access_token"]
                self.refresh_token = result["refresh_token"]
                await self.save_tokens()
                logger.info("Авторизация AmoCRM успешна")
                
    async def refresh_tokens(self):
        """Обновление токенов"""
        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "redirect_uri": self.redirect_uri
            }
            
            async with session.post(
                f"{self.base_url}/oauth2/access_token",
                json=data
            ) as resp:
                result = await resp.json()
                self.access_token = result["access_token"]
                self.refresh_token = result["refresh_token"]
                await self.save_tokens()
                logger.info("Токены обновлены")
                
    async def api_request(self, method: str, endpoint: str, 
                         data: Dict = None, retry: bool = True):
        """Базовый API запрос с автообновлением токенов"""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.base_url}/api/v4/{endpoint}"
        
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, json=data
            ) as resp:
                if resp.status == 401 and retry:
                    await self.refresh_tokens()
                    return await self.api_request(method, endpoint, data, False)
                    
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(f"API error {resp.status}: {text}")
                    return None
                    
                return await resp.json()
    
    async def find_contact(self, phone: str) -> Optional[Dict]:
        """Поиск контакта по телефону"""
        phone_clean = ''.join(filter(str.isdigit, phone))
        
        result = await self.api_request("GET", f"contacts?query={phone_clean}")
        
        if result and result.get("_embedded", {}).get("contacts"):
            return result["_embedded"]["contacts"][0]
        return None
    
    async def create_unsorted(self, phone: str, name: str = None):
        """Создание неразобранного (если контакт не найден)"""
        data = [{
            "source_name": "FreePBX",
            "source_uid": phone,
            "pipeline_id": None,
            "_embedded": {
                "leads": [{
                    "name": f"Новый звонок от {phone}",
                    "custom_fields_values": [{
                        "field_code": "PHONE",
                        "values": [{"value": phone}]
                    }]
                }]
            }
        }]
        
        result = await self.api_request("POST", "leads/unsorted/forms", data)
        return result
    
    async def add_call_to_contact(self, contact_id: int, phone: str, 
                                  call_data: Dict, recording_path: str = None):
        """Добавление звонка к контакту"""
        # Создание звонка
        call_note = {
            "entity_id": contact_id,
            "note_type": "call_in" if call_data["direction"] == "inbound" else "call_out",
            "params": {
                "phone": phone,
                "duration": call_data["duration"],
                "call_status": 4 if call_data["status"] == "ANSWERED" else 6,
                "call_result": "Успешно" if call_data["status"] == "ANSWERED" else "Не отвечен"
            }
        }
        
        result = await self.api_request(
            "POST", 
            f"contacts/{contact_id}/notes",
            [call_note]
        )
        
        # Прикрепление записи разговора
        if recording_path and os.path.exists(recording_path):
            await self.upload_recording(contact_id, recording_path, call_data)
            
        return result
    
    async def upload_recording(self, contact_id: int, file_path: str, call_data: Dict):
        """Загрузка записи разговора"""
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()
                file_name = os.path.basename(file_path)
                
                # Создание FormData для загрузки файла
                form = aiohttp.FormData()
                form.add_field('file', file_content, 
                             filename=file_name,
                             content_type='audio/wav')
                
                headers = {
                    "Authorization": f"Bearer {self.access_token}"
                }
                
                async with aiohttp.ClientSession() as session:
                    # Загрузка файла
                    async with session.post(
                        f"{self.base_url}/api/v4/contacts/{contact_id}/files",
                        headers=headers,
                        data=form
                    ) as resp:
                        if resp.status == 200:
                            logger.info(f"Запись загружена для контакта {contact_id}")
                        else:
                            text = await resp.text()
                            logger.error(f"Ошибка загрузки записи: {text}")
                            
        except Exception as e:
            logger.error(f"Ошибка при загрузке записи: {e}")


class CallProcessor:
    """Обработчик звонков с записью"""
    
    def __init__(self, config: Config, amocrm: AmoCRMAPI):
        self.config = config
        self.amocrm = amocrm
        self.active_calls = {}
        self.recordings_dir = "/var/spool/asterisk/monitor"
        
    async def process_call(self, call_data: Dict):
        """Обработка завершённого звонка"""
        phone = call_data["phone"]
        
        # Поиск контакта
        contact = await self.amocrm.find_contact(phone)
        
        if not contact:
            logger.info(f"Контакт не найден, создаём неразобранное")
            await self.amocrm.create_unsorted(phone)
            return
        
        # Поиск записи разговора
        recording_path = self.find_recording(call_data["uniqueid"])
        
        # Добавление звонка к контакту
        await self.amocrm.add_call_to_contact(
            contact["id"],
            phone,
            call_data,
            recording_path
        )
        
        logger.info(f"Звонок обработан для контакта {contact['id']}")
        
    def find_recording(self, uniqueid: str) -> Optional[str]:
        """Поиск файла записи разговора"""
        # FreePBX сохраняет записи в формате:
        # /var/spool/asterisk/monitor/YYYY/MM/DD/
        
        patterns = [
            f"{self.recordings_dir}/**/*{uniqueid}*.wav",
            f"{self.recordings_dir}/**/*{uniqueid}*.WAV",
            f"{self.recordings_dir}/**/*{uniqueid}*.mp3"
        ]
        
        from glob import glob
        for pattern in patterns:
            files = glob(pattern, recursive=True)
            if files:
                # Возвращаем самый свежий файл
                return max(files, key=os.path.getmtime)
        
        return None


class WebhookServer:
    """HTTP сервер для webhook и OAuth"""
    
    def __init__(self, config: Config, amocrm: AmoCRMAPI, processor: CallProcessor):
        self.config = config
        self.amocrm = amocrm
        self.processor = processor
        self.app = web.Application()
        self.setup_routes()
        
    def setup_routes(self):
        """Настройка маршрутов"""
        self.app.router.add_get('/oauth', self.handle_oauth)
        self.app.router.add_post('/webhook/call', self.handle_call_webhook)
        self.app.router.add_get('/health', self.handle_health)
        
    async def handle_oauth(self, request):
        """Обработка OAuth callback"""
        code = request.query.get('code')
        
        if not code:
            return web.json_response({
                "error": "No code provided",
                "auth_url": await self.amocrm.get_auth_code_url()
            }, status=400)
        
        try:
            await self.amocrm.exchange_code(code)
            return web.json_response({
                "success": True,
                "message": "Авторизация успешна"
            })
        except Exception as e:
            logger.error(f"OAuth error: {e}")
            return web.json_response({
                "error": str(e)
            }, status=500)
    
    async def handle_call_webhook(self, request):
        """Webhook для обработки звонков из Asterisk"""
        try:
            data = await request.json()
            
            # Ожидаемый формат:
            # {
            #   "phone": "79991234567",
            #   "direction": "inbound",
            #   "duration": 120,
            #   "status": "ANSWERED",
            #   "uniqueid": "1634567890.123"
            # }
            
            await self.processor.process_call(data)
            
            return web.json_response({"success": True})
            
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.json_response({
                "error": str(e)
            }, status=500)
    
    async def handle_health(self, request):
        """Health check endpoint"""
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat()
        })
    
    async def start(self, host: str = "0.0.0.0", port: int = 8080):
        """Запуск сервера"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Webhook сервер запущен на {host}:{port}")


class AsteriskAMIHandler:
    """Обработчик Asterisk AMI с интеграцией"""
    
    def __init__(self, config: Config, processor: CallProcessor):
        self.config = config
        self.processor = processor
        self.active_channels = {}
        
    async def connect(self):
        """Подключение к AMI"""
        from panoramisk import Manager
        
        self.manager = Manager(
            host=self.config.get("asterisk", "ami_host"),
            port=self.config.get("asterisk", "ami_port"),
            username=self.config.get("asterisk", "ami_user"),
            secret=self.config.get("asterisk", "ami_secret"),
            ping_delay=10,
            ping_timeout=5
        )
        
        await self.manager.connect()
        logger.info("Подключено к Asterisk AMI")
        
        # Регистрация обработчиков
        self.manager.register_event("Newchannel", self.on_new_channel)
        self.manager.register_event("Hangup", self.on_hangup)
        self.manager.register_event("BridgeEnter", self.on_bridge_enter)
        
    async def on_new_channel(self, manager, event):
        """Новый канал"""
        uniqueid = event.Uniqueid
        channel = event.Channel
        callerid = event.CallerIDNum
        
        self.active_channels[uniqueid] = {
            "channel": channel,
            "callerid": callerid,
            "start_time": datetime.now(),
            "direction": self.detect_direction(channel),
            "connected": False
        }
        
        logger.debug(f"Новый канал: {uniqueid}, CallerID: {callerid}")
        
    async def on_bridge_enter(self, manager, event):
        """Канал вошёл в мост (разговор начался)"""
        uniqueid = event.Uniqueid
        
        if uniqueid in self.active_channels:
            self.active_channels[uniqueid]["connected"] = True
            self.active_channels[uniqueid]["answer_time"] = datetime.now()
            
    async def on_hangup(self, manager, event):
        """Завершение звонка"""
        uniqueid = event.Uniqueid
        cause = event.Cause
        cause_txt = event.get("Cause-txt", "UNKNOWN")
        
        if uniqueid not in self.active_channels:
            return
        
        call = self.active_channels[uniqueid]
        end_time = datetime.now()
        
        # Расчёт длительности
        if call.get("answer_time"):
            duration = (end_time - call["answer_time"]).seconds
            status = "ANSWERED"
        else:
            duration = 0
            status = cause_txt
        
        # Определение номера телефона
        phone = self.extract_phone(call)
        
        if not phone:
            logger.warning(f"Не удалось извлечь номер для {uniqueid}")
            del self.active_channels[uniqueid]
            return
        
        # Подготовка данных для обработки
        call_data = {
            "phone": phone,
            "direction": call["direction"],
            "duration": duration,
            "status": status,
            "uniqueid": uniqueid,
            "timestamp": call["start_time"].isoformat()
        }
        
        # Асинхронная обработка (не блокируем AMI)
        asyncio.create_task(self.processor.process_call(call_data))
        
        del self.active_channels[uniqueid]
        logger.info(f"Звонок завершён: {uniqueid}, {phone}, {duration}с")
        
    def detect_direction(self, channel: str) -> str:
        """Определение направления звонка по имени канала"""
        channel_lower = channel.lower()
        
        # Входящие обычно содержат from-trunk или from-pstn
        if "from-trunk" in channel_lower or "from-pstn" in channel_lower:
            return "inbound"
        # Исходящие
        elif "from-internal" in channel_lower:
            return "outbound"
        
        return "unknown"
    
    def extract_phone(self, call: Dict) -> Optional[str]:
        """Извлечение номера телефона из данных звонка"""
        callerid = call.get("callerid", "")
        
        # Очистка номера
        phone = ''.join(filter(str.isdigit, callerid))
        
        # Для российских номеров
        if phone.startswith("8") and len(phone) == 11:
            phone = "7" + phone[1:]
        
        return phone if len(phone) >= 10 else None


async def main():
    """Основная функция"""
    
    # Загрузка конфигурации
    config = Config()
    
    # Инициализация AmoCRM
    amocrm = AmoCRMAPI(config)
    await amocrm.load_tokens()
    
    # Проверка токенов
    if not amocrm.access_token:
        logger.error("Токены не найдены!")
        logger.info(f"Получите код авторизации: {await amocrm.get_auth_code_url()}")
        logger.info("Затем отправьте GET запрос: http://your-server:8080/oauth?code=YOUR_CODE")
        # Продолжаем работу для обработки OAuth
    
    # Инициализация процессора звонков
    processor = CallProcessor(config, amocrm)
    
    # Запуск webhook сервера
    webhook = WebhookServer(config, amocrm, processor)
    await webhook.start(
        host=config.get("webhook", "host") or "0.0.0.0",
        port=config.get("webhook", "port") or 8080
    )
    
    # Подключение к Asterisk AMI
    ami_handler = AsteriskAMIHandler(config, processor)
    
    try:
        await ami_handler.connect()
    except Exception as e:
        logger.error(f"Ошибка подключения к AMI: {e}")
        logger.info("Сервер продолжит работу без AMI (только webhook)")
    
    # Бесконечный цикл
    try:
        logger.info("Интеграция запущена")
        while True:
            await asyncio.sleep(60)
            # Периодическая проверка токенов (раз в час)
            if datetime.now().minute == 0:
                try:
                    await amocrm.refresh_tokens()
                except Exception as e:
                    logger.error(f"Ошибка обновления токенов: {e}")
                    
    except KeyboardInterrupt:
        logger.info("Остановка сервиса...")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")