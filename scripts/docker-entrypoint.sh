#!/bin/bash
# Запускает браузер, потом само приложение.
#
# browser-use (плагин «Браузер») не поднимает браузер сам: он подключается к уже
# работающему Chromium по протоколу отладки. На настольной машине его открывает
# человек, а в контейнере открыть некому — поэтому это делаем мы, до старта
# интерфейса. Без этого browser_exec падает с «chrome-not-running».
#
# Главное правило этого файла: что бы здесь ни случилось, приложение обязано
# запуститься. Браузер — лишь один из инструментов, и его поломка не должна
# лишать человека интерфейса целиком.

set -u

CHROME_BIN="${CHROME_BIN:-/usr/bin/chromium}"
CHROME_PROFILE="${CHROME_PROFILE:-/root/.config/chromium}"
CHROME_PORT="${CHROME_CDP_PORT:-9222}"
CHROME_LANG="${CHROME_LANG:-en-US,en,ru}"
SCREEN="${CHROME_SCREEN:-1920x1080x24}"
DISPLAY_NUM="${CHROME_DISPLAY:-:99}"
VNC_PORT="${VNC_PORT:-5900}"
VIEW_PORT="${BROWSER_VIEW_PORT:-6080}"
NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"

# Обычный (не headless) режим на виртуальном экране Xvfb. Headless-браузер
# отличается от настоящего десятком признаков, и защита сайтов узнаёт его
# сразу; виртуальный экран снимает самый грубый слой этих отличий.
#
# Включается переменной BROWSER_HEADFUL=1 (её выставляет docker-compose.yml).
# Здесь значение по умолчанию — 0, чтобы скрипт оставался безопасным и при
# запуске в одиночку. Любая неудача с экраном откатывает нас в headless:
# браузер поднимется в любом случае.
HEADFUL="${BROWSER_HEADFUL:-0}"

SCREEN_W="${SCREEN%%x*}"
SCREEN_REST="${SCREEN#*x}"
SCREEN_H="${SCREEN_REST%%x*}"

start_screen() {
    [ "$HEADFUL" = "1" ] || return 1
    command -v Xvfb >/dev/null 2>&1 || {
        echo "browser: BROWSER_HEADFUL=1, но Xvfb в образе нет — остаюсь в headless"
        return 1
    }
    # Xvfb уводим в отдельную сессию и явно возвращаем сигналу уведомления
    # поведение по умолчанию: так он никого не разбудит и ничего не уронит.
    python3 -c "
import os, signal, sys
signal.signal(signal.SIGUSR1, signal.SIG_DFL)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
" Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -nolisten tcp > /tmp/xvfb.log 2>&1 &
    for _ in $(seq 1 24); do
        if xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then
            export DISPLAY="$DISPLAY_NUM"
            return 0
        fi
        sleep 0.25
    done
    echo "browser: виртуальный экран не поднялся — остаюсь в headless"
    return 1
}

start_browser() {
    if [ ! -x "$CHROME_BIN" ]; then
        echo "browser: Chromium не найден ($CHROME_BIN) — задачи с браузером работать не будут."
        echo "browser: пересоберите образ (docker compose build), чтобы он появился."
        return 0
    fi

    mkdir -p "$CHROME_PROFILE"
    if start_screen; then
        HEADLESS_FLAG=""
        echo "browser: обычный режим на виртуальном экране $SCREEN"
    else
        HEADLESS_FLAG="--headless=new"
    fi

    # --no-sandbox: внутри контейнера мы root, песочница Chromium там не работает
    # --disable-dev-shm-usage: /dev/shm в контейнере мал, иначе вкладки падают
    # --disable-blink-features=AutomationControlled: убирает флаг «мной управляют
    #   программно», по которому отсеивают ещё до загрузки страницы
    "$CHROME_BIN" \
        $HEADLESS_FLAG \
        --remote-debugging-port="$CHROME_PORT" \
        --remote-debugging-address=127.0.0.1 \
        --no-sandbox \
        --disable-gpu \
        --disable-dev-shm-usage \
        --disable-blink-features=AutomationControlled \
        --no-first-run \
        --no-default-browser-check \
        --window-size="$SCREEN_W,$SCREEN_H" \
        --lang="${CHROME_LANG%%,*}" \
        --accept-lang="$CHROME_LANG" \
        --user-data-dir="$CHROME_PROFILE" \
        about:blank > /tmp/chromium.log 2>&1 &

    for _ in $(seq 1 20); do
        if curl -fs -o /dev/null --max-time 1 "http://127.0.0.1:$CHROME_PORT/json/version" 2>/dev/null; then
            echo "browser: Chromium готов на порту $CHROME_PORT"
            return 0
        fi
        sleep 0.5
    done
    echo "browser: Chromium не отозвался за 10 с — смотрите /tmp/chromium.log"
}

start_live_view() {
    # Живой вид на экран браузера: человек через него входит на закрытые
    # сайты своими руками. Без виртуального экрана показывать нечего, а без
    # x11vnc/websockify — нечем; в обоих случаях просто молчим, интерфейс
    # честно скажет в окне входа, что вида нет.
    [ -n "${DISPLAY:-}" ] || return 0
    command -v x11vnc >/dev/null 2>&1 || return 0
    command -v websockify >/dev/null 2>&1 || return 0

    # -localhost: наружу порт отдаёт только docker-compose, и только на
    # 127.0.0.1. -nopw без этого означал бы открытый доступ к вашим сессиям.
    x11vnc -display "$DISPLAY" -forever -shared -nopw -localhost -quiet \
        -rfbport "$VNC_PORT" > /tmp/x11vnc.log 2>&1 &

    for _ in $(seq 1 20); do
        if curl -fs -o /dev/null --max-time 1 "http://127.0.0.1:$VNC_PORT" 2>/dev/null \
           || nc -z 127.0.0.1 "$VNC_PORT" 2>/dev/null; then break; fi
        sleep 0.25
    done

    websockify --web "$NOVNC_DIR" "$VIEW_PORT" "127.0.0.1:$VNC_PORT" \
        > /tmp/websockify.log 2>&1 &

    for _ in $(seq 1 20); do
        if curl -fs -o /dev/null --max-time 1 "http://127.0.0.1:$VIEW_PORT/vnc.html"; then
            echo "browser: живой вид доступен на порту $VIEW_PORT"
            return 0
        fi
        sleep 0.25
    done
    echo "browser: живой вид не поднялся — смотрите /tmp/websockify.log"
}

# Ошибка внутри запуска браузера не должна помешать старту приложения.
start_browser || echo "browser: запуск браузера не удался, продолжаю без него"
start_live_view || echo "browser: живой вид не запустился, вход руками будет недоступен"

exec "$@"
