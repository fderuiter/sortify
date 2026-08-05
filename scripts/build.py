"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys


def main():
    """Build the standalone executable."""
    import importlib.util

    import PyInstaller.__main__

    is_lite = "--lite" in sys.argv
    if is_lite:
        sys.argv.remove("--lite")
        os.environ["LITE_BUILD"] = "1"
        print(
            "Lite profile enabled. Heavy ML packages will be excluded from the build."
        )
    else:
        print("Verifying machine learning packages in active environment...")
        ml_packages = [
            ("PyTorch", "torch"),
            ("EasyOCR", "easyocr"),
            ("Transformers", "transformers"),
            ("Scikit-Learn", "sklearn"),
            ("llama-cpp-python", "llama_cpp"),
            ("ONNX Runtime", "onnxruntime"),
            ("NumPy", "numpy"),
            ("Pandas", "pandas"),
            ("Pillow", "PIL"),
        ]
        for name, imp in ml_packages:
            try:
                spec = importlib.util.find_spec(imp)
                if spec is None:
                    raise ImportError()
            except (ImportError, ValueError, AttributeError, TypeError):
                print(f"Error: Missing machine learning package: {name}")
                sys.exit(1)

    print("Verifying SQLCipher in active environment...")
    spec = importlib.util.find_spec("sqlcipher3")
    if not spec or not spec.submodule_search_locations:
        print(
            "Error: sqlcipher3 not found in active environment. Please ensure dependencies are installed."
        )
        sys.exit(1)

    cmd = ["smart-autosorter.spec", "--noconfirm", "--clean"]

    PyInstaller.__main__.run(cmd)


if __name__ == "__main__":
    main()
