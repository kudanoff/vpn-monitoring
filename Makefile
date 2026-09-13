.PHONY: up prepare up-nodes down logs restart deploy render sync-nodes test-alert test-rule test-mode-on test-mode-off check backup

up: prepare    ## Поднять хаб (первый этап: метрики панели + пробники)
	cd hub && docker compose up -d --build
	@# Alertmanager по SIGHUP пишет в лог "конфигурация загружена", но
	@# маршруты остаются старыми. Пересоздаём контейнер целиком — заглушки
	@# и история уведомлений лежат на диске и переживают это спокойно.
	@cd hub && docker compose up -d --force-recreate alertmanager

prepare: render ## Создать каталоги данных с правильными владельцами
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

render:        ## Пересобрать scrape.yml из шаблонов по списку проектов
	./scripts/render-scrape.sh

sync-nodes:    ## Собрать targets/nodes-<проект>.yml из API панелей
	./scripts/sync-nodes.sh $(PROJECT)

deploy:        ## Раскатать агент на ноды: make deploy [LIMIT=nl-3]
	cd ansible && ansible-playbook -i inventory.yml playbook.yml $(if $(LIMIT),--limit $(LIMIT),)

test-alert:    ## Проверить доставку алертов: make test-alert PROJECT=most
	./scripts/test-alert.sh $(PROJECT)

test-mode-on:  ## Ускорить все алерты до минуты (для проверок)
	./scripts/test-mode.sh on

test-mode-off: ## Вернуть боевые пороги
	./scripts/test-mode.sh off

test-rule:     ## Проверить всю цепочку от правила до телеграма
	./scripts/test-rule.sh

check:         ## Сквозная проверка: сбор, правила, алерты, доставка
	./scripts/selfcheck.sh

backup:        ## Снять слепок для переезда
	tar czf ../vpnmon-backup-$$(date +%F).tar.gz --exclude=hub/data/victoriametrics .
