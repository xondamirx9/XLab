#!/bin/bash
#
# Выкладка версии на машину Codex Lab (UzCloud): запустить образ, дождаться
# здоровья; не поднялась — вернуть прежнюю версию и выйти с ошибкой.
#
#   WEB_IP=… ADMIN_IP=… SITE_DOMAIN=… release.sh web|admin <версия>
#
# Запускает кнопка релиза (.github/workflows/release.yml) от пользователя
# deploy, сначала на машине админки, потом на машине сайта (README рядом,
# «Релиз»). Образ codexlab:<версия> к этому времени уже загружен на машину,
# файлы выкладки — в /opt/codexlab/deploy, настройки сайта — в
# /opt/codexlab/env/app.env.
#
# Пароль базы создаётся на машине админки при первой выкладке
# (env/db.env) и уходит только на машину сайта — его переносит кнопка
# релиза, не показывая в журнале.
#
# Машина админки ещё ставит в расписание копии базы (backup.sh).
set -euo pipefail

ROLE=${1:?роль: web или admin}
TAG=${2:?версия образа}
APP_HOME=${CODEXLAB_HOME:-/opt/codexlab}
DEPLOY=$APP_HOME/deploy
ENV_DIR=$APP_HOME/env
: "${WEB_IP:?частный адрес машины сайта}" "${ADMIN_IP:?частный адрес машины админки}"
SITE_DOMAIN=${SITE_DOMAIN:-new.codexlab.uz}
LIVE_DOMAIN=codexlab.uz
IMAGE=codexlab
WAIT=240                 # секунд на здоровье: машина сайта ждёт схему базы до 2 минут

case "$ROLE" in
    web) services=(web) ;;
    admin) services=(db admin) ;;
    *) echo "Роль — web или admin, а не «$ROLE»" >&2; exit 1 ;;
esac
if ! [[ "$TAG" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "Версия — начало хеша коммита, а не «$TAG»" >&2
    exit 1
fi

umask 077
mkdir -p "$ENV_DIR"
COMPOSE=(docker compose --progress quiet -p codexlab -f "$DEPLOY/compose.$ROLE.yml" --env-file "$ENV_DIR/compose.env")

if [ ! -s "$ENV_DIR/app.env" ]; then
    echo "ОШИБКА: нет $ENV_DIR/app.env — его пишет кнопка релиза из секретов GitHub." >&2
    exit 1
fi
if ! docker image inspect "$IMAGE:$TAG" > /dev/null 2>&1; then
    echo "ОШИБКА: на машине нет образа $IMAGE:$TAG — его загружает кнопка релиза." >&2
    exit 1
fi

# Частный адрес этой машины: на нём слушают база и сайт админки, с него
# машина сайта ходит в базу
MY_IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
want=$WEB_IP
if [ "$ROLE" = admin ]; then
    want=$ADMIN_IP
fi
if [ "$MY_IP" != "$want" ]; then
    echo "ОШИБКА: у машины ($ROLE) частный адрес $MY_IP, а в GitHub — $want (переменные PROD_WEB_IP и PROD_ADMIN_IP)." >&2
    exit 1
fi

# ── Пароль базы ────────────────────────────────────────────────────
if [ ! -s "$ENV_DIR/db.env" ]; then
    if [ "$ROLE" = web ]; then
        echo "ОШИБКА: нет $ENV_DIR/db.env — его переносит кнопка релиза с машины админки." >&2
        exit 1
    fi
    # Только буквы и цифры: пароль стоит в адресе базы
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$ENV_DIR/db.env"
    echo "── Пароль базы создан ($ENV_DIR/db.env)"
fi
# shellcheck disable=SC1091
. "$ENV_DIR/db.env"
: "${POSTGRES_PASSWORD:?нет пароля в db.env}"

if [ "$ROLE" = web ] && ! timeout 5 bash -c "exec 3<>/dev/tcp/$ADMIN_IP/5432" 2>/dev/null; then
    echo "ОШИБКА: база на машине админки ($ADMIN_IP:5432) не отвечает — сначала выкладывается машина админки." >&2
    exit 1
fi

# ── Настройки compose ──────────────────────────────────────────────
previous=$(sed -n 's/^TAG=//p' "$ENV_DIR/compose.env" 2>/dev/null || true)
hosts=$SITE_DOMAIN
robots="-X-Robots-Tag"
if [ "$SITE_DOMAIN" = "$LIVE_DOMAIN" ]; then
    hosts="$LIVE_DOMAIN, www.$LIVE_DOMAIN"
else
    # Репетиция (new.codexlab.uz): поисковики сайт не берут, иначе в
    # выдаче появился бы второй codexlab
    robots='X-Robots-Tag "noindex, nofollow"'
fi

write_env() {
    {
        echo "# Пишет release.sh при каждой выкладке ($ROLE, $(date -Iseconds))"
        echo "CODEXLAB_HOME=$APP_HOME"
        echo "IMAGE=$IMAGE"
        echo "TAG=$1"
        echo "WEB_IP=$WEB_IP"
        echo "ADMIN_IP=$ADMIN_IP"
        echo "SITE_HOSTS=$hosts"
        echo "ROBOTS_HEADER=$robots"
        echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD"
    } > "$ENV_DIR/compose.env.new"
    mv "$ENV_DIR/compose.env.new" "$ENV_DIR/compose.env"
}

# healthy <секунд> — все службы роли здоровы
healthy() {
    local deadline=$(( $(date +%s) + $1 )) service id state ok
    while :; do
        ok=1
        for service in "${services[@]}"; do
            id=$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)
            state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)
            if [ "$state" != healthy ]; then
                ok=0
            fi
        done
        if [ "$ok" = 1 ]; then
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            return 1
        fi
        sleep 5
    done
}

