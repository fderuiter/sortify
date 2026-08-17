import hashlib
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.core.shared_registry import (
    SharedModelRegistry,
    SharedWorkerPool,
)


def test_shared_model_registry_singleton():
    """Assert that get_instance always returns the same centralized model registry."""
    reg1 = SharedModelRegistry.get_instance()
    reg2 = SharedModelRegistry.get_instance()
    assert reg1 is reg2


def test_shared_model_registry_defer_loading():
    """Verify that model loading is deferred until explicitly requested."""
    # Reset registry instance for a clean test
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    # Ensure no model weights/pipelines are in _models initially
    assert "generative_naming" not in registry._models
    assert "easyocr" not in registry._models


def test_shared_model_registry_integrity_check(tmp_path):
    """Test SHA-256 integrity checks on loaded models."""
    import sys
    from unittest.mock import MagicMock, patch

    mock_tokenizer = MagicMock()
    mock_model = MagicMock()
    mock_pipeline = MagicMock()
    mock_quantize = MagicMock()

    mock_transformers = MagicMock()
    mock_transformers.AutoTokenizer.from_pretrained.return_value = mock_tokenizer
    mock_transformers.AutoModelForSeq2SeqLM.from_pretrained.return_value = mock_model
    mock_transformers.pipeline.return_value = mock_pipeline

    mock_torch = MagicMock()
    mock_torch.quantization.quantize_dynamic.return_value = mock_quantize

    with (
        patch.dict(
            sys.modules, {"transformers": mock_transformers, "torch": mock_torch}
        ),
    ):
        SharedModelRegistry._instance = None
        registry = SharedModelRegistry.get_instance()

        model_dir = tmp_path / "dummy_model"
        model_dir.mkdir()
        config_file = model_dir / "config.json"
        config_content = b'{"model_type": "t5"}'
        config_file.write_bytes(config_content)

        # Compute expected SHA-256
        config_hash = hashlib.sha256(config_content).hexdigest()

        # Case 1: Register expected hash, matches actual -> should load successfully
        registry.register_expected_hashes(
            "generative_naming", {"config.json": config_hash}
        )

        gen, task, tok = registry.get_generative_model(str(model_dir))
        assert gen is not None
        # Verify we cached the model in registry
        assert "generative_naming" in registry._models

        # Case 2: Register expected hash, mismatch -> should raise ValueError and prevent execution
        SharedModelRegistry._instance = None
        registry = SharedModelRegistry.get_instance()
        registry.register_expected_hashes(
            "generative_naming", {"config.json": "wrong_hash"}
        )

        with pytest.raises(ValueError, match="Integrity check failed"):
            registry.get_generative_model(str(model_dir))


def test_shared_worker_pool_singleton():
    """Assert that get_instance always returns the same global worker pool."""
    SharedWorkerPool._instance = None
    pool1 = SharedWorkerPool.get_instance(max_workers=3)
    pool2 = SharedWorkerPool.get_instance(max_workers=5)
    assert pool1 is pool2
    assert pool1.max_workers == 3  # Initial creation max_workers respected


def test_shared_worker_pool_offline_enforcement():
    """Assert that tasks submitted to the global pool are blocked from external connections."""
    pool = SharedWorkerPool.get_instance()

    def task_trying_to_connect():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("8.8.8.8", 53))
        finally:
            s.close()

    future = pool.submit(task_trying_to_connect)
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        future.result()

    def task_trying_to_connect_ex():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect_ex(("8.8.8.8", 53))
        finally:
            s.close()

    future_ex = pool.submit(task_trying_to_connect_ex)
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        future_ex.result()


def test_session_db_and_cache_isolation():
    """Assert routing tasks preserves session-specific database and settings references."""
    pool = SharedWorkerPool.get_instance()

    def process_task(db_instance, settings_instance):
        return db_instance.get_id(), settings_instance.get_val()

    mock_db_1 = MagicMock()
    mock_db_1.get_id.return_value = "session_1_db"
    mock_settings_1 = MagicMock()
    mock_settings_1.get_val.return_value = "session_1_settings"

    mock_db_2 = MagicMock()
    mock_db_2.get_id.return_value = "session_2_db"
    mock_settings_2 = MagicMock()
    mock_settings_2.get_val.return_value = "session_2_settings"

    fut1 = pool.submit(process_task, mock_db_1, mock_settings_1)
    fut2 = pool.submit(process_task, mock_db_2, mock_settings_2)

    assert fut1.result() == ("session_1_db", "session_1_settings")
    assert fut2.result() == ("session_2_db", "session_2_settings")


