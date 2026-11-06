SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.NOTPARALLEL:

REPOSITORY := treethatgoesmoo/spy-market-reports
BRANCH := main

PYTHON ?= /usr/bin/python3
GENERATOR_PYTHON ?= /usr/bin/python3
RUN_USER ?= $(shell id -un)
RUN_GROUP ?= $(shell id -gn)
SUDO ?= sudo

PREFIX ?= /opt/spy-report-publisher
CONFIG_DIR ?= /etc/spy-report-publisher
CONFIG_FILE ?= $(CONFIG_DIR)/publisher.ini
STATE_DIR ?= /var/lib/spy-report-publisher
LOG_DIR ?= /var/log/spy-report-publisher
REPO_DIR ?= /srv/spy-market-reports

DEPLOY_KEY := $(CONFIG_DIR)/deploy_key
KNOWN_HOSTS := $(CONFIG_DIR)/known_hosts
SERVICE_FILE := /etc/systemd/system/spy-report-publisher@.service
TIMERS := \
	spy-report-publisher-premarket.timer \
	spy-report-publisher-postmarket-early.timer \
	spy-report-publisher-postmarket-regular.timer
GITHUB_ED25519_KEY := github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
SSH_COMMAND := /usr/bin/ssh -i $(DEPLOY_KEY) -o IdentitiesOnly=yes -o UserKnownHostsFile=$(KNOWN_HOSTS) -o StrictHostKeyChecking=yes -o BatchMode=yes

GENERATOR_DIR ?=
GENERATOR_SCRIPT ?=

.PHONY: help check-user test check deps install install-files install-config \
	setup-key show-key install-systemd bootstrap configure configure-generator \
	verify-config verify-auth clone-repo check-schedule enable-timers \
	disable-timers finish update status logs next-steps

help: ## Show the available setup and operations targets.
	@printf '%s\n' \
	  'SPY report publisher setup' \
	  '' \
	  'Recommended first installation:' \
	  '  make bootstrap' \
	  '  # Add the displayed deploy key to GitHub with write access.' \
	  '  make configure-generator GENERATOR_DIR=/absolute/generator/dir GENERATOR_SCRIPT=/absolute/generator.py' \
	  '  make finish' \
	  '' \
	  'If the generator has a different CLI, run: make configure' \
	  '' \
	  'Targets:'
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-user:
	@case '$(RUN_USER)' in ''|*[!A-Za-z0-9_.-]*) echo 'Invalid RUN_USER: $(RUN_USER)' >&2; exit 2;; esac
	@case '$(RUN_GROUP)' in ''|*[!A-Za-z0-9_.-]*) echo 'Invalid RUN_GROUP: $(RUN_GROUP)' >&2; exit 2;; esac
	@for path in '$(PREFIX)' '$(CONFIG_DIR)' '$(CONFIG_FILE)' '$(STATE_DIR)' '$(LOG_DIR)' '$(REPO_DIR)'; do \
	  [[ "$$path" =~ ^/[A-Za-z0-9._/-]+$$ ]] || { echo "Unsafe or non-absolute path: $$path" >&2; exit 2; }; \
	done

test: ## Run the complete unit and temporary-Git integration test suite.
	$(PYTHON) -m py_compile market_calendar.py spy_report_publisher.py schedule_runner.py
	$(PYTHON) -m unittest discover -s tests -v

check: test ## Run all checks that do not change the Pi or contact GitHub.
	@git diff --check
	@echo 'Checks passed.'

deps: ## Install Raspberry Pi OS package dependencies.
	$(SUDO) apt-get update
	$(SUDO) apt-get install -y git make openssh-client python3

install-files: check-user test ## Install or update the Python component under PREFIX.
	$(SUDO) install -d -m 0755 '$(PREFIX)'
	$(SUDO) install -m 0755 market_calendar.py spy_report_publisher.py schedule_runner.py '$(PREFIX)/'

