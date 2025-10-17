#!/usr/bin/env python3
"""
Полная интеграция FreePBX 17 с AmoCRM v2.0
С поддержкой отладки внутренних звонков
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
from glob import glob

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
        logger.info(f"Конфигурация загружена из {config_path}")
    
    def get(self, *keys, default=None):
        """Получение значения по пути ключей"""
        value = self.data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            if value is None:
                return default
        return value


class AmoCRMAPI:
    """Клиент AmoCRM API v4"""
    
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
                logger.info("✓ Токены AmoCRM загружены")
        else:
            logger.warning("⚠️  Токены не найдены, требуется авторизация")
                
    async def save_tokens(self):
        """Сохранение токенов в файл"""
        with open(self.token_file, 'w') as f:
            json.dump({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "updated_at": datetime.now().isoformat()
            }, f)
        os.chmod(self.token_file, 0o600)
        logger.info("✓ Токены сохранены")
        
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
                logger.info("✓ Авторизация AmoCRM успешна")
                
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
                logger.info("✓ Токены обновлены")
                
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
                    logger.info("Токен устарел, обновляем...")
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
        
        logger.debug(f"Поиск контакта: {phone_clean}")
        result = await self.api_request("GET", f"contacts?query={phone_clean}")
        
        if result and result.get("_embedded", {}).get("contacts"):
            contact = result["_embedded"]["contacts"][0]
            logger.info(f"✓ Контакт найден: ID={contact['id']}, Имя={contact.get('name', 'N/A')}")
            return contact
        
        logger.info(f"❌ Контакт с номером {phone_clean} не найден")
        return None
    
    async def create_unsorted(self, phone: str):
        """Создание неразобранного"""
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
        if result:
            logger.info(f"✓ Неразобранное создано для {phone}")
        return result
    
    async def add_call_to_contact(self, contact_id: int, phone: str, 
                                  call_data: Dict, recording_path: str = None):
        """Добавление звонка к контакту"""
        # Определение типа звонка
        if call_data["direction"] == "inbound":
            note_type = "call_in"
        elif call_data["direction"] == "outbound":
            note_type = "call_out"
        else:
            note_type = "call_in"  # default
        
        # Определение статуса
        call_status = 4 if call_data["status"] == "ANSWERED" else 6
        
        call_note = {
            "entity_id": contact_id,
            "note_type": note_type,
            "params": {
                "phone": phone,
                "duration": call_data["duration"],
                "call_status": call_status,
                "call_result": "Успешно" if call_data["status"] == "ANSWERED" else "Не отвечен"
            }
        }
        
        result = await self.api_request(
            "POST", 
            f"contacts/{contact_id}/notes",
            [call_note]
        )
        
        # Загрузка записи (если есть)
        if recording_path and os.path.exists(recording_path):
            await self.upload_recording(contact_id, recording_path)
            
        return result
    
    async def upload_recording(self, contact_id: int, file_path: str):
        """Загрузка записи разговора"""
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()
                file_name = os.path.basename(file_path)
                
                form = aiohttp.FormData()
                form.add_field('file', file_content, 
                             filename=file_name,
                             content_type='audio/wav')
                
                headers = {
                    "Authorization": f"Bearer {self.access_token}"
                }
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/api/v4/contacts/{contact_id}/files",
                        headers=headers,
                        data=form
                    ) as resp:
                        if resp.status == 200:
                            logger.info(f"🎙️  Запись загружена: {file_name}")
                        else:
                            text = await resp.text()
                            logger.error(f"Ошибка загрузки записи: {text}")
                            
        except Exception as e:
            logger.error(f"Ошибка при загрузке записи: {e}")


class CallProcessor:
    """Обработчик звонков"""
    
    def __init__(self, config: Config, amocrm: AmoCRMAPI):
        self.config = config
        self.amocrm = amocrm
        self.recordings_dir = "/var/spool/asterisk/monitor"
        
    async def process_call(self, call_data: Dict):
        """Обработка завершённого звонка"""
        phone = call_data["phone"]
        is_internal = call_data.get("internal_call", False)
        
        logger.info(f"📞 Обработка звонка: {phone} ({call_data['direction']}, {call_data['duration']}с)")
        
        if is_internal:
            logger.info(f"ℹ️  Внутренний звонок, используем тестовый режим")
        
        # Поиск контакта
        contact = await self.amocrm.find_contact(phone)
        
        if not contact:
            logger.info(f"Создаём неразобранное для {phone}")
            await self.amocrm.create_unsorted(phone)
            return
        
        # Поиск записи
        recording_path = self.find_recording(call_data["uniqueid"])
        
        if recording_path:
            logger.info(f"🎙️  Найдена запись: {recording_path}")
        
        # Добавление звонка
        result = await self.amocrm.add_call_to_contact(
            contact["id"],
            phone,
            call_data,
            recording_path
        )
        
        if result:
            logger.info(f"✓ Звонок добавлен к контакту {contact['id']}")
        else:
            logger.error(f"❌ Ошибка добавления звонка")
        
    def find_recording(self, uniqueid: str) -> Optional[str]:
        """Поиск файла записи"""
        patterns = [
            f"{self.recordings_dir}/**/*{uniqueid}*.wav",
            f"{self.recordings_dir}/**/*{uniqueid}*.WAV",
            f"{self.recordings_dir}/**/*{uniqueid}*.mp3"
        ]
        
        for pattern in patterns:
            files = glob(pattern, recursive=True)
            if files:
                return max(files, key=os.path.getmtime)
        
        return None


class WebhookServer:
    """HTTP сервер"""
    
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
        self.app.router.add_get('/test-call', self.handle_test_call)
        
    async def handle_oauth(self, request):
        """OAuth callback"""
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
            return web.json_response({"error": str(e)}, status=500)
    
    async def handle_call_webhook(self, request):
        """Webhook для звонков"""
        try:
            data = await request.json()
            await self.processor.process_call(data)
            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def handle_health(self, request):
        """Health check"""
        amocrm_status = "ok" if self.amocrm.access_token else "no_token"
        return web.json_response({
            "status": "ok",
            "amocrm": amocrm_status,
            "timestamp": datetime.now().isoformat()
        })
    
    async def handle_test_call(self, request):
        """🧪 ТЕСТОВЫЙ ENDPOINT"""
        try:
            test_phone = self.config.get("debug", "test_phone") or "79991234567"
            
            call_data = {
                "phone": test_phone,
                "direction": "inbound",
                "duration": 42,
                "status": "ANSWERED",
                "uniqueid": f"test_{int(datetime.now().timestamp())}",
                "timestamp": datetime.now().isoformat(),
                "internal_call": False
            }
            
            logger.info(f"🧪 Тестовый звонок: {call_data}")
            await self.processor.process_call(call_data)
            
            return web.json_response({
                "success": True,
                "message": f"Тестовый звонок обработан для {test_phone}",
                "call_data": call_data
            })
        except Exception as e:
            logger.error(f"Test call error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
    
    async def start(self, host: str = "0.0.0.0", port: int = 8080):
        """Запуск сервера"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"🌐 Webhook сервер запущен на {host}:{port}")
        logger.info(f"   - Health check: http://{host}:{port}/health")
        logger.info(f"   - Test call: http://{host}:{port}/test-call")


