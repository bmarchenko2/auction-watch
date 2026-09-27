#!/bin/bash
# Встановлення щоденної перевірки OLX на Mac.
# Запуск (у Terminal):
#   curl -fsSL https://raw.githubusercontent.com/bmarchenko2/auction-watch/main/install_olx_mac.sh | bash
# Повторний запуск оновлює скрипт і нічого не ламає. Видалення: ... | bash -s -- --uninstall
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/bmarchenko2/auction-watch/main"
DIR="$HOME/Library/Application Support/auction-watch"
LABEL="ua.auction-watch.olx"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR=15
MINUTE=50

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Щоденну перевірку OLX вимкнено. Файли лишилися в: $DIR"
  exit 0
fi

echo "==> Перевіряю Python 3"
if ! /usr/bin/python3 -c "import sys; assert sys.version_info >= (3, 8)" 2>/dev/null; then
  echo "Потрібен Python 3. Зараз відкриється встановлення Command Line Tools від Apple."
  echo "Після завершення встановлення запустіть цю команду ще раз."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

mkdir -p "$DIR/logs"
cd "$DIR"

echo "==> Завантажую скрипт"
curl -fsSL "$REPO_RAW/olx_watch.py?t=$(date +%s)" -o olx_watch.py.new
mv olx_watch.py.new olx_watch.py

echo "==> Готую окреме середовище Python (це може зайняти хвилину)"
[[ -x venv/bin/python ]] || /usr/bin/python3 -m venv venv
venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
venv/bin/python -m pip install --quiet cryptography

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=.+' config 2>/dev/null; then
  echo
  echo "Вставте токен бота (той самий, що в секреті TELEGRAM_BOT_TOKEN на GitHub)"
  echo "і натисніть Enter. Символи під час вставлення не показуються — так і має бути."
  read -rs -p "Токен: " TOKEN </dev/tty
  echo
  if [[ ! "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
    echo "Це не схоже на токен бота (має вигляд 123456789:AA...). Запустіть команду ще раз."
    exit 1
  fi
  umask 077
  printf 'TELEGRAM_BOT_TOKEN=%s\nMIN_HECTARES=1\n# TELEGRAM_CHAT_ID=  (необов'"'"'язково, додаткові адресати через кому)\n' "$TOKEN" > config
  chmod 600 config
  unset TOKEN
fi

echo "==> Налаштовую щоденний запуск о $HOUR:$(printf %02d $MINUTE)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/venv/bin/python</string>
    <string>$DIR/olx_watch.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$DIR/logs/olx.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/olx.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "==> Перший запуск (надішле в групу стартовий знімок OLX, якщо є що надсилати)"
echo
venv/bin/python olx_watch.py || true
echo
echo "Готово. Mac перевірятиме OLX щодня о $HOUR:$(printf %02d $MINUTE)."
echo "Якщо Mac у цей час спить, перевірка виконається, щойно він прокинеться."
echo "Журнал: $DIR/logs/olx.log"
