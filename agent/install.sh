#!/bin/bash
# Установка агента на Mac: venv + автозапуск через launchd
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "Сначала создай .env (cp .env.example .env) и заполни его"; exit 1; }
python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
PLIST=~/Library/LaunchAgents/com.tgmac.agent.plist
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.tgmac.agent</string>
<key>ProgramArguments</key><array>
<string>$(pwd)/venv/bin/python</string><string>$(pwd)/agent.py</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/tmp/tgmac.log</string>
<key>StandardErrorPath</key><string>/tmp/tgmac.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ Агент установлен и запущен. Логи: /tmp/tgmac.log"
