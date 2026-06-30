# main.py
from ui.app import run_app
from core.validator import validator

if __name__ == "__main__":
    validator.validate_startup()
    run_app()