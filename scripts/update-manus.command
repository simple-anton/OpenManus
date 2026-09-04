#!/bin/bash
# Обновление Манус: забрать новую версию и пересобрать контейнер.
# Двойной клик по файлу — остальное скрипт делает сам.
#
# Если OpenManus лежит не в домашней папке, поправьте строку REPO ниже.

REPO="$HOME/OpenManus"
REMOTE="web"
REMOTE_URL="https://github.com/simple-anton/OpenManus.git"
BRANCH="claude/open-manus-web-interface-rjjcda"

set -u

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  %s\n' "$1"; }
stop() {
    printf '\n\033[31m✗ %s\033[0m\n' "$1"
    printf '\nМанус остался на прежней версии — ничего не сломалось.\n'
    printf 'Если непонятно, что делать, покажите этот текст Клоду.\n'
    printf '\nНажмите любую клавишу, чтобы закрыть окно…'
    read -r -n 1 -s
    printf '\n'
    exit 1
}

clear
say "Обновление Манус"

# --- 1. папка проекта -------------------------------------------------------
cd "$REPO" 2>/dev/null || stop "Не нашёл папку $REPO. Если OpenManus лежит в другом месте, откройте этот файл в TextEdit и поправьте строку REPO."
git rev-parse --git-dir >/dev/null 2>&1 || stop "В папке $REPO нет git-репозитория."
ok "Папка проекта: $REPO"

# --- 2. Docker --------------------------------------------------------------
say "Проверяю Docker…"
command -v docker >/dev/null 2>&1 || stop "Docker не установлен."
docker info >/dev/null 2>&1 || stop "Docker не запущен. Откройте Docker Desktop, дождитесь зелёного значка внизу слева и запустите этот файл снова."
ok "Docker работает"

# --- 3. адрес репозитория ---------------------------------------------------
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
    ok "Добавляю адрес «$REMOTE»…"
    git remote add "$REMOTE" "$REMOTE_URL" || stop "Не смог добавить адрес репозитория."
fi

# --- 4. свои правки не теряем ----------------------------------------------
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    say "В файлах проекта есть несохранённые правки — откладываю их в сторону"
    git stash push -m "перед обновлением $(date '+%Y-%m-%d %H:%M')" >/dev/null || stop "Не смог отложить правки."
    ok "Отложено. Вернуть потом: git stash pop"
fi

# --- 5. скачиваем -----------------------------------------------------------
say "Скачиваю новую версию…"
git fetch "$REMOTE" "$BRANCH" 2>&1 | sed 's/^/  /'
git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null || stop "Не смог скачать обновление. Проверьте интернет."

BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo '')"
TARGET="$(git rev-parse --short "$REMOTE/$BRANCH")"

# локальные коммиты, которых нет в репозитории, молча не выбрасываем
if git rev-parse --verify --quiet "$BRANCH" >/dev/null; then
    EXTRA="$(git rev-list --count "$REMOTE/$BRANCH..$BRANCH" 2>/dev/null || echo 0)"
    if [ "${EXTRA:-0}" -gt 0 ]; then
        say "Внимание: в вашей ветке есть $EXTRA собственных коммитов, которых нет в репозитории:"
        git log --oneline --no-decorate "$REMOTE/$BRANCH..$BRANCH" | sed 's/^/  /'
        printf '\nОбновление их перезапишет. Продолжить? (д/н) '
        read -r ANSWER
        case "$ANSWER" in
            д|Д|y|Y|да|Да) ;;
            *) stop "Отменено вами." ;;
        esac
    fi
fi

if [ "$BEFORE" = "$TARGET" ]; then
    say "Новых изменений нет — у вас уже последняя версия ($TARGET)"
    printf '\nНажмите любую клавишу, чтобы закрыть окно…'
    read -r -n 1 -s
    printf '\n'
    exit 0
fi

git checkout -B "$BRANCH" "$REMOTE/$BRANCH" 2>&1 | sed 's/^/  /'
AFTER="$(git rev-parse --short HEAD)"
[ "$AFTER" = "$TARGET" ] || stop "Не смог переключиться на новую версию."

say "Новая версия: $AFTER (было ${BEFORE:-неизвестно}). Что нового:"
git log --oneline --no-decorate "${BEFORE}..${AFTER}" 2>/dev/null | sed 's/^/  /' || true

# --- 6. пересборка ----------------------------------------------------------
say "Пересобираю контейнер…"
printf '  Обычно секунды. Если менялся список библиотек — несколько минут, это нормально.\n\n'
docker compose build || stop "Сборка не прошла. Скопируйте последние строки выше и покажите Клоду."

say "Запускаю…"
docker compose up -d || stop "Контейнер не запустился. Покажите Клоду строки выше."

# --- 7. готово --------------------------------------------------------------
say "Готово. Версия $AFTER"
ok "Открываю http://localhost:8000"
open "http://localhost:8000" >/dev/null 2>&1

printf '\nЕсли интерфейс выглядит по-старому — обновите страницу: Cmd+Shift+R.\n'
printf '\nНажмите любую клавишу, чтобы закрыть окно…'
read -r -n 1 -s
printf '\n'
