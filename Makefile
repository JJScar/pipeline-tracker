# Pipeline Tracker controls. Run from this folder: make start | stop | restart | status | logs | open
PORT := 8765
PID  := .tracker.pid
LOG  := tracker.log
URL  := http://localhost:$(PORT)

.PHONY: start stop restart status logs open

start:
	@if lsof -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Already running at $(URL)"; \
	else \
		nohup python3 server.py $(PORT) --no-browser >> $(LOG) 2>&1 & echo $$! > $(PID); \
		sleep 1; \
		if lsof -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
			echo "Started at $(URL)"; \
		else \
			echo "Failed to start. Last log lines:"; tail -n 20 $(LOG); rm -f $(PID); exit 1; \
		fi; \
	fi

stop:
	@PIDS=$$(lsof -tiTCP:$(PORT) -sTCP:LISTEN 2>/dev/null); \
	if [ -n "$$PIDS" ]; then kill $$PIDS && echo "Stopped."; else echo "Not running."; fi; \
	rm -f $(PID)

restart: stop
	@sleep 1
	@$(MAKE) --no-print-directory start

status:
	@if lsof -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Running at $(URL) (pid $$(lsof -tiTCP:$(PORT) -sTCP:LISTEN))"; \
	else echo "Not running."; fi

logs:
	@tail -n 50 -f $(LOG)

open:
	@open $(URL)
