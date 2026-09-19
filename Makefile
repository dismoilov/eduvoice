SERVER ?= root@10.103.10.86
REMOTE ?= /opt/eduvoice/app
SSH ?= ssh -o PubkeyAuthentication=no

.PHONY: voice-prewarm sql install lint test check deploy deploy-dialplan logs restart status call-test barge-test say agent-on agent-off calls hangup-all crm-logs crm-restart crm-user backup

install:            ## local environment
	uv sync

lint:
	uv run ruff check eduvoice store crm tests
	uv run ruff format --check eduvoice store crm tests
	uv run mypy

test:
	uv run pytest -q

check: lint test    ## run before every deploy

deploy: check       ## ship the code to the server and restart the bridge
	rsync -az --delete -e '$(SSH)' \
		--exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
		--exclude '.ruff_cache' --exclude '.git' --exclude 'logs' --exclude '.env' \
		--exclude 'audio' --exclude '.mypy_cache' --exclude '.DS_Store' \
		--exclude '.coverage*' --exclude 'htmlcov' --exclude 'data' \
		./ $(SERVER):$(REMOTE)/
	$(SSH) $(SERVER) 'install -m 644 $(REMOTE)/deploy/eduvoice-bridge.service $(REMOTE)/deploy/eduvoice-crm.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable eduvoice-bridge eduvoice-crm >/dev/null 2>&1 || true'
	$(SSH) $(SERVER) 'cd $(REMOTE) && UV_PYTHON_INSTALL_DIR=/opt/eduvoice/.python UV_CACHE_DIR=/opt/eduvoice/.cache/uv /usr/local/bin/uv sync --frozen --no-dev'
	$(SSH) $(SERVER) 'chown -R eduvoice:eduvoice /opt/eduvoice && chmod 600 /opt/eduvoice/app/.env 2>/dev/null; systemctl restart eduvoice-bridge eduvoice-crm && sleep 3 && systemctl is-active eduvoice-bridge eduvoice-crm'

logs:
	$(SSH) $(SERVER) 'journalctl -u eduvoice-bridge -f -o cat'

crm-logs:
	$(SSH) $(SERVER) 'journalctl -u eduvoice-crm -f -o cat'

crm-restart:
	$(SSH) $(SERVER) 'systemctl restart eduvoice-crm && sleep 2 && systemctl is-active eduvoice-crm'

USER_LOGIN ?=
USER_NAME ?=
USER_ROLE ?= operator
USER_PASSWORD ?=
USER_EXT ?=

backup:             ## consistent copy of the CRM database, kept on the server
	$(SSH) $(SERVER) 'cd $(REMOTE) && /usr/local/bin/uv run --no-sync python -m crm.cli backup /opt/eduvoice/data/backup-$$(date +%Y%m%d-%H%M).db && ls -lh /opt/eduvoice/data/*.db | tail -3'

voice-prewarm:      ## synthesise every phrase and answer into the cache (ARGS=--dry-run to only count)
	$(SSH) $(SERVER) 'cd $(REMOTE) && /usr/local/bin/uv run --no-sync python -m eduvoice.voice_tools prewarm $(ARGS)'

sql:                ## make sql Q="select count(*) from calls" — the system sqlite3 cannot read this database
	@test -n "$(Q)" || (echo 'usage: make sql Q="select ..."' && false)
	$(SSH) $(SERVER) 'cd $(REMOTE) && /usr/local/bin/uv run --no-sync python -m crm.cli sql "$(Q)"'

crm-user:           ## make crm-user USER_LOGIN=olim USER_NAME="Olim" USER_ROLE=operator USER_PASSWORD=... USER_EXT=101
	@test -n "$(USER_LOGIN)" -a -n "$(USER_PASSWORD)" || (echo "USER_LOGIN and USER_PASSWORD are required" && false)
	$(SSH) $(SERVER) 'cd $(REMOTE) && EDUVOICE_DB=/opt/eduvoice/data/eduvoice.db /usr/local/bin/uv run --no-sync python -m crm.cli user "$(USER_LOGIN)" "$(USER_NAME)" $(USER_ROLE) --password "$(USER_PASSWORD)" --extension "$(USER_EXT)"'

restart:
	$(SSH) $(SERVER) 'systemctl restart eduvoice-bridge && systemctl is-active eduvoice-bridge'

status:
	$(SSH) $(SERVER) 'systemctl is-active eduvoice-bridge eduvoice-crm; ss -tlnp | grep -E "9092|9093|9095"; asterisk -rx "pjsip show endpoints" | grep -cE "Endpoint:  (101|102|200)/"'

FILE ?= hello-world

call-test:          ## simulated call: question after the greeting (FILE=<sound>)
	$(SSH) $(SERVER) 'asterisk -rx "channel originate Local/s@eduvoice-ai/n extension q$(FILE)@eduvoice-test"'

barge-test:         ## simulated call: the caller speaks over the greeting
	$(SSH) $(SERVER) 'asterisk -rx "channel originate Local/s@eduvoice-ai/n extension b$(FILE)@eduvoice-test"'

say:                ## what the fake recogniser should "hear": make say TEXT="operator"
	@# only the one line is replaced: .env also holds the VoiceLab key and the tuning values
	$(SSH) $(SERVER) 'cd /opt/eduvoice/app && touch .env && sed -i "/^EDUVOICE_FAKE_STT=/d" .env && printf "EDUVOICE_FAKE_STT=%s\n" "$(TEXT)" >> .env && chown eduvoice:eduvoice .env && chmod 600 .env && systemctl restart eduvoice-bridge && sleep 2 && systemctl is-active eduvoice-bridge'

agent-on:           ## stand-in operator so transfers can be tested without a softphone
	$(SSH) $(SERVER) 'asterisk -rx "queue add member Local/agent@eduvoice-testagent to eduvoice-operators"'

agent-off:
	$(SSH) $(SERVER) 'asterisk -rx "queue remove member Local/agent@eduvoice-testagent from eduvoice-operators"'

calls:              ## last calls with timings
	$(SSH) $(SERVER) 'tail -5 /opt/eduvoice/app/logs/*.jsonl'

hangup-all:
	$(SSH) $(SERVER) 'asterisk -rx "channel request hangup all"'

deploy-dialplan:    ## copy the dialplan to Asterisk and reload it
	rsync -az -e '$(SSH)' asterisk/extensions_custom.conf asterisk/globals_custom.conf asterisk/queues_custom.conf asterisk/pjsip.endpoint_custom_post.conf $(SERVER):/etc/asterisk/
	$(SSH) $(SERVER) 'chown asterisk:asterisk /etc/asterisk/extensions_custom.conf /etc/asterisk/globals_custom.conf /etc/asterisk/queues_custom.conf /etc/asterisk/pjsip.endpoint_custom_post.conf && fwconsole reload >/dev/null && asterisk -rx "dialplan show globals" | grep EDUVOICE_BRIDGE && asterisk -rx "pjsip show endpoint 101" | grep -E "^ allow " && asterisk -rx "dialplan show eduvoice-ai" | tail -3'

