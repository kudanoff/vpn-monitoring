.PHONY: up prepare up-nodes down logs restart deploy sync-nodes test-alert test-rule check backup

up: prepare    ## Поднять хаб (первый этап: метрики панели + пробники)
	cd hub && docker compose up -d --build

prepare:       ## Создать каталоги данных с правильными владельцами
	@mkdir -p hub/data/victoriametrics hub/data/vmagent hub/data/alertmanager hub/data/grafana
	@# Alertmanager и Grafana работают не от root и иначе не могут писать
	@# в свои каталоги: молча ломаются заглушки и подавление повторов.
	-@chown -R 65534:65534 hub/data/alertmanager 2>/dev/null
	-@chown -R 472:472 hub/data/grafana 2>/dev/null

up-nodes: prepare ## То же плюс рефлектор iperf3 — когда пойдёт второй этап
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

test-rule:     ## Проверить всю цепочку от правила до телеграма
	./scripts/test-rule.sh

check:         ## Проверить, что конфиги валидны и цели скрейпятся
	cd hub && docker compose exec vmagent wget -qO- http://localhost:8429/api/v1/targets | head -50

backup:        ## Снять слепок для переезда
	tar czf ../vpnmon-backup-$$(date +%F).tar.gz --exclude=hub/data/victoriametrics .
