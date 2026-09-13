# The commands somebody actually runs. Everything here is a one-liner over the
# real tool, so nothing is hidden behind a make target that could drift from what
# the README tells a reader to type.

PY ?= python
COMPOSE ?= docker compose
IMAGE ?= toolmarket:local
API_PORT ?= 8000

.PHONY: help install test lint check-config grpc-gen compose-up compose-obs compose-down \
        compose-logs smoke verify image psql redis-cli hf-deploy hf-deploy-docker \
        render-init tf-init tf-plan tf-apply tf-ready tf-destroy clean

help:  ## list targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /  - /'

install:  ## editable install with every backend
	$(PY) -m pip install -e ".[all]"

# Referenced by .gitignore, by tools/grpc_gen.py and by the error message in
# toolmarket/grpc/server.py. It existed in all three places and in none of this
# file until CI went red, which is the drift a doc-cited target is prone to.
grpc-gen:  ## regenerate the gRPC stubs from proto/ (needs grpcio-tools)
	$(PY) tools/grpc_gen.py

test:  ## the whole suite (no server needed; the Postgres cases skip)
	$(PY) -m pytest tests -q

lint:  ## the CI lint selection
	$(PY) -m ruff check --select E9,F63,F7,F82,F821 toolmarket tests tools

check-config:  ## validate the prometheus/grafana files (same script CI runs)
	$(PY) tools/check_dashboards.py

image:  ## build the runtime image
	docker build --target runtime -t $(IMAGE) .

compose-up:  ## core stack: api + worker + postgres + redis (~500 MB)
	$(COMPOSE) up -d --build
	@echo "api      -> http://127.0.0.1:$(API_PORT)/docs"
	@echo "health   -> http://127.0.0.1:$(API_PORT)/health"
	@echo "metrics  -> http://127.0.0.1:$(API_PORT)/metrics"

compose-obs:  ## core stack + prometheus + grafana (needs ~1 GB free)
	$(COMPOSE) --profile observability up -d --build
	@echo "grafana    -> http://127.0.0.1:3000  (dashboard: toolmarket / substrate)"
	@echo "prometheus -> http://127.0.0.1:9090"

compose-down:  ## stop everything, keep volumes
	$(COMPOSE) --profile observability down

compose-logs:  ## follow the api and worker
	$(COMPOSE) logs -f api worker

psql:  ## a psql shell in the database container
	$(COMPOSE) exec postgres psql -U toolmarket -d toolmarket

redis-cli:  ## a redis-cli shell
	$(COMPOSE) exec redis redis-cli

verify:  ## the live Postgres suite against the running stack
	@./deploy/verify.sh

smoke:  ## end-to-end against a running stack: health, ready, metrics, async
	@./deploy/smoke.sh

hf-deploy:  ## publish the API to a HuggingFace Space (gradio mode; free)
	$(PY) deploy/huggingface/deploy.py --mode gradio

hf-deploy-docker:  ## publish the Dockerfile to a Space (needs a PRO account)
	$(PY) deploy/huggingface/deploy.py --mode docker

render-init:  ## copy the Render blueprint to the repo root (Render reads it there)
	cp deploy/render/render.yaml render.yaml
	@echo "render.yaml copied; commit it and point a Blueprint at the repo"

tf-init:  ## terraform: download the docker provider (~25 MB, once)
	cd terraform && terraform init

tf-plan:  ## terraform: show every change before any of it happens
	cd terraform && terraform plan

tf-apply:  ## terraform: bring up postgres + redis + api + worker as a graph
	cd terraform && terraform apply

tf-ready:  ## terraform: prove the stack found Postgres/Redis, not the fallbacks
	@curl -s http://127.0.0.1:$(API_PORT)/ready

tf-destroy:  ## terraform: tear the stack down (base images are kept)
	cd terraform && terraform destroy

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist render.yaml