install-config: check-user ## Create state/log directories and install config only if absent.
	$(SUDO) install -d -m 0750 -o '$(RUN_USER)' -g '$(RUN_GROUP)' '$(CONFIG_DIR)'
	$(SUDO) install -d -m 0750 -o '$(RUN_USER)' -g '$(RUN_GROUP)' '$(STATE_DIR)/incoming'
	$(SUDO) install -d -m 0750 -o '$(RUN_USER)' -g '$(RUN_GROUP)' '$(LOG_DIR)'
	@if ! $(SUDO) test -f '$(CONFIG_FILE)'; then \
	  tmp="$$(mktemp)"; trap 'rm -f "$$tmp"' EXIT; \
	  sed \
	    -e 's|^repo_dir = .*|repo_dir = $(REPO_DIR)|' \
	    -e 's|^calendar_overrides = .*|calendar_overrides = $(CONFIG_DIR)/calendar-overrides.json|' \
	    -e 's|^log_file = .*|log_file = $(LOG_DIR)/publisher.log|' \
	    -e 's|^input_dir = .*|input_dir = $(STATE_DIR)/incoming|' \
	    config/spy-report-publisher.ini.example >"$$tmp"; \
	  $(SUDO) install -m 0640 -o '$(RUN_USER)' -g '$(RUN_GROUP)' "$$tmp" '$(CONFIG_FILE)'; \
	  echo 'Installed new $(CONFIG_FILE)'; \
	else \
	  echo 'Preserved existing $(CONFIG_FILE)'; \
	fi
	@if ! $(SUDO) test -f '$(CONFIG_DIR)/calendar-overrides.json'; then \
	  $(SUDO) install -m 0640 -o '$(RUN_USER)' -g '$(RUN_GROUP)' \
	    config/calendar-overrides.json '$(CONFIG_DIR)/calendar-overrides.json'; \
	else \
	  echo 'Preserved existing calendar overrides.'; \
	fi

setup-key: check-user install-config ## Create a repository-scoped deploy key and pin GitHub's host key.
	@if ! $(SUDO) test -f '$(DEPLOY_KEY)'; then \
	  $(SUDO) ssh-keygen -q -t ed25519 -N '' \
	    -C 'spy-report-publisher Raspberry Pi deploy key' -f '$(DEPLOY_KEY)'; \
	  echo 'Created $(DEPLOY_KEY)'; \
	else \
	  echo 'Preserved existing $(DEPLOY_KEY)'; \
	fi
	@if ! $(SUDO) test -f '$(DEPLOY_KEY).pub'; then \
	  $(SUDO) ssh-keygen -y -f '$(DEPLOY_KEY)' | $(SUDO) tee '$(DEPLOY_KEY).pub' >/dev/null; \
	fi
	$(SUDO) chown '$(RUN_USER):$(RUN_GROUP)' '$(DEPLOY_KEY)' '$(DEPLOY_KEY).pub'
	$(SUDO) chmod 0600 '$(DEPLOY_KEY)'
	$(SUDO) chmod 0644 '$(DEPLOY_KEY).pub'
	@printf '%s\n' '$(GITHUB_ED25519_KEY)' | $(SUDO) tee '$(KNOWN_HOSTS)' >/dev/null
	$(SUDO) chown root:root '$(KNOWN_HOSTS)'
	$(SUDO) chmod 0644 '$(KNOWN_HOSTS)'
	@echo
	@echo 'Add this deploy key to https://github.com/$(REPOSITORY)/settings/keys'
	@echo 'and enable Allow write access:'
	@$(SUDO) cat '$(DEPLOY_KEY).pub'

show-key: ## Print the deploy public key to add in GitHub repository settings.
	@$(SUDO) test -f '$(DEPLOY_KEY).pub' || { echo 'Run make setup-key first.' >&2; exit 2; }
	@$(SUDO) cat '$(DEPLOY_KEY).pub'

install-systemd: check-user ## Install service/timer units without enabling them.
	@sed \
	  -e 's/__RUN_USER__/$(RUN_USER)/g' \
	  -e 's/__RUN_GROUP__/$(RUN_GROUP)/g' \
	  -e 's|/opt/spy-report-publisher|$(PREFIX)|g' \
	  -e 's|/etc/spy-report-publisher|$(CONFIG_DIR)|g' \
	  -e 's|/var/lib/spy-report-publisher|$(STATE_DIR)|g' \
	  -e 's|/var/log/spy-report-publisher|$(LOG_DIR)|g' \
	  -e 's|/srv/spy-market-reports|$(REPO_DIR)|g' \
	  -e 's|/usr/bin/python3|$(PYTHON)|g' \
	  systemd/spy-report-publisher@.service \
	  | $(SUDO) tee '$(SERVICE_FILE)' >/dev/null
	$(SUDO) install -m 0644 systemd/spy-report-publisher-premarket.timer /etc/systemd/system/
	$(SUDO) install -m 0644 systemd/spy-report-publisher-postmarket-early.timer /etc/systemd/system/
	$(SUDO) install -m 0644 systemd/spy-report-publisher-postmarket-regular.timer /etc/systemd/system/
	$(SUDO) systemctl daemon-reload

