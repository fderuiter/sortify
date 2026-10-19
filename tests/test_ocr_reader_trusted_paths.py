"""EasyOCR weights are never loaded from the working directory or an unverified bundle (#722)."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.offline_loader import ModelWeightsNotFoundError, OfflineModelLoader
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture
def registry(monkeypatch, tmp_path):
    mock_easyocr = MagicMock()
    monkeypatch.setitem(sys.modules, "easyocr", mock_easyocr)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    monkeypatch.delenv("EASYOCR_MODULE_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        OfflineModelLoader,
        "resolve_model_path",
        classmethod(
            lambda cls, model_id, **kw: (_ for _ in ()).throw(
                ModelWeightsNotFoundError(model_id, [])
            )
        ),
    )
    SharedModelRegistry._instance = None
    reg = SharedModelRegistry.get_instance()
    reg._models.pop("easyocr", None)
    yield reg, mock_easyocr
    SharedModelRegistry._instance = None


def _storage_dir(mock_easyocr):
    return mock_easyocr.Reader.call_args.kwargs["model_storage_directory"]


def test_unresolved_model_is_not_loaded_from_cwd(registry, tmp_path, monkeypatch):
    reg, mock_easyocr = registry
    cwd = tmp_path / "downloads"
    (cwd / "offline_bundle" / "easyocr").mkdir(parents=True)
    monkeypatch.chdir(cwd)

    with patch("app.core.path_utils.get_base_path", return_value=str(tmp_path / "app")):
        reg.get_ocr_reader()

    assert "downloads" not in _storage_dir(mock_easyocr)


def test_packaged_build_loads_no_ocr_without_a_verified_model(
    registry, tmp_path, monkeypatch
):
    reg, mock_easyocr = registry
    bundle = tmp_path / "meipass"
    (bundle / "offline_bundle" / "easyocr").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

    assert reg.get_ocr_reader() is None
    mock_easyocr.Reader.assert_not_called()