class AsteriskAMIHandler:
    """Обработчик Asterisk AMI"""
    
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
        logger.info("✓ Подключено к Asterisk AMI")
        
        # Регистрация обработчиков
        self.manager.register_event("Newchannel", self.on_new_channel)
        self.manager.register_event("Hangup", self.on_hangup)
        self.manager.register_event("BridgeEnter", self.on_bridge_enter)
        
    async def on_new_channel(self, manager, event):
        """Новый канал"""
        uniqueid = event.Uniqueid
        channel = event.Channel
        callerid = event.CallerIDNum
        exten = event.get("Exten", "")
        context = event.get("Context", "")
        
        # Детальное логирование
        if self.config.get("debug", "detailed_ami_logging"):
            logger.info(f"""
╔══════════════════════════════════════
║ НОВЫЙ КАНАЛ
╠══════════════════════════════════════
║ UniqueID:  {uniqueid}
║ Channel:   {channel}
║ CallerID:  {callerid}
║ Exten:     {exten}
║ Context:   {context}
╚══════════════════════════════════════""")
        
        self.active_channels[uniqueid] = {
            "channel": channel,
            "callerid": callerid,
            "exten": exten,
            "context": context,
            "start_time": datetime.now(),
            "direction": self.detect_direction(channel, context),
            "connected": False
        }
        
    async def on_bridge_enter(self, manager, event):
        """Канал вошёл в мост"""
        uniqueid = event.Uniqueid
        if uniqueid in self.active_channels:
            self.active_channels[uniqueid]["connected"] = True
            self.active_channels[uniqueid]["answer_time"] = datetime.now()
            
    async def on_hangup(self, manager, event):
        """Завершение звонка"""
        uniqueid = event.Uniqueid
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
        
        # Детальное логирование
        if self.config.get("debug", "detailed_ami_logging"):
            logger.info(f"""
╔══════════════════════════════════════
║ ЗАВЕРШЕНИЕ ЗВОНКА
╠══════════════════════════════════════
║ UniqueID:   {uniqueid}
║ CallerID:   {call.get('callerid')}
║ Exten:      {call.get('exten')}
║ Direction:  {call.get('direction')}
║ Duration:   {duration}s
║ Status:     {status}
╚══════════════════════════════════════""")
        
        # Извлечение номера
        phone = self.extract_phone(call)
        
        # 🔴 РЕЖИМ ОТЛАДКИ - подмена внутренних номеров
        if not phone and self.config.get("debug", "process_internal_calls"):
            test_phone = self.config.get("debug", "test_phone")
            if test_phone:
                logger.info(f"⚠️  РЕЖИМ ОТЛАДКИ: Используем тестовый номер {test_phone}")
                phone = test_phone
            else:
                logger.warning(f"Внутренний звонок {call.get('callerid')} → {call.get('exten')}, пропускаем")
        
        if not phone:
            logger.warning(f"Не удалось извлечь номер для {uniqueid}")
            del self.active_channels[uniqueid]
            return
        
        # Подготовка данных
        call_data = {
            "phone": phone,
            "direction": call["direction"],
            "duration": duration,
            "status": status,
            "uniqueid": uniqueid,
            "timestamp": call["start_time"].isoformat(),
            "internal_call": len(call.get("callerid", "")) <= 4
        }
        
        # Обработка
        asyncio.create_task(self.processor.process_call(call_data))
        
        del self.active_channels[uniqueid]
        logger.info(f"✓ Звонок отправлен на обработку: {phone}, {duration}с, {status}")
        
    def detect_direction(self, channel: str, context: str = "") -> str:
        """Определение направления"""
        channel_lower = channel.lower()
        context_lower = context.lower()
        
        if "from-trunk" in channel_lower or "from-pstn" in channel_lower:
            return "inbound"
        if "from-trunk" in context_lower or "from-pstn" in context_lower:
            return "inbound"
        elif "from-internal" in channel_lower or "from-internal" in context_lower:
            return "outbound"
        elif "ext-local" in context_lower:
            return "internal"
        
        return "unknown"
    
    def extract_phone(self, call: Dict) -> Optional[str]:
        """Извлечение номера"""
        callerid = call.get("callerid", "")
        phone = ''.join(filter(str.isdigit, callerid))
        
        # Нормализация для РФ
        if phone.startswith("8") and len(phone) == 11:
            phone = "7" + phone[1:]
        
        # Минимум 10 цифр для валидного номера
        return phone if len(phone) >= 10 else None


