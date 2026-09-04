#!/bin/bash
# Запуск Манус: поднимает Docker, если он спит, стартует контейнер и открывает
# интерфейс в браузере. Двойной клик — остальное скрипт делает сам.
#
# Если OpenManus лежит не в домашней папке, поправьте строку REPO ниже.

REPO="$HOME/OpenManus"
URL="http://localhost:8000"
DOCKER_WAIT=120   # сколько секунд ждать, пока Docker Desktop проснётся

set -u

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  %s\n' "$1"; }
stop() {
    printf '\n\033[31m✗ %s\033[0m\n' "$1"
    printf '\nНажмите любую клавишу, чтобы закрыть окно…'
    read -r -n 1 -s
    printf '\n'
    exit 1
}

clear
say "Запуск Манус"

cd "$REPO" 2>/dev/null || stop "Не нашёл папку $REPO. Если OpenManus лежит в другом месте, откройте этот файл в TextEdit и поправьте строку REPO."

# --- Docker: сам разбудим, если спит ---------------------------------------
command -v docker >/dev/null 2>&1 || stop "Docker не установлен. Скачайте Docker Desktop с docker.com и поставьте."

if docker info >/dev/null 2>&1; then
    ok "Docker уже работает"
else
    say "Docker спит — бужу Docker Desktop…"
    open -a Docker 2>/dev/null || stop "Не нашёл приложение Docker Desktop. Откройте его вручную и запустите этот файл снова."
    printf '  Жду, пока он поднимется (обычно 20–60 секунд)'
    ready=""
    for _ in $(seq 1 $((DOCKER_WAIT / 2))); do
        sleep 2
        printf '.'
        if docker info >/dev/null 2>&1; then ready="yes"; break; fi
    done
    printf '\n'
    [ -n "$ready" ] || stop "Docker не поднялся за $DOCKER_WAIT секунд. Откройте Docker Desktop вручную, дождитесь зелёного значка кита и запустите этот файл снова."
    ok "Docker готов"
fi

# --- контейнер --------------------------------------------------------------
say "Запускаю Манус…"
docker compose up -d || stop "Контейнер не запустился. Покажите Клоду строки выше."

# --- ждём, пока интерфейс ответит ------------------------------------------
printf '  Жду интерфейс'
for _ in $(seq 1 40); do
    if curl -fs -o /dev/null --max-time 2 "$URL/api/config" 2>/dev/null; then
        printf '\n'
        say "Готово — открываю $URL"
        open "$URL" >/dev/null 2>&1
        printf '\nОкно можно закрыть, Манус продолжит работать.\n'
        printf 'Остановить его — файлом «Стоп Манус».\n'
        printf '\nНажмите любую клавишу, чтобы закрыть окно…'
        read -r -n 1 -s
        printf '\n'
        exit 0
    fi
    sleep 1
    printf '.'
done

printf '\n'
say "Контейнер запущен, но интерфейс пока не отвечает"
ok "Это бывает при первом запуске после сборки. Подождите полминуты и откройте $URL сами."
ok "Если не появится — покажите Клоду вывод команды: docker compose logs --tail 50"
printf '\nНажмите любую клавишу, чтобы закрыть окно…'
read -r -n 1 -s
printf '\n'
