#!/data/data/com.termux/files/usr/bin/bash
# Установка Avito Monitor Bot на Android (Termux). Запуск одной командой:
#   curl -fsSL https://raw.githubusercontent.com/agutenok-max/avito-parser/claude/avito-telegram-bot-om1luy/scripts/android/install.sh | bash
set -e

REPO="${AVITO_REPO:-https://github.com/agutenok-max/avito-parser.git}"
BRANCH="${AVITO_BRANCH:-claude/avito-telegram-bot-om1luy}"
APP="$HOME/avito-bot"
DATA="$HOME/avito-bot-data"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$1"; }

if [ -z "$PREFIX" ] || [ ! -d "$PREFIX" ]; then
  echo "Этот скрипт нужно запускать в приложении Termux на Android." >&2
  exit 1
fi

say "1/5 Обновляю Termux и ставлю Python (5–10 минут, телефон не блокируйте)"
export DEBIAN_FRONTEND=noninteractive
yes | pkg update -y -o Dpkg::Options::=--force-confnew >/dev/null 2>&1 || true
pkg install -y -o Dpkg::Options::=--force-confnew python git procps
pkg install -y python-lxml >/dev/null 2>&1 || echo "(lxml не установлен — будет использован встроенный разбор страниц)"

say "2/5 Скачиваю бота"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --depth 1 origin "$BRANCH"
  git -C "$APP" checkout -B "$BRANCH" FETCH_HEAD
else
  git clone --depth 1 -b "$BRANCH" "$REPO" "$APP"
fi

say "3/5 Устанавливаю библиотеки"
pip install --disable-pip-version-check -q -r "$APP/requirements-android.txt"

say "4/5 Настройки"
mkdir -p "$DATA"
if [ -f "$APP/.env" ] && grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$APP/.env"; then
  echo "Настройки уже есть ($APP/.env) — оставляю как есть."
else
  echo "Вставьте токен бота от @BotFather (долгое нажатие → Paste) и нажмите Enter:"
  read -r TOKEN < /dev/tty
  echo "Введите ваш Telegram ID (число из переменной TELEGRAM_ADMIN_ID в Amvera) и нажмите Enter:"
  read -r ADMIN < /dev/tty
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  ADMIN="$(printf '%s' "$ADMIN" | tr -d '[:space:]')"
  umask 077
  cat > "$APP/.env" <<CONF
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_ADMIN_ID=$ADMIN
FETCH_MODE=http
DATA_DIR=$DATA
TZ=Europe/Moscow
CONF
  echo "Сохранено в $APP/.env (доступно только Termux)."
fi

say "5/5 Команда управления и автозапуск"
cat > "$PREFIX/bin/avito" <<CMD
#!/data/data/com.termux/files/usr/bin/bash
exec bash "$APP/scripts/android/ctl.sh" "\$@"
CMD
chmod 700 "$PREFIX/bin/avito"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/avito-bot.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
"$PREFIX/bin/avito" start
BOOT
chmod 700 "$HOME/.termux/boot/avito-bot.sh"

avito restart
sleep 20
avito log

cat <<DONE

Готово. Бот запущен и будет перезапускаться сам при сбоях.
Команды Termux:
  avito status   — работает ли бот
  avito log      — последние строки журнала
  avito restart  — перезапустить
  avito update   — скачать новую версию и перезапустить
  avito stop     — остановить
DONE
