#!/data/data/com.termux/files/usr/bin/bash
# Управление ботом на Android (Termux): avito start|stop|restart|status|log|update
APP="$HOME/avito-bot"
DATA="$HOME/avito-bot-data"
LOG="$DATA/console.log"
RUNNER="$APP/scripts/android/run.sh"

running() { pgrep -f "scripts/android/run.sh" >/dev/null 2>&1; }

start() {
  if running; then echo "Бот уже работает."; return; fi
  mkdir -p "$DATA"
  termux-wake-lock 2>/dev/null || true
  nohup bash "$RUNNER" >/dev/null 2>&1 &
  echo "Бот запущен."
}

stop() {
  pkill -f "scripts/android/run.sh" 2>/dev/null || true
  pkill -f "telegram_bot.bot" 2>/dev/null || true
  termux-wake-unlock 2>/dev/null || true
  echo "Бот остановлен."
}

case "$1" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 2; start ;;
  status)
    if running; then echo "🟢 Бот работает."; else echo "🔴 Бот не запущен. Команда: avito start"; fi ;;
  log) tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "Журнала пока нет." ;;
  update)
    stop
    branch="$(git -C "$APP" rev-parse --abbrev-ref HEAD)"
    git -C "$APP" fetch --depth 1 origin "$branch" && git -C "$APP" reset --hard FETCH_HEAD
    pip install --disable-pip-version-check -q -r "$APP/requirements-android.txt"
    start ;;
  *) echo "Использование: avito start|stop|restart|status|log|update" ;;
esac
