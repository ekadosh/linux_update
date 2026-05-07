SHELL := /usr/bin/env bash

INVENTORY_ARGS := -i inventory/static_hosts.yml -i inventory/proxmox_guest_agent.py
PLAYBOOK := playbooks/update_ubuntu.yml
ANSIBLE := .venv/bin/ansible
ANSIBLE_INVENTORY := .venv/bin/ansible-inventory
ENV_FILE ?= .env
EXTRA_ARGS ?=
LIMIT ?=

ifneq ($(strip $(LIMIT)),)
LIMIT_ARGS := --limit $(LIMIT)
endif

define LOAD_ENV
if [[ ! -f "$(ENV_FILE)" ]]; then echo "Missing $(ENV_FILE). Copy .env.example to .env and fill in values." >&2; exit 1; fi; set -a; source "$(ENV_FILE)"; set +a; export PATH="$(CURDIR)/.venv/bin:$$PATH"; export ANSIBLE_REMOTE_USER="$${ANSIBLE_SSH_USER:-ansible}";
endef

.PHONY: bootstrap create-user known-hosts inventory ping update dry-run install-cron syntax-check static-inventory-check shell-check test

bootstrap:
	./scripts/bootstrap.sh

create-user:
	./scripts/create_ansible_user.sh $(EXTRA_ARGS)

known-hosts:
	./scripts/seed_known_hosts.sh

inventory:
	@$(LOAD_ENV) $(ANSIBLE_INVENTORY) $(INVENTORY_ARGS) --list $(EXTRA_ARGS)

static-inventory-check:
	$(ANSIBLE_INVENTORY) -i inventory/static_hosts.yml --list

ping:
	@$(LOAD_ENV) $(ANSIBLE) $(INVENTORY_ARGS) linux_update_targets -m ping $(LIMIT_ARGS) $(EXTRA_ARGS)

update:
	./scripts/run_updates.sh $(LIMIT_ARGS) $(EXTRA_ARGS)

dry-run:
	./scripts/run_updates.sh --check --diff $(LIMIT_ARGS) $(EXTRA_ARGS)

install-cron:
	./scripts/install_cron.sh $(EXTRA_ARGS)

syntax-check:
	.venv/bin/ansible-playbook -i tests/syntax_inventory.yml --syntax-check $(PLAYBOOK)

shell-check:
	tests/shell_checks.sh

test: shell-check syntax-check
