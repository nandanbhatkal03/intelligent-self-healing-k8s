# Intelligent Self-Healing Kubernetes Platform — developer entry points.
# `make help` lists targets. Scripts live in scripts/, manifests in deploy/.
SHELL := /bin/bash
PY    ?= python3
VENV  := platform/.venv
SH    := $(VENV)/bin/selfheal

.DEFAULT_GOAL := help
TF    := terraform -chdir=infra/terraform
.PHONY: dashboards pause resume help host infra infra-destroy up-scripts prereqs cluster observability chaos app up down port-forward stop-forward \
        smoke check-metrics reset-metrics venv test lint ci fault-% bad-release status clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

host:  ## [O1] Provision this Ubuntu/WSL2 host with Ansible (Docker, kind, kubectl, helm, terraform)
	ansible-playbook -i infra/ansible/inventory.ini infra/ansible/site.yml -K

infra:  ## [O1] Terraform: Kind cluster + namespaces + observability + Chaos Mesh
	$(TF) init -upgrade
	$(TF) apply -auto-approve

infra-destroy:  ## Terraform destroy (deletes the cluster)
	$(TF) destroy -auto-approve

prereqs:  ## Check docker/kind/kubectl/helm/terraform/python and host resources
	@scripts/check-prereqs.sh

cluster:  ## Create the Kind cluster (1 control-plane + 2 workers)
	@scripts/cluster-up.sh

observability:  ## Install Prometheus, Grafana, Loki, Jaeger, OTel Collector, blackbox probe
	@scripts/install-observability.sh

chaos:  ## Install Chaos Mesh
	@scripts/install-chaos-mesh.sh

app:  ## Build + deploy ShopLite and the Locust load generator
	@scripts/deploy-app.sh

up: prereqs infra app  ## Terraform infra + app (~10-15 min first time)
	@echo; echo "Next: make port-forward, then make smoke (after ~2 min of traffic)"

up-scripts: prereqs cluster observability chaos app  ## Same as `up` without Terraform (shell-script fallback)
	@echo; echo "Next: make port-forward, then make smoke (after ~2 min of traffic)"

down:  ## Delete the cluster (Terraform-managed or script-managed; works when paused too)
	@scripts/cluster-down.sh

pause:  ## End of session: stop port-forwards and pause the cluster (data is kept)
	@scripts/cluster-pause.sh

resume:  ## Start of session: restore node IPs, start the cluster, wait for pods, open port-forwards
	@scripts/cluster-resume.sh

dashboards:  ## Reload Grafana dashboards from deploy/observability/dashboards (no other changes)
	kubectl -n monitoring create configmap selfheal-dashboards \
	  --from-file=deploy/observability/dashboards/ --dry-run=client -o yaml \
	  | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -
	@echo "Grafana picks up the change within about a minute (refresh the page)."

port-forward:  ## Expose Grafana :3000, Prometheus :9090, Loki :3100, Jaeger :16686, Chaos :2333, app :8080, Locust :8089
	@scripts/port-forward.sh start

stop-forward:  ## Stop port-forwards
	@scripts/port-forward.sh stop

check-metrics:  ## Grafana shows "No data"? Diagnose Prometheus storage + Grafana data source
	@scripts/check-metrics.sh

reset-metrics:  ## Wipe Prometheus' stored metrics (fix for samples from the future); data/*.csv untouched
	@scripts/reset-metrics.sh

smoke:  ## Verify metrics, logs, traces, probe and dashboards are all flowing
	@scripts/smoke-test.sh

venv:  ## Create platform/.venv with the selfheal CLI installed
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e 'platform[dev]'
	@echo "Activate with: source $(VENV)/bin/activate   (then: selfheal --help)"

test:  ## Run the platform unit tests
	cd platform && $(if $(wildcard $(VENV)/bin/python),.venv/bin/python,$(PY)) -m unittest discover -s tests -t . -v

lint:  ## Lint Python (ruff) and shell scripts (shellcheck), as CI does
	$(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff) check --config ruff.toml platform/selfheal platform/tests apps/shoplite deploy/loadgen
	shellcheck -x -S warning scripts/*.sh

ci: lint test  ## Run the GitHub Actions checks locally before pushing (+ terraform fmt/validate)
	$(TF) fmt -check -recursive -diff
	$(TF) validate
	@echo "All CI checks passed locally."


fault-%:  ## Run a Chaos Mesh experiment, e.g. make fault-pod-kill MODE=baseline
	cd platform && $(SH) experiment run ../deploy/chaos/$*.yaml --mode $(or $(MODE),selfheal)

bad-release:  ## Roll out a buggy revision of orders (rollback test)
	@scripts/bad-release.sh orders 0.4

status:  ## Pods in the app namespace + recent selfheal audit events
	@kubectl get pods -n shoplite -o wide
	@-cd platform && $(SH) status

clean:  ## Remove local logs, models, data and reports (not the cluster)
	rm -rf platform/logs platform/models/*.joblib platform/data/*.csv platform/reports .port-forward*
