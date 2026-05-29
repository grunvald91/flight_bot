#!/usr/bin/env python3
"""Deploy flight_bot to server — first deploy + subsequent updates"""
import paramiko
import os
import sys
import time
import tarfile

HOST = "155.212.162.47"
USER = "root"
PASSWORD = "Booking2025"
REMOTE_PATH = "/opt/flight_bot"
SERVICE_NAME = "flight-bot"

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_NAME = "flight_bot_deploy.tar.gz"

EXCLUDE_DIRS = {"venv", ".venv", "__pycache__", ".git", "data", ".pytest_cache", ".egg-info", "dist", "build"}
EXCLUDE_FILES = {".env.example", "*.pyc", "*.pyo"}
ARCHIVE_NAME = "flight_bot_deploy.tar.gz"


def create_archive():
    print("Creating deployment archive...")
    archive_path = os.path.join(LOCAL_DIR, ARCHIVE_NAME)

    with tarfile.open(archive_path, "w:gz") as tar:
        count = 0
        for root, dirs, files in os.walk(LOCAL_DIR):
            # Filter out excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            
            for f in files:
                # Skip excluded files and archive itself
                if f == ARCHIVE_NAME or any(f.endswith(ext) for ext in [".pyc", ".pyo"]):
                    continue
                    
                filepath = os.path.join(root, f)
                arcname = os.path.relpath(filepath, LOCAL_DIR)
                tar.add(filepath, arcname)
                count += 1
        
        print(f"  Added {count} files")

    size = os.path.getsize(archive_path) / 1024
    print(f"Archive created: {size:.1f} KB")
    return archive_path


def deploy(archive_path: str, first_deploy: bool = False):
    print(f"\nConnecting to {HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)
        print("Connected!")

        def run(cmd, check=True):
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if out:
                print(f"  {out}", flush=True)
            if err and check:
                print(f"  STDERR: {err}", flush=True)
            return exit_code, out, err

        if first_deploy:
            print("\n--- First deploy: setting up environment ---")

            # Create directory
            run(f"mkdir -p {REMOTE_PATH}")

            # Create PostgreSQL database
            print("\nCreating database...")
            run('su - postgres -c "psql -c \\"CREATE DATABASE flight_bot;\\" 2>/dev/null || echo DB exists"', check=False)
            run('su - postgres -c "psql -c \\"GRANT ALL PRIVILEGES ON DATABASE flight_bot TO bot_user;\\" 2>/dev/null || true"', check=False)

        # Upload archive
        print("\nUploading archive...")
        sftp = ssh.open_sftp()
        sftp.put(archive_path, f"{REMOTE_PATH}/{ARCHIVE_NAME}")
        sftp.close()
        print("Uploaded!")

        # Stop service if exists
        print("\nStopping service...")
        run(f"systemctl stop {SERVICE_NAME} 2>/dev/null || true", check=False)

        # Extract
        print("\nExtracting files...")
        run(f"cd {REMOTE_PATH} && tar -xzf {ARCHIVE_NAME} && rm {ARCHIVE_NAME}")

        if first_deploy:
            # Create venv and install dependencies
            print("\nCreating virtual environment...")
            run(f"cd {REMOTE_PATH} && python3 -m venv venv")

            print("\nInstalling dependencies...")
            run(f"cd {REMOTE_PATH} && venv/bin/pip install --upgrade pip")
            code, out, err = run(f"cd {REMOTE_PATH} && venv/bin/pip install -r requirements.txt")
            run(f"cd {REMOTE_PATH} && cp requirements.txt requirements.txt.bak")

            # Create data directory
            run(f"mkdir -p {REMOTE_PATH}/data")

            # Install systemd service
            print("\nInstalling systemd service...")
            run(f"cp {REMOTE_PATH}/flight_bot.service /etc/systemd/system/{SERVICE_NAME}.service")
            run("systemctl daemon-reload")
            run(f"systemctl enable {SERVICE_NAME}")
        else:
            # Update dependencies only if requirements.txt changed
            print("\nChecking for dependency changes...")
            code, out, err = run(f"cd {REMOTE_PATH} && diff -q requirements.txt requirements.txt.bak 2>/dev/null && echo 'unchanged' || echo 'changed'", check=False)
            
            if "changed" in out:
                print("Updating dependencies...")
                run(f"cd {REMOTE_PATH} && venv/bin/pip install -r requirements.txt --no-cache-dir -q")
                run(f"cd {REMOTE_PATH} && cp requirements.txt requirements.txt.bak")
            else:
                print("Dependencies unchanged, skipping pip install")

        # Start service
        print("\nStarting service...")
        run(f"systemctl start {SERVICE_NAME}")

        time.sleep(3)

        # Check status
        print("\nChecking status...")
        code, out, err = run(f'ps aux | grep "python.*bot.main" | grep -v grep')

        if out:
            print(f"\n{'=' * 50}")
            print("SUCCESS: Flight Bot is running!")
            print(f"{'=' * 50}")
        else:
            print(f"\n{'=' * 50}")
            print("WARNING: Bot may not be running!")
            print(f"{'=' * 50}")
            print("\nLast logs:")
            run(f"journalctl -u {SERVICE_NAME} -n 30 --no-pager")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        ssh.close()


if __name__ == "__main__":
    os.chdir(LOCAL_DIR)

    # Check if first deploy
    first = "--first" in sys.argv or "--init" in sys.argv

    print(f"{'=' * 50}")
    print("FLIGHT BOT DEPLOYMENT")
    if first:
        print("MODE: First deploy (full setup)")
    else:
        print("MODE: Update")
    print(f"{'=' * 50}")

    archive = create_archive()
    deploy(archive, first_deploy=first)

    # Cleanup local archive
    if os.path.exists(archive):
        os.remove(archive)
