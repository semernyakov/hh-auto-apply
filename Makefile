PROJECT_DIR := $(shell pwd)
PYTHON      := $(PROJECT_DIR)/.venv/bin/python
PID_FILE    := /tmp/hh_dashboard.pid
LOG_FILE    := /tmp/hh_dashboard.log
PORT        := 8765

.PHONY: start stop restart status logs

start:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Dashboard уже запущен (PID $$(cat $(PID_FILE))) — http://127.0.0.1:$(PORT)"; \
	else \
		rm -f $(PID_FILE); \
		HH_HEADLESS=1 nohup $(PYTHON) -u $(PROJECT_DIR)/dashboard.py > $(LOG_FILE) 2>&1 & \
		echo $$! > $(PID_FILE); \
		sleep 2; \
		echo "Dashboard запущен (PID $$(cat $(PID_FILE))) — http://127.0.0.1:$(PORT)"; \
	fi

stop:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		PID=$$(cat $(PID_FILE)); \
		echo "Останавливаю воркеров через API..."; \
		for w in reply apply boost; do \
			curl -s -X POST http://127.0.0.1:$(PORT)/api/$$w/stop > /dev/null || true; \
		done; \
		kill $$PID 2>/dev/null || true; \
		for i in 1 2 3 4 5 6 7 8; do \
			kill -0 $$PID 2>/dev/null || break; \
			sleep 0.5; \
		done; \
		kill -9 $$PID 2>/dev/null || true; \
		rm -f $(PID_FILE); \
		echo "Dashboard остановлен"; \
	else \
		rm -f $(PID_FILE); \
		echo "Dashboard не запущен"; \
	fi

restart: stop start

status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Dashboard: running (PID $$(cat $(PID_FILE)))"; \
		curl -s http://127.0.0.1:$(PORT)/api/status | $(PYTHON) -c "import json,sys; d=json.load(sys.stdin); [print(f'  {n}: running={w[\"running\"]} pid={w[\"pid\"]}') for n,w in d['workers'].items()]" 2>/dev/null || true; \
	else \
		echo "Dashboard: stopped"; \
	fi

logs:
	@tail -f $(LOG_FILE)