async def main():
    """Главная функция"""
    
    try:
        # Загрузка конфигурации
        config = Config()
        
        # AmoCRM
        amocrm = AmoCRMAPI(config)
        await amocrm.load_tokens()
        
        if not amocrm.access_token:
            logger.warning("="*50)
            logger.warning("⚠️  ТРЕБУЕТСЯ АВТОРИЗАЦИЯ AmoCRM")
            logger.warning(f"1. Откройте: {await amocrm.get_auth_code_url()}")
            logger.warning("2. Авторизуйтесь и скопируйте code")
            logger.warning("3. Откройте: http://your-server:8080/oauth?code=YOUR_CODE")
            logger.warning("="*50)
        
        # Процессор
        processor = CallProcessor(config, amocrm)
        
        # Webhook сервер
        webhook = WebhookServer(config, amocrm, processor)
        await webhook.start(
            host=config.get("webhook", "host", default="0.0.0.0"),
            port=config.get("webhook", "port", default=8080)
        )
        
        # AMI
        ami_handler = AsteriskAMIHandler(config, processor)
        try:
            await ami_handler.connect()
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к AMI: {e}")
            logger.info("Сервер продолжит работу без AMI (только webhook)")
        
        # Бесконечный цикл
        logger.info("="*50)
        logger.info("✓ Интеграция FreePBX + AmoCRM запущена")
        logger.info("="*50)
        
        while True:
            await asyncio.sleep(60)
            
            # Автообновление токенов каждый час
            if datetime.now().minute == 0 and amocrm.refresh_token:
                try:
                    await amocrm.refresh_tokens()
                except Exception as e:
                    logger.error(f"Ошибка обновления токенов: {e}")
                    
    except KeyboardInterrupt:
        logger.info("Остановка сервиса...")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено")
