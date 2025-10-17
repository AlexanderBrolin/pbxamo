#!/bin/bash
# Скрипт установки интеграции FreePBX 17 с AmoCRM

set -e

echo "=== Установка интеграции FreePBX 17 с AmoCRM ==="

# 1. Обновление системы
apt update && apt upgrade -y

# 2. Установка зависимостей
apt install -y python3-pip python3-venv redis-server git

# 3. Создание директории проекта
mkdir -p /opt/freepbx-amocrm
cd /opt/freepbx-amocrm

# 4. Создание виртуального окружения
python3 -m venv venv
source venv/bin/activate

# 5. Установка Python пакетов
pip install --upgrade pip
pip install aiohttp panoramisk redis

# 6. Создание конфигурационного файла
cat > /opt/freepbx-amocrm/config.json <<'EOF'
{
  "amocrm": {
    "subdomain": "your_subdomain",
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "redirect_uri": "https://your-domain.com/oauth"
  },
  "asterisk": {
    "ami_host": "localhost",
    "ami_port": 5038,
    "ami_user": "amocrm_user",
    "ami_secret": "strong_password_here"
  },
  "redis": {
    "url": "redis://localhost:6379"
  },
  "logging": {
    "level": "INFO",
    "file": "/var/log/freepbx-amocrm.log"
  }
}
EOF

echo "Конфигурация создана в /opt/freepbx-amocrm/config.json"
echo "ВАЖНО: Отредактируйте файл с вашими данными!"

# 7. Настройка AMI пользователя в FreePBX
echo ""
echo "=== Настройка Asterisk AMI ==="
echo "Добавляем пользователя AMI для интеграции..."

cat >> /etc/asterisk/manager_custom.conf <<'EOF'

[amocrm_user]
secret = strong_password_here
deny=0.0.0.0/0.0.0.0
permit=127.0.0.1/255.255.255.0
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan
writetimeout = 5000
EOF

# Перезагрузка Asterisk для применения настроек
asterisk -rx "manager reload"

echo "AMI пользователь создан"

# 8. Создание systemd сервиса
cat > /etc/systemd/system/freepbx-amocrm.service <<'EOF'
[Unit]
Description=FreePBX AmoCRM Integration Service
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

[Install]
WantedBy=multi-user.target
EOF

# 9. Настройка логирования
mkdir -p /var/log
touch /var/log/freepbx-amocrm.log
chown asterisk:asterisk /var/log/freepbx-amocrm.log

# 10. Настройка прав
chown -R asterisk:asterisk /opt/freepbx-amocrm

# 11. Включение сервисов
systemctl daemon-reload
systemctl enable redis-server
systemctl start redis-server
systemctl enable freepbx-amocrm.service

echo ""
echo "=== Установка завершена! ==="
echo ""
echo "Следующие шаги:"
echo "1. Отредактируйте /opt/freepbx-amocrm/config.json"
echo "2. Скопируйте основной скрипт в /opt/freepbx-amocrm/integration.py"
echo "3. Получите код авторизации AmoCRM и выполните первичную аутентификацию"
echo "4. Запустите сервис: systemctl start freepbx-amocrm"
echo "5. Проверьте статус: systemctl status freepbx-amocrm"
echo "6. Логи: journalctl -u freepbx-amocrm -f"
echo ""
echo "Настройка AMI:"
echo "- Пользователь: amocrm_user"
echo "- Пароль: Измените в /etc/asterisk/manager_custom.conf"
echo "- Порт: 5038"
echo ""