.PHONY: setup

setup:
	@echo "Setting up local environment with uv..."
	uv sync --all-extras
	uv run pre-commit install
	@echo "Setup complete."


