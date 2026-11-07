SHELL := /bin/bash
PYTHON ?= python3
DEPLOYMENT_ROOT ?= /workspace/rmm-deployment
WORKSPACE_ID ?= default

PY_MODULES := rmm_settings.py rmm_plugin_sources.py rmm_plugin_releases.py rmm_embeddings.py server_rmm.py rmm_cli.py rmm_socks.py rmm_ws.py rmm_tun.py rmm_tools.py rmm_rclone.py \
	rmm_deployment.py rmm_workspace_migration.py \
	mcp_rmm_server.py rmm_mcp_client.py rmm_ai.py rmm_ai_skill_context.py rmm_ai_context.py rmm_ai_skills.py rmm_ai_concurrency.py rmm_ai_jobs.py rmm_run_on_host.py rmm_kill_host_sessions.py \
	rmm_workspace.py workspace_pivot.py workspace_service.py rmm_asset_store.py rmm_profile.py rmm_bloodhound.py rmm_bloodhound_broker.py rmm_bloodhound_proxy.py rmm_exegol_runtime.py rmm_bloodhound_library.py scripts/import_bloodhound_library.py

MD_SCAN := find . -type f -name '*.md' -not -path './.git/*' -not -path '*/.venv/*'
YAML_SCAN := find . -type f \( -name '*.yml' -o -name '*.yaml' \) -not -path './.git/*' -not -path '*/.venv/*'

.PHONY: help install-lint lint-md lint-yaml lint test check-ai check-cli check-agent-deployment check-agent-commands check-parity check-web-contracts check check-exegol-mcp go-test tun-build workspace-service docker-build docker-up

help:
	@printf "Available targets:\n"
	@printf "  make install-lint  Install pymarkdownlnt and yamllint\n"
	@printf "  make lint-md       Markdown lint (pymarkdown)\n"
	@printf "  make lint-yaml     YAML lint (yamllint)\n"
	@printf "  make lint          Run lint-md and lint-yaml\n"
	@printf "  make test          Python syntax check (py_compile)\n"
	@printf "  make check-parity  MCP / API client / web shell alignment\n"
	@printf "  make check-cli     CLI dispatch and interactive completion tests\n"
	@printf "  make check-agent-deployment  Agent enrollment, registration proof, and generator regressions\n"
	@printf "  make check-ai      AI skill, context, provider and route regression tests\n"
	@printf "  make check-web-contracts  WebUI completion and configuration contracts\n"
	@printf "  make check         test + check-parity + lint\n"
	@printf "  make check-exegol-mcp  Validate a live Exegol MCP endpoint (needs RMM_EXEGOL_MCP_URL); not part of 'check'\n"
	@printf "                         pass ARGS=\"--config PATH\" or ARGS=\"--data-dir PATH\" to point at a saved workspace config\n"
	@printf "  make go-test       Cross-build the Go agent and test the pivot components\n"
	@printf "  make check-agent-commands  Test Go command routing and CMD quote normalization\n"
	@printf "  make tun-build     Build the server-side TUN helper\n"
	@printf "  make workspace-service  Start the local Exegol workspace service\n"
	@printf "  make docker-build  Build the distributed central-only image\n"
	@printf "  make docker-up     Start the central control plane with Compose\n"

check-agent-commands:
	@cd agent-go && go test -buildvcs=false -count=1 -run 'Test(NormalizeCMDQuotes|BuildCommandInvocation)' .

go-test:
	@cd agent-go && go test -buildvcs=false ./...
	@cd agent-go && go test -buildvcs=false -tags=rmmdebug ./...
	@cd agent-go && GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -buildvcs=false ./...
	@cd agent-go && GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build -buildvcs=false ./...
	@cd agent-go && GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -buildvcs=false -tags=rmmdebug ./...
	@cd agent-go && GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build -buildvcs=false -tags=rmmdebug ./...
	@cd server-tun && go test -buildvcs=false ./...
	@if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then \
		printf "stage1 C compile check (MinGW x86_64)...\n"; \
		x86_64-w64-mingw32-gcc -Os -s -mwindows \
			-DBASE_URL=L\"https://127.0.0.1:8443\" \
			-DSTAGE1_INSECURE \
			-o /tmp/stage1-check.exe \
			agent-go/stage1/c/stage1.c \
			-lwinhttp -lbcrypt -lshell32 && \
		printf "stage1 C compile: OK\n"; \
		x86_64-w64-mingw32-gcc -Os -s -mwindows \
			-o /tmp/lateral-service-check.exe \
			agent-go/lateral-service/c/service.c \
			-lwinhttp -lbcrypt -lshell32 -ladvapi32 && \
		printf "lateral service C compile: OK\n"; \
	else \
		printf "stage1 C compile: SKIP (x86_64-w64-mingw32-gcc not found; install mingw-w64 to enable)\n"; \
	fi

tun-build:
	@mkdir -p tools
	@cd server-tun && go build -buildvcs=false -trimpath -ldflags "-s -w" -o ../tools/rmm-tun .

workspace-service:
	@$(PYTHON) rmm_deployment.py run --root "$(DEPLOYMENT_ROOT)" --component workspace --workspace-id "$(WORKSPACE_ID)"

docker-build:
	docker build -t minimal-rmm:local .

docker-up:
	$(PYTHON) rmm_deployment.py run --root "$(DEPLOYMENT_ROOT)" --component control --docker

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
	@$(PYTHON) -m py_compile $(PY_MODULES) scripts/check_operator_parity.py scripts/check_web_contracts.py scripts/check_exegol_mcp.py

check-parity:
	@$(PYTHON) scripts/check_operator_parity.py

check-cli:
	@$(PYTHON) -m unittest discover -s tests -p 'test_cli*.py'

check-ai:
	@$(PYTHON) -m unittest discover -s tests -p 'test_ai*.py'
	@$(PYTHON) -m unittest discover -s tests -p 'test_rmm_ai*.py'
	@$(PYTHON) -m unittest discover -s tests -p 'test_*embedding*.py'
	@$(PYTHON) -m unittest discover -s tests -p 'test_server_rmm_ai.py'
	@node tests/test_ai_jobs_ui.cjs
	@node tests/test_ai_operation_details.cjs

check-agent-deployment:
	@PYTHONPATH=tests $(PYTHON) -m unittest test_spawn_session test_lateral_move test_make_token test_creds_routes test_agent_workspace_binding test_deployment_verification test_operator_proxy_timeout test_agent_deploy test_go_agent test_deployment_artifacts \
		test_debug_registration test_debug_deployment test_go_debug_build test_spawnas_debug
	@node tests/test_agent_generator_workspace.cjs

check-web-contracts:
	@$(PYTHON) scripts/check_web_contracts.py
	@node tests/test_session_identity_ui.cjs
	@node tests/test_spawn_result_ui.cjs
	@node tests/test_shell_completion.cjs
	@node tests/test_embeddings_shell.cjs
	@node tests/test_bloodhound_ui.cjs
	@node tests/test_bloodhound_shell.cjs
	@node tests/test_workspace_access_ui.cjs
	@node tests/test_go_agent_ui.cjs
	@node tests/test_assets_ui.cjs
	@node tests/test_agent_redirects.cjs

check: test check-agent-commands check-ai check-cli check-agent-deployment check-parity check-web-contracts lint

check-exegol-mcp:
	@$(PYTHON) scripts/check_exegol_mcp.py $(ARGS)
