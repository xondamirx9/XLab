#!/bin/bash
#
# Подготовка чистой машины Ubuntu 24.04 в UzCloud под Codex Lab.
#
# Запускается от root из кнопки релиза (.github/workflows/release.yml,
# галочка «подготовить машины») по SSH-ключу, вписанному в машину при
# создании. Повторный запуск ничего не ломает: каждый шаг проверяет,
# сделан ли он уже.
#
#   CODEXLAB_ROLE=web|admin WEB_IP=<частный адрес машины сайта> bash bootstrap.sh
#
# - web — машина сайта: снаружи открыты HTTP и HTTPS;
# - admin — машина админки: база (5432) и сайт админки (8080) — только
#   с частного адреса машины сайта (WEB_IP).
#
# Что делает:
# - обновления: всё сейчас и дальше автоматически (исправления
#   безопасности, без перезагрузки); часовой пояс — Ташкент;
# - Docker и docker compose из пакетов Ubuntu, журналы контейнеров с
#   ротацией — иначе однажды они съедят диск;
# - файрвол ufw. Контейнеры работают в сети машины (network_mode: host),
#   портов Docker не публикует, поэтому ufw закрывает и их;
# - SSH только по ключу, fail2ban банит перебор;
# - пользователь deploy (без пароля, ключ — тот же, которым вошли): под
#   ним кнопка релиза выкладывает сайт;
# - каталог /opt/codexlab (настройки — env/, права 700);
# - подкачка 2 ГБ: у машин по 2 ГБ памяти;
# - машине админки — gpg и cron для резервных копий базы (backup.sh).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужен root: sudo bash bootstrap.sh" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
APP_HOME=/opt/codexlab
ROLE=${CODEXLAB_ROLE:?роль машины: web или admin}

case "$ROLE" in
    web) ;;
    admin)
        if [ -z "${WEB_IP:-}" ]; then
            echo "Машине админки нужен частный адрес машины сайта: WEB_IP" >&2
            exit 1
        fi
        ;;
    *)
        echo "Неизвестная роль: $ROLE (web или admin)" >&2
        exit 1
        ;;
esac
echo "── Роль машины: $ROLE"

extra_packages=()
if [ "$ROLE" = admin ]; then
    extra_packages=(gnupg cron)
fi

echo "── Часовой пояс и обновления"
timedatectl set-timezone Asia/Tashkent 2>/dev/null || true
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
apt-get -y -q install --no-install-recommends \
    docker.io docker-compose-v2 ufw fail2ban unattended-upgrades ca-certificates curl \
    "${extra_packages[@]}"

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF

echo "── Docker: ротация журналов контейнеров"
mkdir -p /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
    cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
JSON
    systemctl restart docker
fi
systemctl enable --now docker

echo "── Подкачка"
if ! swapon --show | grep -q .; then
    [ -f /swapfile ] || fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    if swapon /swapfile; then
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    else
        # Без подкачки сайт работает, только запаса памяти меньше
        rm -f /swapfile
        echo "ВНИМАНИЕ: подкачка не включилась — машина работает без неё." >&2
    fi
fi

echo "── Пользователь deploy"
if ! id deploy >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash deploy
fi
usermod -aG docker deploy
passwd -l deploy >/dev/null

# Ключ — тот же, которым вошли (root или пользователь образа через sudo)
source_home=/root
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    source_home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
fi
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
if [ -s "$source_home/.ssh/authorized_keys" ]; then
    touch /home/deploy/.ssh/authorized_keys
    # Дописать только новые строки: повторный запуск не плодит дубли
    sort -u "$source_home/.ssh/authorized_keys" /home/deploy/.ssh/authorized_keys \
        -o /home/deploy/.ssh/authorized_keys
fi
chown deploy:deploy /home/deploy/.ssh/authorized_keys 2>/dev/null || true
chmod 600 /home/deploy/.ssh/authorized_keys 2>/dev/null || true

echo "── Каталог $APP_HOME"
install -d -m 755 -o deploy -g deploy "$APP_HOME" "$APP_HOME/deploy"
install -d -m 700 -o deploy -g deploy "$APP_HOME/env"
if [ "$ROLE" = admin ]; then
    # Каталог базы — 755: образ postgres входит в него ещё до смены
    # владельца, а сами данные (pg/18/docker) PostgreSQL закрывает
    # правами 700 сам
    install -d -m 755 -o root -g root "$APP_HOME/pg"
    install -d -m 700 -o deploy -g deploy "$APP_HOME/backup"
fi

echo "── SSH: только по ключу"
cat > /etc/ssh/sshd_config.d/10-codexlab.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
# Ubuntu 24.04 запускает sshd по сокету (ssh.socket): каталога /run/sshd
# может не быть, и без него `sshd -t` отказывается проверять настройки
install -d -m 755 /run/sshd
sshd -t
if systemctl is-active --quiet ssh; then
    systemctl reload ssh
elif systemctl is-active --quiet sshd; then
    systemctl reload sshd
fi

echo "── fail2ban"
systemctl enable --now fail2ban

echo "── Файрвол"
ufw allow OpenSSH >/dev/null
case "$ROLE" in
    web)
        ufw allow 80/tcp >/dev/null
        ufw allow 443/tcp >/dev/null
        ufw allow 443/udp >/dev/null
        ;;
    admin)
        # База и сайт админки — только машине сайта: её сайту (база) и её
        # Caddy (адреса админки)
        ufw allow from "$WEB_IP" to any port 5432 proto tcp >/dev/null
        ufw allow from "$WEB_IP" to any port 8080 proto tcp >/dev/null
        ;;
esac
ufw default deny incoming >/dev/null
ufw --force enable >/dev/null
ufw reload >/dev/null

echo "── Готово: $(lsb_release -ds 2>/dev/null || echo Linux), $(docker --version), $(docker compose version)"
echo "Ключ этой машины (для known_hosts в GitHub):"
cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub
