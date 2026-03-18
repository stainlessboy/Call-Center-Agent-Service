#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — Production deployment for agent-bot.uz
#
#  Предполагается:
#   - Docker и docker compose plugin уже установлены на сервере
#   - Код уже скопирован на сервер (через git clone / scp / rsync)
#   - Запускать из папки с проектом: bash deploy.sh
# =============================================================================
set -euo pipefail

DOMAIN="agent-bot.uz"
EMAIL="stainlessclash@gmail.com"
COMPOSE_FILE="docker-compose.prod.yml"
LETSENCRYPT_LIVE_DIR="nginx/certbot/conf/live/$DOMAIN"

echo "============================================"
echo "  Deploy: $DOMAIN"
echo "============================================"

# ── 1. Проверка .env ──────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  Файл .env создан из .env.example."
  echo "   Заполни обязательные переменные и запусти скрипт снова:"
  echo ""
  echo "   nano .env"
  echo "   bash deploy.sh"
  echo ""
  echo "   Обязательные: BOT_TOKEN, OPENAI_API_KEY, ADMIN_PASSWORD, WEBHOOK_BASE_URL"
  exit 1
fi

# ── 2. Проверка обязательных переменных ──────────────────────────────────────
source .env
MISSING=()
[ -z "${BOT_TOKEN:-}" ] && MISSING+=("BOT_TOKEN")
[ -z "${OPENAI_API_KEY:-}" ] && MISSING+=("OPENAI_API_KEY")
[ -z "${ADMIN_PASSWORD:-}" ] && MISSING+=("ADMIN_PASSWORD")

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "❌ Не заполнены обязательные переменные в .env:"
  printf '   - %s\n' "${MISSING[@]}"
  echo ""
  echo "   nano .env"
  exit 1
fi

# ── 3. Создать директории nginx ──────────────────────────────────────────────
mkdir -p nginx/conf.d nginx/certbot/conf nginx/certbot/www

# ── 4. Запустить nginx (HTTP-only) для certbot challenge ─────────────────────
if [ ! -f "$LETSENCRYPT_LIVE_DIR/fullchain.pem" ]; then
  echo ""
  echo "▶ SSL-сертификат не найден. Получаем через Let's Encrypt..."
  echo ""

  echo "▶ Активируем HTTP-конфиг..."
  cp nginx/app-http.conf nginx/conf.d/app.conf

  echo "▶ Запуск nginx (HTTP)..."
  docker compose -f "$COMPOSE_FILE" up -d nginx
  sleep 3

  echo "▶ Получаем SSL-сертификат для $DOMAIN..."
  docker compose -f "$COMPOSE_FILE" run --rm --entrypoint certbot certbot \
    certonly --webroot \
    --webroot-path=/var/www/certbot \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

  if [ ! -f "$LETSENCRYPT_LIVE_DIR/fullchain.pem" ] || [ ! -f "$LETSENCRYPT_LIVE_DIR/privkey.pem" ]; then
    echo "❌ SSL-сертификат не найден в $LETSENCRYPT_LIVE_DIR"
    echo "   Проверь DNS домена и логи certbot:"
    echo "   docker compose -f $COMPOSE_FILE logs certbot"
    exit 1
  fi

  echo "✅ SSL-сертификат получен!"
else
  echo "▶ SSL-сертификат уже существует, пропускаем certbot."
fi

# ── 5. Переключить nginx на HTTPS-конфиг ─────────────────────────────────────
echo "▶ Активируем HTTPS конфиг..."
cp nginx/app-ssl.conf nginx/conf.d/app.conf

# ── 6. Поднять все сервисы ────────────────────────────────────────────────────
echo "▶ Запуск db, api, nginx..."
docker compose -f "$COMPOSE_FILE" up -d --build

echo "▶ Ждём готовности базы данных..."
docker compose -f "$COMPOSE_FILE" exec api python -c "import asyncio; asyncio.sleep(0)" 2>/dev/null || sleep 5

# ── 7. Миграции Alembic ──────────────────────────────────────────────────────
echo "▶ Применяем миграции..."
docker compose -f "$COMPOSE_FILE" exec -T api alembic upgrade head

# ── 8. Перезагрузить nginx с новым конфигом ──────────────────────────────────
echo "▶ Проверяем nginx конфиг..."
docker compose -f "$COMPOSE_FILE" exec nginx nginx -t

echo "▶ Перезагружаем nginx..."
docker compose -f "$COMPOSE_FILE" exec nginx nginx -s reload

# ── 9. Seed-данные (первый запуск) ───────────────────────────────────────────
echo ""
read -rp "▶ Загрузить seed-данные (продукты, FAQ, филиалы)? [y/N]: " SEED
if [[ "$SEED" =~ ^[Yy]$ ]]; then
  echo "▶ Загружаем продукты..."
  docker compose -f "$COMPOSE_FILE" exec -T api python scripts/seed_credit_product_offers.py --replace
  docker compose -f "$COMPOSE_FILE" exec -T api python scripts/seed_deposit_product_offers.py --replace
  docker compose -f "$COMPOSE_FILE" exec -T api python scripts/seed_card_product_offers.py --replace

  echo "▶ Загружаем FAQ..."
  docker compose -f "$COMPOSE_FILE" exec -T api python scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

  echo "▶ Загружаем филиалы..."
  docker compose -f "$COMPOSE_FILE" exec -T api python scripts/seed_branches.py --replace 2>/dev/null || echo "   (скрипт не найден, пропускаем)"

  echo "✅ Seed-данные загружены!"
fi

# ── Готово ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  ✅ Деплой завершён!"
echo "============================================"
echo ""
echo "  🌐 Сайт:    https://$DOMAIN"
echo "  📊 Health:   https://$DOMAIN/health"
echo "  🔧 Admin:    https://$DOMAIN/admin/"
echo "  🤖 Webhook:  https://$DOMAIN/telegram/webhook"
echo ""
echo "Полезные команды:"
echo "  make prod-logs       # логи"
echo "  make prod-status     # статус контейнеров"
echo "  make prod-restart    # рестарт API"
echo "  make prod-migrate    # миграции"
echo "  make prod-seed       # загрузить seed-данные"
echo "  make prod-down       # остановить всё"
echo ""
