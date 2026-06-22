.PHONY: up down build logs clean status urls cp-env restart

cp-env:
	@if [ ! -f .env ]; then cp .env.example .env && echo "✅ .env created — edit JWT_SECRET"; \
	else echo "ℹ️  .env already exists"; fi

up: cp-env
	docker compose up -d --build
	@echo ""
	@$(MAKE) urls

down:
	docker compose down

build:
	docker compose build --no-cache

logs:
	docker compose logs -f

logs-%:
	docker compose logs -f $*

restart:
	docker compose restart

clean:
	docker compose down -v --remove-orphans

status:
	docker compose ps

urls:
	@echo "────────────────────────────────────"
	@echo "  🌐 Frontend:  http://localhost:3000"
	@echo "  🔌 BFF API:   http://localhost:8080"
	@echo "  📊 NATS mon:  http://localhost:8222"
	@echo "  🐘 Postgres:  localhost:5432"
	@echo "────────────────────────────────────"
