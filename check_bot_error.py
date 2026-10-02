#!/usr/bin/env python3
"""Проверяем ошибки бота на сервере"""
import paramiko
import sys

HOST = "155.212.162.47"
USER = "root"
PASSWORD = "Booking2025"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

try:
    print("Подключаемся к серверу...")
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    print("✓ Подключено\n")

    # Запускаем бота напрямую
    print("=== Попытка запустить бота ===\n")
    stdin, stdout, stderr = ssh.exec_command(
        'cd /opt/flight_bot && /opt/flight_bot/venv/bin/python -m bot.main 2>&1 | head -100',
        get_pty=False
    )
    
    output = stdout.read().decode('utf-8', errors='ignore')
    print(output)
    
    if stderr:
        error_output = stderr.read().decode('utf-8', errors='ignore')
        if error_output:
            print("\n=== STDERR ===\n")
            print(error_output)

except Exception as e:
    print(f"Ошибка подключения: {e}")
    sys.exit(1)
finally:
    ssh.close()
