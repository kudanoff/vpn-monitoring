.PHONY: up down logs restart deploy check backup

up:            ## Поднять хаб (первый этап: метрики панели + пробники)
	cd hub && docker compose up -d --build

up-nodes:      ## То же плюс рефлектор iperf3 — когда пойдёт второй этап
	cd hub && docker compose --profile nodes up -d --build

down:
	cd hub && docker compose down

logs:
	cd hub && docker compose logs -f --tail=100 bot

restart:
	cd hub && docker compose restart bot

sync-nodes:    ## Собрать targets/nodes.yml из API панели
	./scripts/sync-nodes.sh

deploy:        ## Раскатать агент на ноды: make deploy [LIMIT=nl-3]
	cd ansible && ansible-playbook -i inventory.yml playbook.yml $(if $(LIMIT),--limit $(LIMIT),)

test-alert:    ## Проверить доставку алертов до телеграма
	./scripts/test-alert.sh

check:         ## Проверить, что конфиги валидны и цели скрейпятся
	cd hub && docker compose exec vmagent wget -qO- http://localhost:8429/api/v1/targets | head -50

backup:        ## Снять слепок для переезда
	tar czf ../vpnmon-backup-$$(date +%F).tar.gz --exclude=hub/data/victoriametrics .