def test_socket_sandbox_blocking_of_external_and_allow_localhost(socket_mock):
    """Verify that socket sandboxing blocks external domains while allowing localhost/loopback."""
    from app.core.shared_registry import (
        apply_global_socket_sandbox,
        block_external_network,
        safe_connect,
        safe_connect_ex,
    )

    mock_connect, mock_connect_ex = socket_mock

    apply_global_socket_sandbox()

    # Try connecting to external domain
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        with block_external_network():
            s = socket.socket()
            try:
                safe_connect(s, ("8.8.8.8", 80))
            finally:
                s.close()

    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        with block_external_network():
            s = socket.socket()
            try:
                safe_connect_ex(s, ("8.8.8.8", 80))
            finally:
                s.close()

    # Try connecting to localhost
    with block_external_network():
        s1 = socket.socket()
        try:
            safe_connect(s1, ("127.0.0.1", 8080))
            mock_connect.assert_called_once_with(s1, ("127.0.0.1", 8080))
        finally:
            s1.close()

    with block_external_network():
        s2 = socket.socket()
        try:
            safe_connect_ex(s2, ("localhost", 8080))
            mock_connect_ex.assert_called_once_with(s2, ("localhost", 8080))
        finally:
            s2.close()


def test_socket_sandbox_inactive_allows_external_connections(socket_mock):
    """Verify that when block_external_network is not active, external connections are allowed."""
    from app.core.shared_registry import safe_connect, safe_connect_ex

    mock_connect, mock_connect_ex = socket_mock

    s1 = socket.socket()
    try:
        safe_connect(s1, ("8.8.8.8", 80))
        mock_connect.assert_called_once_with(s1, ("8.8.8.8", 80))
    finally:
        s1.close()

    s2 = socket.socket()
    try:
        safe_connect_ex(s2, ("8.8.8.8", 80))
        mock_connect_ex.assert_called_once_with(s2, ("8.8.8.8", 80))
    finally:
        s2.close()


def test_socket_sandbox_case_insensitivity_and_local_suffixes(socket_mock):
    """Verify that the socket sandbox is case-insensitive and permits .local/.localhost suffixes."""
    from app.core.shared_registry import (
        _is_local_address,
        block_external_network,
        safe_connect,
        safe_connect_ex,
    )

    mock_connect, mock_connect_ex = socket_mock

    # Test cases for local addresses (various cases and suffixes)
    assert _is_local_address("LOCALHOST") is True
    assert _is_local_address("LocalHost") is True
    assert _is_local_address("my-pc.local") is True
    assert _is_local_address("MY-PC.LOCAL") is True
    assert _is_local_address("my-server.localhost") is True

    # Test case-insensitivity in safe_connect within active sandbox
    with block_external_network():
        s1 = socket.socket()
        try:
            safe_connect(s1, ("LOCALHOST", 8080))
            mock_connect.assert_called_once_with(s1, ("LOCALHOST", 8080))
        finally:
            s1.close()

        s2 = socket.socket()
        try:
            safe_connect_ex(s2, ("my-machine.local", 8080))
            mock_connect_ex.assert_called_once_with(s2, ("my-machine.local", 8080))
        finally:
            s2.close()


def test_check_ai_status_corrupt_or_missing(tmp_path, monkeypatch):
    """Verify check_ai_status correctly warns when models are corrupt/missing."""
    from app.config import AppSettings
    from app.core.verifier import check_ai_status

    settings = AppSettings()
    settings.AI_ASSISTED_NAMING = True

    # Case 1: missing/uninstalled dependencies -> mock is_ml_available returning False
    with patch("app.core.verifier.is_ml_available", return_value=False):
        is_healthy, warn_msg = check_ai_status(settings)
        assert not is_healthy
        assert "dependencies" in warn_msg

    # Case 2: ML is available but files are missing
    with (
        patch("app.core.verifier.is_ml_available", return_value=True),
        patch("app.core.verifier.os.path.exists", return_value=False),
    ):
        is_healthy, warn_msg = check_ai_status(settings)
        assert not is_healthy
        assert "weights are missing or corrupt" in warn_msg


def test_is_local_address_dynamic_resolution():
    """Verify that _is_local_address dynamically resolves hostname to identify local IP."""
    from app.core.shared_registry import _is_local_address

    # Mock getaddrinfo to return a private IP for a custom local hostname
    with patch(
        "socket.getaddrinfo",
        return_value=[(None, None, None, None, ("192.168.1.100", 0))],
    ):
        assert _is_local_address("custom-local-host") is True

    # Mock getaddrinfo to return an external public IP
    with patch(
        "socket.getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 0))]
    ):
        assert _is_local_address("custom-external-host") is False


