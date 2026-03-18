COMPOSE_PROD := docker compose -f docker-compose.prod.yml
COMPOSE_DEV  := docker compose

.PHONY: help dev dev-down prod-deploy prod-update prod-logs prod-down prod-restart \
        prod-status prod-migrate prod-seed prod-shell prod-renew-ssl test

help:
	@echo ""
	@echo "  agent-bot.uz — Makefile"
	@echo "  ─────────────────────────────────────────"
	@echo ""
	@echo "  Development:"
	@echo "    make dev           Start local PostgreSQL"
	@echo "    make dev-down      Stop local PostgreSQL"
	@echo "    make test          Run pytest"
	@echo "    make migrate       Run alembic upgrade head (local)"
	@echo ""
	@echo "  Production:"
	@echo "    make prod-deploy   First-time deploy (SSL + build + migrate)"
	@echo "    make prod-update   Rebuild, migrate, reload nginx"
	@echo "    make prod-logs     Tail api + nginx logs"
	@echo "    make prod-restart  Restart api, reload nginx"
	@echo "    make prod-status   Show container statuses"
	@echo "    make prod-migrate  Run alembic upgrade head"
	@echo "    make prod-seed     Load seed data (products, FAQ)"
	@echo "    make prod-shell    Open bash in api container"
	@echo "    make prod-down     Stop all prod containers"
	@echo "    make prod-renew-ssl  Renew SSL certificate"
	@echo ""

# ── Development ──────────────────────────────────────────────────────────────

dev:
	$(COMPOSE_DEV) up -d

dev-down:
	$(COMPOSE_DEV) down

test:
	python3 -m pytest tests/test_agent.py -v

migrate:
	alembic upgrade head

# ── Production ───────────────────────────────────────────────────────────────

prod-deploy:
	bash deploy.sh

prod-update:
	$(COMPOSE_PROD) up -d --build
	$(COMPOSE_PROD) exec -T api alembic upgrade head
	$(COMPOSE_PROD) exec -T nginx nginx -s reload

prod-logs:
	$(COMPOSE_PROD) logs -f api nginx

prod-down:
	$(COMPOSE_PROD) down

prod-restart:
	$(COMPOSE_PROD) restart api
	$(COMPOSE_PROD) exec -T nginx nginx -s reload

prod-status:
	$(COMPOSE_PROD) ps

prod-migrate:
	$(COMPOSE_PROD) exec -T api alembic upgrade head

prod-seed:
	$(COMPOSE_PROD) exec -T api python scripts/seed_credit_product_offers.py --replace
	$(COMPOSE_PROD) exec -T api python scripts/seed_deposit_product_offers.py --replace
	$(COMPOSE_PROD) exec -T api python scripts/seed_card_product_offers.py --replace
	$(COMPOSE_PROD) exec -T api python scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

prod-shell:
	$(COMPOSE_PROD) exec api bash

prod-renew-ssl:
	$(COMPOSE_PROD) run --rm --entrypoint certbot certbot renew
	$(COMPOSE_PROD) exec nginx nginx -s reload