install: bootstrap ## Alias for bootstrap.

bootstrap: deps install-files install-config setup-key install-systemd ## Install safely, but do not enable timers yet.
	@$(MAKE) --no-print-directory next-steps

configure: ## Open the installed configuration for manual generator integration.
	$(SUDO) test -f '$(CONFIG_FILE)' || { echo 'Run make bootstrap first.' >&2; exit 2; }
	$(SUDO) -e '$(CONFIG_FILE)'

configure-generator: check-user ## Configure a generator using the documented command-line contract.
	@if [[ -z '$(GENERATOR_DIR)' || -z '$(GENERATOR_SCRIPT)' ]]; then \
	  echo 'Usage: make configure-generator GENERATOR_DIR=/absolute/dir GENERATOR_SCRIPT=/absolute/script.py' >&2; \
	  exit 2; \
	fi
	@case '$(GENERATOR_DIR)' in /*) ;; *) echo 'GENERATOR_DIR must be absolute.' >&2; exit 2;; esac
	@case '$(GENERATOR_SCRIPT)' in /*) ;; *) echo 'GENERATOR_SCRIPT must be absolute.' >&2; exit 2;; esac
	@if [[ '$(GENERATOR_DIR)$(GENERATOR_SCRIPT)' == *[\ \|\&\\]* ]]; then \
	  echo 'Generator paths cannot contain whitespace, |, &, or backslash.' >&2; exit 2; \
	fi
	@$(SUDO) -u '$(RUN_USER)' test -d '$(GENERATOR_DIR)' || { echo 'Generator directory is not accessible to $(RUN_USER): $(GENERATOR_DIR)' >&2; exit 2; }
	@$(SUDO) -u '$(RUN_USER)' test -r '$(GENERATOR_SCRIPT)' || { echo 'Generator script is not readable by $(RUN_USER): $(GENERATOR_SCRIPT)' >&2; exit 2; }
	@$(SUDO) test -f '$(CONFIG_FILE)' || { echo 'Run make bootstrap first.' >&2; exit 2; }
	@tmp="$$(mktemp)"; trap 'rm -f "$$tmp"' EXIT; \
	  $(SUDO) sed \
	    -e 's|^working_directory = .*|working_directory = $(GENERATOR_DIR)|' \
	    -e 's|^premarket_command = .*|premarket_command = $(GENERATOR_PYTHON) $(GENERATOR_SCRIPT) --session {session} --trading-date {trading_date} --output {output}|' \
	    -e 's|^postmarket_command = .*|postmarket_command = $(GENERATOR_PYTHON) $(GENERATOR_SCRIPT) --session {session} --trading-date {trading_date} --output {output}|' \
	    '$(CONFIG_FILE)' >"$$tmp"; \
	  $(SUDO) install -m 0640 -o '$(RUN_USER)' -g '$(RUN_GROUP)' "$$tmp" '$(CONFIG_FILE)'
	@echo 'Configured generator in $(CONFIG_FILE)'

verify-config: check-user ## Validate the installed config and generator working directory.
	@$(SUDO) test -f '$(CONFIG_FILE)' || { echo 'Missing $(CONFIG_FILE)' >&2; exit 2; }
	@$(SUDO) -u '$(RUN_USER)' '$(PYTHON)' -c \
	  "from pathlib import Path; import sys; sys.path.insert(0, '$(PREFIX)'); from schedule_runner import load_config; c=load_config(Path('$(CONFIG_FILE)')); assert c.generator_working_dir.is_dir(), f'generator working directory not found: {c.generator_working_dir}'; print(f'Config OK: generator={c.generator_working_dir}, repo={c.publisher.repo_dir}')"

verify-auth: check-user ## Verify that the deploy key authenticates to GitHub.
	@set +e; \
	  output="$$( $(SUDO) -u '$(RUN_USER)' $(SSH_COMMAND) -T git@github.com 2>&1 )"; status=$$?; \
	  set -e; printf '%s\n' "$$output"; \
	  if [[ $$status -ne 1 ]] || ! grep -q 'successfully authenticated' <<<"$$output"; then \
	    echo 'GitHub deploy-key authentication failed.' >&2; exit 1; \
	  fi

clone-repo: check-user ## Clone or validate the dedicated publication checkout.
	@$(SUDO) install -d -m 0755 "$$(dirname '$(REPO_DIR)')"
	@if $(SUDO) test -d '$(REPO_DIR)/.git'; then \
	  remote="$$( $(SUDO) -u '$(RUN_USER)' git -C '$(REPO_DIR)' remote get-url origin )"; \
	  branch="$$( $(SUDO) -u '$(RUN_USER)' git -C '$(REPO_DIR)' branch --show-current )"; \
	  [[ "$$remote" == 'git@github.com:$(REPOSITORY).git' ]] || { echo "Unexpected remote: $$remote" >&2; exit 2; }; \
	  [[ "$$branch" == '$(BRANCH)' ]] || { echo "Unexpected branch: $$branch" >&2; exit 2; }; \
	  echo 'Validated existing $(REPO_DIR)'; \
	else \
	  if $(SUDO) test -d '$(REPO_DIR)' && [[ -n "$$( $(SUDO) find '$(REPO_DIR)' -mindepth 1 -print -quit )" ]]; then \
	    echo 'Refusing to clone into non-empty $(REPO_DIR)' >&2; exit 2; \
	  fi; \
	  $(SUDO) install -d -m 0750 -o '$(RUN_USER)' -g '$(RUN_GROUP)' '$(REPO_DIR)'; \
	  $(SUDO) -u '$(RUN_USER)' env GIT_SSH_COMMAND='$(SSH_COMMAND)' \
	    git clone --branch '$(BRANCH)' --single-branch \
	    'git@github.com:$(REPOSITORY).git' '$(REPO_DIR)'; \
	fi
	$(SUDO) -u '$(RUN_USER)' git -C '$(REPO_DIR)' config user.name 'SPY Report Publisher'
	$(SUDO) -u '$(RUN_USER)' git -C '$(REPO_DIR)' config user.email 'spy-report-publisher@localhost'
	@$(SUDO) -u '$(RUN_USER)' git -C '$(REPO_DIR)' status --short --branch

check-schedule: check-user verify-config ## Evaluate both schedule gates without generating or publishing.
	$(SUDO) -u '$(RUN_USER)' $(PYTHON) '$(PREFIX)/schedule_runner.py' --config '$(CONFIG_FILE)' --session premarket --check-schedule
	$(SUDO) -u '$(RUN_USER)' $(PYTHON) '$(PREFIX)/schedule_runner.py' --config '$(CONFIG_FILE)' --session postmarket --check-schedule

enable-timers: install-systemd verify-config clone-repo ## Verify and enable all market-aware timers.
	$(SUDO) systemd-analyze verify '$(SERVICE_FILE)' $(addprefix /etc/systemd/system/,$(TIMERS))
	$(SUDO) systemctl enable --now $(TIMERS)
	@$(MAKE) --no-print-directory status

disable-timers: ## Disable the publisher timers without removing files or reports.
	$(SUDO) systemctl disable --now $(TIMERS)

finish: verify-auth clone-repo verify-config enable-timers ## Complete setup after the deploy key and generator are configured.
	@echo 'SPY report publisher setup is complete.'

update: install-files install-systemd ## Update code and unit files while preserving config and credentials.
	@echo 'Publisher code updated. Existing configuration and deploy key were preserved.'

status: ## Show timer and recent service status.
	@systemctl list-timers 'spy-report-publisher*' --no-pager || true
	@systemctl --no-pager --full status 'spy-report-publisher@premarket.service' 'spy-report-publisher@postmarket.service' || true

logs: ## Show today's publisher service logs.
	$(SUDO) journalctl -u 'spy-report-publisher@*' --since today --no-pager

next-steps:
	@printf '%s\n' \
	  '' \
	  'Bootstrap complete; timers are still disabled.' \
	  '1. Add the deploy key above in GitHub repository Settings > Deploy keys with write access.' \
	  '2. Configure the generator:' \
	  '   make configure-generator GENERATOR_DIR=/absolute/dir GENERATOR_SCRIPT=/absolute/script.py' \
	  '   (or run make configure for a different generator CLI)' \
	  '3. Complete setup: make finish' \
	  ''