def test_is_local_address_bypasses_dns_when_sandboxed():
    """Verify that _is_local_address completely bypasses getaddrinfo calls when sandboxed."""
    from app.core.shared_registry import _is_local_address, block_external_network

    with patch("socket.getaddrinfo") as mock_getaddrinfo:
        with block_external_network(reason="test offline bypass"):
            res = _is_local_address("custom-external-host")
            # When sandboxed, it should immediately return False without querying DNS
            assert res is False
            mock_getaddrinfo.assert_not_called()


def test_onnx_thread_limits_application(monkeypatch):
    """Verify that ONNX InferenceSession applies intra-op and inter-op thread limits dynamically."""
    import sys

    mock_ort = MagicMock()
    mock_sess_options = MagicMock()
    mock_ort.SessionOptions.return_value = mock_sess_options

    class DummyInferenceSession:
        def __init__(self, model_path, sess_options=None, *args, **kwargs):
            self.model_path = model_path
            self.sess_options = sess_options

    mock_ort.InferenceSession = DummyInferenceSession
    monkeypatch.setitem(sys.modules, "onnxruntime", mock_ort)

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.apply_onnx_thread_limits()

    sess_opts = mock_ort.SessionOptions()

    # Case 1: default settings (MODEL_THREADS = 2)
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.MODEL_THREADS = 2
        mock_settings_cls.return_value = mock_settings

        session = mock_ort.InferenceSession("some_model.onnx", sess_opts)
        assert sess_opts.intra_op_num_threads == 2
        assert sess_opts.inter_op_num_threads == 2

    # Case 2: custom settings (MODEL_THREADS = 4)
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.MODEL_THREADS = 4
        mock_settings_cls.return_value = mock_settings

        session = mock_ort.InferenceSession("some_model.onnx", sess_opts)
        assert sess_opts.intra_op_num_threads == 4
        assert sess_opts.inter_op_num_threads == 4

    # Case 3: out-of-bounds fallback (MODEL_THREADS = 100 -> fallback to 2)
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.MODEL_THREADS = 100
        mock_settings_cls.return_value = mock_settings

        session = mock_ort.InferenceSession("some_model.onnx", sess_opts)
        assert sess_opts.intra_op_num_threads == 2
        assert sess_opts.inter_op_num_threads == 2


def test_pytorch_thread_limits_selection(monkeypatch):
    """Verify PyTorch model loads use the configured dynamic thread limit."""
    import sys

    mock_easyocr = MagicMock()
    mock_torch = MagicMock()

    monkeypatch.setitem(sys.modules, "easyocr", mock_easyocr)
    monkeypatch.setitem(sys.modules, "torch", mock_torch)

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    # Case 1: default settings (MODEL_THREADS = 2)
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.MODEL_THREADS = 2
        mock_settings_cls.return_value = mock_settings

        registry.get_ocr_reader()
        mock_torch.set_num_threads.assert_any_call(2)

    # Case 2: custom settings (MODEL_THREADS = 4)
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.MODEL_THREADS = 4
        mock_settings_cls.return_value = mock_settings

        registry._models.pop("easyocr", None)
        registry.get_ocr_reader()
        mock_torch.set_num_threads.assert_any_call(4)


def test_no_dns_during_import():
    """Verify that no DNS or hostname resolution runs during the module import phase."""
    import importlib
    import sys

    # Remove from sys.modules if already imported to force a fresh reload/import
    if "app.core.shared_registry" in sys.modules:
        del sys.modules["app.core.shared_registry"]

    mock_gethostname = MagicMock(
        side_effect=RuntimeError(
            "socket.gethostname() should not be called at import time!"
        )
    )
    with patch("socket.gethostname", mock_gethostname):
        # Importing should not trigger the gethostname call
        import app.core.shared_registry

        importlib.reload(app.core.shared_registry)


def test_sandbox_address_resolution_blocks_external():
    """Verify that name resolution functions raise standard socket address errors on external domains inside sandbox."""
    from app.core.shared_registry import block_external_network

    with block_external_network():
        # 1. getaddrinfo
        with pytest.raises(socket.gaierror) as excinfo:
            socket.getaddrinfo("external-domain.com", 80)
        assert excinfo.value.errno == getattr(socket, "EAI_NONAME", -2)

        # 2. gethostbyname
        with pytest.raises(socket.gaierror) as excinfo:
            socket.gethostbyname("external-domain.com")
        assert excinfo.value.errno == getattr(socket, "EAI_NONAME", -2)

        # 3. gethostbyname_ex
        with pytest.raises(socket.gaierror) as excinfo:
            socket.gethostbyname_ex("external-domain.com")
        assert excinfo.value.errno == getattr(socket, "EAI_NONAME", -2)

        # 4. gethostbyaddr
        with pytest.raises(socket.herror) as excinfo:
            socket.gethostbyaddr("8.8.8.8")
        assert excinfo.value.args[0] == 1

        # 5. getnameinfo
        with pytest.raises(socket.gaierror) as excinfo:
            socket.getnameinfo(("8.8.8.8", 80), 0)
        assert excinfo.value.errno == getattr(socket, "EAI_NONAME", -2)

        # 6. getfqdn returns the original domain name immediately without query
        assert socket.getfqdn("external-domain.com") == "external-domain.com"


