SHELL := /bin/bash
PYTHON ?= python3

PY_MODULES := server_rmm.py rmm_cli.py rmm_socks.py rmm_ws.py rmm_tools.py rmm_rclone.py \
	mcp_rmm_server.py rmm_mcp_client.py rmm_ai.py rmm_run_on_host.py rmm_kill_host_sessions.py

MD_SCAN := find . -type f -name '*.md' -not -path './.git/*' -not -path '*/.venv/*'
YAML_SCAN := find . -type f \( -name '*.yml' -o -name '*.yaml' \) -not -path './.git/*' -not -path '*/.venv/*'

.PHONY: help install-lint lint-md lint-yaml lint test

help:
	@printf "Available targets:\n"
	@printf "  make install-lint  Install pymarkdownlnt and yamllint\n"
	@printf "  make lint-md       Markdown lint (pymarkdown)\n"
	@printf "  make lint-yaml     YAML lint (yamllint)\n"
	@printf "  make lint          Run lint-md and lint-yaml\n"
	@printf "  make test          Python syntax check (py_compile)\n"

install-lint:
	$(PYTHON) -m pip install pymarkdownlnt yamllint

lint-md:
	@count=$$($(MD_SCAN) | wc -l | tr -d ' '); \
	if [ "$$count" = "0" ]; then \
		printf "No Markdown files found.\n"; \
	else \
		$(MD_SCAN) -print0 | sort -z | xargs -0 $(PYTHON) -m pymarkdown -d md003,md013,md022,md026,md041 scan; \
	fi

lint-yaml:
	@count=$$($(YAML_SCAN) | wc -l | tr -d ' '); \
	if [ "$$count" = "0" ]; then \
		printf "No YAML files found.\n"; \
	else \
		$(YAML_SCAN) -print0 | sort -z | xargs -0 $(PYTHON) -m yamllint; \
	fi

lint: lint-md lint-yaml

test:
	@$(PYTHON) -m py_compile $(PY_MODULES)
