.PHONY: up down logs health ps build

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f

health:
	@echo "--- Backend ---"
	@curl -sf http://localhost:8001/health | python3 -m json.tool || echo "UNHEALTHY"
	@echo "\n--- Backend Readiness ---"
	@curl -sf http://localhost:8001/ready | python3 -m json.tool || echo "UNHEALTHY"
	@echo "\n--- Frontend ---"
	@curl -sf http://localhost:3001 > /dev/null && echo "OK" || echo "UNHEALTHY"

ps:
	docker compose ps

build:
	docker compose build
