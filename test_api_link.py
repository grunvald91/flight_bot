#!/usr/bin/env python3
"""Test what Travelpayouts API returns in the 'link' field"""
import paramiko

HOST = "155.212.162.47"
USER = "root"
PASSWORD = "Booking2025"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)

script = r"""
import requests, json
token = '7608a8996921e1dd5e48839c4a10610d'
url = 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates'
params = {
    'origin': 'LED',
    'destination': 'AER',
    'currency': 'rub',
    'sorting': 'price',
    'direct': 'false',
    'one_way': 'true',
    'unique': 'false',
    'limit': 3,
    'token': token,
}
resp = requests.get(url, params=params, timeout=15)
data = resp.json()
if data.get('success') and data.get('data'):
    for t in data['data'][:3]:
        print('link:', repr(t.get('link')))
        print('origin:', t.get('origin'))
        print('destination:', t.get('destination'))
        print('departure_at:', t.get('departure_at'))
        print('price:', t.get('price'))
        print('airline:', t.get('airline'))
        print('---')
else:
    print('No data:', json.dumps(data, indent=2)[:500])
"""

# Write script to server and run
sftp = ssh.open_sftp()
with sftp.file("/tmp/test_link.py", "w") as f:
    f.write(script)
sftp.close()

stdin, stdout, stderr = ssh.exec_command("python3 /tmp/test_link.py")
stdout.channel.recv_exit_status()
print(stdout.read().decode("utf-8", errors="replace"))
err = stderr.read().decode("utf-8", errors="replace").strip()
if err:
    print("STDERR:", err)

ssh.close()