def test_sandbox_address_resolution_allows_local():
    """Verify that localhost and loopback queries are unblocked inside sandboxed execution."""
    from app.core.shared_registry import block_external_network

    mock_gai = MagicMock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    )
    mock_ghn = MagicMock(return_value="127.0.0.1")
    mock_ghne = MagicMock(return_value=("localhost", [], ["127.0.0.1"]))
    mock_gha = MagicMock(return_value=("localhost", [], ["127.0.0.1"]))
    mock_gni = MagicMock(return_value=("localhost", "http"))
    mock_gfq = MagicMock(return_value="localhost")

    with (
        patch("app.core.shared_registry._original_getaddrinfo", mock_gai),
        patch("app.core.shared_registry._original_gethostbyname", mock_ghn),
        patch("app.core.shared_registry._original_gethostbyname_ex", mock_ghne),
        patch("app.core.shared_registry._original_gethostbyaddr", mock_gha),
        patch("app.core.shared_registry._original_getnameinfo", mock_gni),
        patch("app.core.shared_registry._original_getfqdn", mock_gfq),
        block_external_network(),
    ):
        # 1. getaddrinfo
        res1 = socket.getaddrinfo("localhost", 80)
        assert res1 == [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        mock_gai.assert_called_once_with("localhost", 80)

        # 2. gethostbyname
        res2 = socket.gethostbyname("127.0.0.1")
        assert res2 == "127.0.0.1"
        mock_ghn.assert_called_once_with("127.0.0.1")

        # 3. gethostbyname_ex
        res3 = socket.gethostbyname_ex("localhost")
        assert res3 == ("localhost", [], ["127.0.0.1"])
        mock_ghne.assert_called_once_with("localhost")

        # 4. gethostbyaddr
        res4 = socket.gethostbyaddr("127.0.0.1")
        assert res4 == ("localhost", [], ["127.0.0.1"])
        mock_gha.assert_called_once_with("127.0.0.1")

        # 5. getnameinfo
        res5 = socket.getnameinfo(("127.0.0.1", 80), 0)
        assert res5 == ("localhost", "http")
        mock_gni.assert_called_once_with(("127.0.0.1", 80), 0)

        # 6. getfqdn
        res6 = socket.getfqdn("localhost")
        assert res6 == "localhost"
        mock_gfq.assert_called_once_with("localhost")


def test_sandbox_address_resolution_inactive_allows_all():
    """Verify that external lookups are permitted and call original methods when block is inactive."""
    mock_gai = MagicMock(return_value=[])
    mock_ghn = MagicMock(return_value="93.184.216.34")

    with (
        patch("app.core.shared_registry._original_getaddrinfo", mock_gai),
        patch("app.core.shared_registry._original_gethostbyname", mock_ghn),
    ):
        res1 = socket.getaddrinfo("example.com", 80)
        assert res1 == []
        mock_gai.assert_called_once_with("example.com", 80)

        res2 = socket.gethostbyname("example.com")
        assert res2 == "93.184.216.34"
        mock_ghn.assert_called_once_with("example.com")


def test_sandbox_address_resolution_supports_mocks():
    """Verify that mock/magic mock interfaces are supported and don't cause failures or blocks."""
    from app.core.shared_registry import block_external_network

    # If the original getaddrinfo is a Mock, it should bypass sandbox and return mock's value
    mock_orig = MagicMock(return_value="mocked-result")

    with (
        patch("app.core.shared_registry._original_getaddrinfo", mock_orig),
        block_external_network(),
    ):
        res = socket.getaddrinfo("example.com", 80)
        assert res == "mocked-result"
        mock_orig.assert_called_once_with("example.com", 80)

    # If host argument is a Mock, it should also bypass and be processed by original
    mock_host = MagicMock()
    mock_orig_host = MagicMock(return_value="mocked-host-result")
    with (
        patch("app.core.shared_registry._original_getaddrinfo", mock_orig_host),
        block_external_network(),
    ):
        res_host = socket.getaddrinfo(mock_host, 80)
        assert res_host == "mocked-host-result"
        mock_orig_host.assert_called_once_with(mock_host, 80)


def test_hardware_helpers():
    """Test environment helper hardware check functions."""
    from app.core.env_helper import is_cuda_available, is_mps_available

    with patch("torch.cuda.is_available", return_value=True):
        assert is_cuda_available() is True

    with patch("torch.cuda.is_available", return_value=False):
        assert is_cuda_available() is False

    with patch("torch.backends.mps.is_available", return_value=True):
        assert is_mps_available() is True

    with patch("torch.backends.mps.is_available", return_value=False):
        assert is_mps_available() is False


def test_get_ocr_reader_dynamic_config(monkeypatch):
    """Test that get_ocr_reader reloads and uses the custom language and GPU settings."""
    import sys

    mock_easyocr = MagicMock()
    mock_torch = MagicMock()

    monkeypatch.setitem(sys.modules, "easyocr", mock_easyocr)
    monkeypatch.setitem(sys.modules, "torch", mock_torch)

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    # Dynamic settings test
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "en,de"
        mock_settings.OCR_GPU_ENABLED = True
        mock_settings_cls.return_value = mock_settings

        with (
            patch("app.core.env_helper.is_cuda_available", return_value=True),
            patch("app.core.env_helper.is_mps_available", return_value=False),
        ):
            registry.get_ocr_reader()

            # Ensure Reader called with ["en", "de"] and gpu=True
            from unittest.mock import ANY

            mock_easyocr.Reader.assert_any_call(
                ["en", "de"],
                gpu=True,
                model_storage_directory=ANY,
                download_enabled=False,
            )


def test_get_ocr_reader_fallback(monkeypatch):
    """Test that get_ocr_reader falls back to 'en' and cpu if initialization fails."""
    import sys

    mock_easyocr = MagicMock()
    mock_torch = MagicMock()

    # Make first initialization raise an exception (e.g. invalid language)
    mock_easyocr.Reader.side_effect = [Exception("Init failed"), MagicMock()]

    monkeypatch.setitem(sys.modules, "easyocr", mock_easyocr)
    monkeypatch.setitem(sys.modules, "torch", mock_torch)

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "unsupported_lang"
        mock_settings.OCR_GPU_ENABLED = True
        mock_settings_cls.return_value = mock_settings

        registry.get_ocr_reader()

        # It should try once with the configured unsupported language, fail,
        # and then initialize with "en" and gpu=False
        from unittest.mock import ANY

        assert mock_easyocr.Reader.call_count == 2
        mock_easyocr.Reader.assert_any_call(
            ["en"],
            gpu=False,
            model_storage_directory=ANY,
            download_enabled=False,
        )


def test_check_ai_status_local_offline_bundle(tmp_path, monkeypatch):
    """Verify that check_ai_status and get_ocr_reader can locate and verify easyocr and model files in the offline_bundle directory."""
    from app.config import AppSettings
    from app.core.shared_registry import SharedModelRegistry
    from app.core.verifier import check_ai_status

    settings = AppSettings()
    settings.AI_ASSISTED_NAMING = True

    # Setup directories
    base_dir = tmp_path
    offline_bundle_dir = base_dir / "offline_bundle"
    model_dir = offline_bundle_dir / "model"
    easyocr_dir = offline_bundle_dir / "easyocr"

    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    # Create empty dummy files to satisfy path existence checks
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (easyocr_dir / "craft_mlt_25k.pth").write_text("", encoding="utf-8")
    (easyocr_dir / "english_g2.pth").write_text("", encoding="utf-8")

    # Mock get_base_path to point to our tmp_path base_dir
    monkeypatch.setattr("app.core.path_utils.get_base_path", lambda *args, **kwargs: str(base_dir))
    monkeypatch.setattr("os.getcwd", lambda: str(base_dir))

    # Mock is_ml_available to True
    with (
        patch("app.core.verifier.is_ml_available", return_value=True),
        patch("app.core.shared_registry.SharedModelRegistry.verify_integrity", return_value=True) as mock_verify,
    ):
        is_healthy, warn_msg = check_ai_status(settings)
        # Should be healthy, with no warning/error message
        assert is_healthy is True
        assert warn_msg is None

        # Verify that get_ocr_reader also correctly resolves the directory
        registry = SharedModelRegistry.get_instance()
        with patch("easyocr.Reader") as mock_reader:
            registry.get_ocr_reader()
            _, kwargs = mock_reader.call_args
            assert "offline_bundle" in kwargs.get("model_storage_directory", "")
            assert "easyocr" in kwargs.get("model_storage_directory", "")

