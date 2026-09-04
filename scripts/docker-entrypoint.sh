#!/bin/bash
# Запускает браузер, потом само приложение.
#
# browser-use (плагин «Браузер») не поднимает браузер сам: он подключается к уже
# работающему Chromium по протоколу отладки. На настольной машине его открывает
# человек, а в контейнере открыть некому — поэтому это делаем мы, до старта
# интерфейса. Без этого browser_exec падает с «chrome-not-running».

set -u

CHROME_BIN="${CHROME_BIN:-/usr/bin/chromium}"
CHROME_PROFILE="${CHROME_PROFILE:-/root/.config/chromium}"
CHROME_PORT="${CHROME_CDP_PORT:-9222}"

if [ -x "$CHROME_BIN" ]; then
    mkdir -p "$CHROME_PROFILE"
    # --no-sandbox: внутри контейнера мы root, песочница Chromium там не работает
    # --disable-dev-shm-usage: /dev/shm в контейнере мал, иначе вкладки падают
    "$CHROME_BIN" \
        --headless=new \
        --remote-debugging-port="$CHROME_PORT" \
        --remote-debugging-address=127.0.0.1 \
        --no-sandbox \
        --disable-gpu \
        --disable-dev-shm-usage \
        --user-data-dir="$CHROME_PROFILE" \
        about:blank > /tmp/chromium.log 2>&1 &

    for _ in $(seq 1 20); do
        if curl -fs -o /dev/null --max-time 1 "http://127.0.0.1:$CHROME_PORT/json/version" 2>/dev/null; then
            echo "browser: Chromium готов на порту $CHROME_PORT"
            break
        fi
        sleep 0.5
    done
else
    echo "browser: Chromium не найден ($CHROME_BIN) — задачи с браузером работать не будут."
    echo "browser: пересоберите образ (docker compose build), чтобы он появился."
fi

exec "$@"
