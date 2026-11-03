#!/data/data/com.termux/files/usr/bin/bash
# Держит бота запущенным: при выходе/сбое перезапускает через минуту.
cd "$HOME/avito-bot" || exit 1
DATA="$HOME/avito-bot-data"
mkdir -p "$DATA"
while true; do
  python -m telegram_bot.bot >> "$DATA/console.log" 2>&1
  code=$?
  echo "$(date '+%F %T') бот завершился (код $code), перезапуск через 60 с" >> "$DATA/console.log"
  # Не даём журналу разрастаться: оставляем последние ~5000 строк.
  tail -n 5000 "$DATA/console.log" > "$DATA/console.log.tmp" && mv "$DATA/console.log.tmp" "$DATA/console.log"
  sleep 60
done
