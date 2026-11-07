# local-ctf — run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help
S := scripts

.PHONY: help reip install secrets build build-ctfd build-controller deploy ctfd-setup \
        teams challenges push status logs stop-instances backup restore new-round \
        join-script test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── One-time setup (core laptop) ───────────────────────────────────────────
install: ## Install k3s, registry, Traefik config, firewall rules on this laptop
	bash $(S)/install-server.sh

secrets: ## Generate platform secrets + HTTPS cert and load them into the cluster
	bash $(S)/gen-secrets.sh

build: ## Build + push CTFd, controller and static-files images
	bash $(S)/build-images.sh all

build-ctfd: ## Rebuild CTFd image (after plugin/branding changes) and restart it
	bash $(S)/build-images.sh ctfd && kubectl -n ctfd rollout restart deploy/ctfd

build-controller: ## Rebuild controller image and restart it
	bash $(S)/build-images.sh controller && kubectl -n ctfd rollout restart deploy/controller-api deploy/controller-daemon deploy/controller-admin

deploy: ## Apply all platform manifests and wait until healthy
	bash $(S)/deploy-platform.sh

ctfd-setup: ## First-time CTFd setup; add TEAMS_CSV=data/teams.csv to import teams
	TEAMS_CSV=$(TEAMS_CSV) bash $(S)/ctfd-setup.sh

join-script: ## Regenerate .secrets/join-agent.sh for a challenge laptop
	bash $(S)/make-join-script.sh

# ── Every round ────────────────────────────────────────────────────────────
teams: ## Import/refresh teams: make teams CSV=data/teams.csv
	@test -n "$(CSV)" || { echo "Usage: make teams CSV=data/teams.csv"; exit 1; }
	TEAMS_CSV=$(CSV) bash $(S)/ctfd-setup.sh

push: ## Build + push one challenge image: make push DIR=challenges/src/web-login
	@test -n "$(DIR)" || { echo "Usage: make push DIR=challenges/src/<name>"; exit 1; }
	bash $(S)/push-challenge.sh $(DIR) $(NAME)

challenges: ## Publish all manifests in challenges/manifests (idempotent)
	bash $(S)/deploy-challenges.sh

status: ## Nodes, pods, running instances, endpoints
	bash $(S)/status.sh

logs: ## Follow CTFd + controller logs
	kubectl -n ctfd logs -f --max-log-requests 10 --prefix -l 'app in (ctfd,controller-api,controller-daemon)' --tail=20

stop-instances: ## Stop every running challenge instance (end of round)
	kubectl -n challenges scale deploy -l managed-by=ctf-controller --replicas=0

reip: ## Move the platform to this laptop's new IP (optional IP=x.x.x.x)
	bash $(S)/reip.sh $(IP)

backup: ## Dump DB + secrets to backups/ (optional NAME=label)
	bash $(S)/backup.sh $(NAME)

restore: ## Restore a dump: make restore BACKUP=backups/<folder>
	BACKUP=$(BACKUP) bash $(S)/restore.sh

new-round: ## Reset for the next round (MODE=full wipes accounts too)
	MODE=$(or $(MODE),keep-teams) bash $(S)/new-round.sh

test: ## Run controller unit tests
	cd controller && ../.venv/bin/python -m pip install -q pytest && ../.venv/bin/python -m pytest -q unit_tests
