#!/usr/bin/env python3
"""Fix PostgreSQL permissions for flight_bot database"""
import paramiko

HOST = "155.212.162.47"
USER = "root"
PASSWORD = "Booking2025"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)
print("Connected")

sql_commands = [
    "GRANT ALL ON SCHEMA public TO bot_user;",
    "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO bot_user;",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bot_user;",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO bot_user;",
    "ALTER DATABASE flight_bot OWNER TO bot_user;",
]

for sql in sql_commands:
    cmd = f"su - postgres -c \"psql -d flight_bot -c '{sql}'\""
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    result = out or err
    print(f"  {sql[:50]}... -> {result}")

ssh.close()
print("Done - permissions fixed")
