# Интеграция FreePBX 17 с AmoCRM

## Установка

### 1. Быстрая установка

```bash
# Скачайте и запустите установочный скрипт
wget https://your-repo/install.sh
chmod +x install.sh
sudo ./install.sh
```

### 2. Ручная установка

```bash
# Установка зависимостей
apt update && apt install -y python3-pip python3-venv redis-server

# Создание директории
mkdir -p /opt/freepbx-amocrm
cd /opt/freepbx-amocrm

# Виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# Python пакеты
pip install aiohttp panoramisk redis
```

## Конфигурация

### config.json

```json
{
  "amocrm": {
    "subdomain": "yourcompany",
    "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "client_secret": "your_secret_here",
    "redirect_uri": "https://your-domain.com:8080/oauth"
  },
  "asterisk": {
    "ami_host": "localhost",
    "ami_port": 5038,
    "ami_user": "amocrm_user",
    "ami_secret": "StrongPassword123!"
  },
  "webhook": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "redis": {
    "url": "redis://localhost:6379"
  },
  "logging": {
    "level": "INFO",
    "file": "/var/log/freepbx-amocrm.log"
  },
  "debug": {
    "process_internal_calls": true,
    "test_phone": "79991234567",
    "detailed_ami_logging": true
  }
}
```

## Настройка AmoCRM

### 1. Создание интеграции

1. Перейдите в AmoCRM: **Настройки → Интеграции**
2. Нажмите **Создать интеграцию**
3. Заполните данные:
   - **Название**: FreePBX Integration
   - **Redirect URI**: `https://your-server:8080/oauth`
4. Сохраните **Client ID** и **Client Secret**

### 2. Права доступа

Необходимые scope:
- `crm` - доступ к CRM
- `contacts` - работа с контактами
- `leads` - работа со сделками
- `files` - загрузка файлов

### 3. Получение токенов

После установки выполните:

```bash
# Запустите сервис
systemctl start freepbx-amocrm

# Получите URL для авторизации
journalctl -u freepbx-amocrm -n 50 | grep "auth_url"

# Откройте URL в браузере, авторизуйтесь
# Скопируйте code из redirect URL
# Отправьте запрос:
curl "http://localhost:8080/oauth?code=YOUR_CODE"
```

## Настройка FreePBX 17

### 1. AMI пользователь

Создайте файл `/etc/asterisk/manager_custom.conf`:

```ini
[amocrm_user]
secret = StrongPassword123!
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
writetimeout = 5000
```

Примените изменения:

```bash
asterisk -rx "manager reload"
```

### 2. Запись разговоров

В FreePBX перейдите: **Admin → System Recordings**

Включите запись для нужных направлений:
- **Inbound Routes** → выберите маршрут → **Call Recording**: Yes
- **Extensions** → выберите номер → **Record Incoming/Outgoing**: Always

### 3. Особенности FreePBX 17

FreePBX 17 на Asterisk 21 использует:
- Новые форматы каналов: `PJSIP/xxx` вместо `SIP/xxx`
- Улучшенную систему мостов (Bridge)
- Изменённые пути записей: `/var/spool/asterisk/monitor/YYYY/MM/DD/`

## Systemd Service

Создайте `/etc/systemd/system/freepbx-amocrm.service`:

```ini
[Unit]
Description=FreePBX AmoCRM Integration
After=network.target asterisk.service redis.service
Requires=redis.service

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=/opt/freepbx-amocrm
Environment="PATH=/opt/freepbx-amocrm/venv/bin"
ExecStart=/opt/freepbx-amocrm/venv/bin/python3 /opt/freepbx-amocrm/integration.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=freepbx-amocrm

[Install]
WantedBy=multi-user.target
```

Активируйте:

```bash
systemctl daemon-reload
systemctl enable freepbx-amocrm
systemctl start freepbx-amocrm
```

## Мониторинг

### Просмотр логов

```bash
# Realtime логи
journalctl -u freepbx-amocrm -f

# Последние 100 строк
journalctl -u freepbx-amocrm -n 100

# Логи за сегодня
journalctl -u freepbx-amocrm --since today

# Файл лог
tail -f /var/log/freepbx-amocrm.log
```

### Проверка статуса

```bash
# Статус сервиса
systemctl status freepbx-amocrm

# Health check
curl http://localhost:8080/health
```

### Тестирование звонка

```bash
# Через AMI (для исходящего)
asterisk -rx "originate PJSIP/101 application playback demo-congrats"

# Проверьте логи интеграции
journalctl -u freepbx-amocrm -f
```

## Webhook для внешних систем

Для интеграции с внешними системами используйте webhook endpoint:

```bash
POST http://your-server:8080/webhook/call
Content-Type: application/json

{
  "phone": "79991234567",
  "direction": "inbound",
  "duration": 120,
  "status": "ANSWERED",
  "uniqueid": "1634567890.123"
}
```

## Troubleshooting

### Проблема: Токены не обновляются

```bash
# Удалите старые токены
rm /opt/freepbx-amocrm/tokens.json

# Перезапустите и пройдите OAuth заново
systemctl restart freepbx-amocrm
```

### Проблема: AMI не подключается

```bash
# Проверьте AMI
asterisk -rx "manager show users"

# Проверьте подключение
telnet localhost 5038

# Проверьте права файла
ls -la /etc/asterisk/manager_custom.conf
```

### Проблема: Записи не загружаются

```bash
# Проверьте права на директорию
ls -la /var/spool/asterisk/monitor/

# Установите права
chown -R asterisk:asterisk /var/spool/asterisk/monitor/

# Проверьте формат записей
ls -lah /var/spool/asterisk/monitor/$(date +%Y/%m/%d)/
```

### Проблема: Контакты не находятся

- Проверьте формат номеров в AmoCRM (должны быть цифры)
- Убедитесь, что номера сохранены в поле "Телефон"
- Проверьте нормализацию номеров в коде

## Дополнительные возможности

### Уведомления в Telegram

Добавьте в `integration.py`:

```python
async def send_telegram_notification(phone, status):
    bot_token = "YOUR_BOT_TOKEN"
    chat_id = "YOUR_CHAT_ID"
    
    message = f"📞 Новый звонок\nНомер: {phone}\nСтатус: {status}"
    
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message}
        )
```

### Интеграция с внешним API

```python
async def notify_external_system(call_data):
    async with aiohttp.ClientSession() as session:
        await session.post(
            "https://your-api.com/calls",
            json=call_data,
            headers={"Authorization": "Bearer YOUR_TOKEN"}
        )
```

## Безопасность

1. **Используйте HTTPS** для webhook endpoint
2. **Ограничьте доступ** к AMI только с localhost
3. **Регулярно обновляйте** токены AmoCRM
4. **Логируйте** все операции
5. **Бэкап** конфигурации и токенов

```bash
# Настройка firewall
ufw allow 5060/tcp  # SIP
ufw allow 5061/tcp  # SIP TLS
ufw allow 10000:20000/udp  # RTP
ufw allow 8080/tcp  # Webhook (только для доверенных IP)
```

## Обновление

```bash
cd /opt/freepbx-amocrm
source venv/bin/activate
pip install --upgrade aiohttp panoramisk redis
systemctl restart freepbx-amocrm
```

## Поддержка

- Логи: `/var/log/freepbx-amocrm.log`
- Конфигурация: `/opt/freepbx-amocrm/config.json`
- Токены: `/opt/freepbx-amocrm/tokens.json`

## Лицензия

MIT License