# Последние строки журнала службы, которая не поднялась, — в журнал релиза.
# Журналы Actions публичные: адреса и почты вычеркнуты
tail_logs() {
    local service id state
    for service in "${services[@]}"; do
        id=$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)
        state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)
        if [ "$state" = healthy ]; then
            continue
        fi
        echo "── Журнал $service ($state, последние строки):" >&2
        "${COMPOSE[@]}" logs --no-color --tail 25 "$service" 2>&1 \
            | sed -E 's/([0-9]{1,3}\.){3}[0-9]{1,3}/[адрес]/g; s/[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}/[почта]/g' >&2 || true
    done
}

echo "── $ROLE: версия $TAG${previous:+ (была $previous)}"
write_env "$TAG"
"${COMPOSE[@]}" up -d --remove-orphans
if ! healthy "$WAIT"; then
    tail_logs
    if [ -n "$previous" ] && [ "$previous" != "$TAG" ] && docker image inspect "$IMAGE:$previous" > /dev/null 2>&1; then
        echo "ОШИБКА: версия $TAG не поднялась за $WAIT с — возвращаю $previous." >&2
        write_env "$previous"
        "${COMPOSE[@]}" up -d --remove-orphans
        if healthy "$WAIT"; then
            echo "Прежняя версия $previous снова работает." >&2
        else
            echo "ОШИБКА: и прежняя версия $previous не поднялась." >&2
        fi
    else
        echo "ОШИБКА: версия $TAG не поднялась за $WAIT с, вернуть нечего." >&2
    fi
    exit 1
fi

if [ "$ROLE" = web ]; then
    # Caddyfile мог измениться, а контейнер Caddy compose пересоздаёт, только
    # если поменялись его настройки — перечитать файл без обрыва соединений
    if ! out=$("${COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1); then
        echo "$out" >&2
        echo "ОШИБКА: Caddy не принял новый Caddyfile — работает прежний." >&2
        exit 1
    fi
fi

if [ "$ROLE" = admin ]; then
    echo "── Копии базы — в расписание"
    # Время — по часам машины (Ташкент, bootstrap.sh). Строки Codex Lab в
    # crontab заменяются целиком, чужие не трогаются
    log=$APP_HOME/backup/backup.log
    mkdir -p "$APP_HOME/backup"
    {
        crontab -l 2>/dev/null | grep -v '# codexlab-backup' || true
        echo "40 3 * * * bash $DEPLOY/backup.sh db >> $log 2>&1 # codexlab-backup"
        # После ночной копии: поднимается ли она
        echo "10 5 * * * bash $DEPLOY/backup.sh check >> $log 2>&1 # codexlab-backup"
    } | crontab -
    if [ ! -s "$ENV_DIR/backup.env" ]; then
        echo "ВНИМАНИЕ: нет $ENV_DIR/backup.env — копии в хранилище не пойдут (секреты S3_* и BACKUP_PASSPHRASE в GitHub)." >&2
    fi
fi

# Старые образы: на машине остаются эта версия и две предыдущие — для отката
docker image ls "$IMAGE" --format '{{.Tag}}' | grep -vxF "$TAG" | grep -vxF "${previous:-}" \
    | tail -n +2 | while read -r old; do
        docker image rm "$IMAGE:$old" > /dev/null 2>&1 || true
    done || true

echo "── Готово: $ROLE на версии $TAG"
