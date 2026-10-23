# Shortcuts for Linux/macOS (on Windows use the equivalent commands shown in the README).
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
HOST ?= 127.0.0.1
PORT ?= 8080

.PHONY: help venv install run test test-unit test-integration docker-build docker-up docker-down docker-logs docker-test clean

help:
	@echo "Available targets:"
	@echo "  venv              create the virtual environment in $(VENV)"
	@echo "  install           install the dependencies (including test dependencies)"
	@echo "  run               start the service on $(HOST):$(PORT) without Docker"
	@echo "  test              run all tests (integration skipped without LibreOffice)"
	@echo "  test-unit         run the unit tests only"
	@echo "  test-integration  run the tests with real LibreOffice"
	@echo "  docker-up         start the service with Docker Compose"
	@echo "  docker-down       stop the Docker service"
	@echo "  docker-test       run all tests inside a container"
	@echo "  clean             remove caches and temporary files (not the data)"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

run:
	$(BIN)/uvicorn app.main:app --host $(HOST) --port $(PORT)

test:
	$(BIN)/python -m pytest

test-unit:
	$(BIN)/python -m pytest -m "not integration"

test-integration:
	$(BIN)/python -m pytest -m integration -v

docker-build:
	docker compose build

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f foldpdf

docker-test:
	docker build --target test -t foldpdf:test .
	docker run --rm foldpdf:test

clean:
	rm -rf .pytest_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
