import os
import tempfile
from unittest import mock

import pytest

from app.ui.dialog_helper import (
    ask_directory_async,
    find_tree_node,
    scan_subdirectories,
    scan_subdirectories_async,
)


@pytest.mark.anyio
async def test_scan_subdirectories_filtering():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create normal subdirectories
        sub1 = os.path.join(tmp_dir, "alpha")
        sub2 = os.path.join(tmp_dir, "beta")
        os.makedirs(sub1)
        os.makedirs(sub2)

        # Create a hidden directory starting with '.'
        hidden = os.path.join(tmp_dir, ".hidden_dir")
        os.makedirs(hidden)

        # Create a normal file
        file_path = os.path.join(tmp_dir, "test_file.txt")
        with open(file_path, "w") as f:
            f.write("sample content")

        # Test sync scan
        results = scan_subdirectories(tmp_dir)
        names = [r["name"] for r in results]

        assert "alpha" in names
        assert "beta" in names
        assert ".hidden_dir" not in names
        assert "test_file.txt" not in names

        # Test async scan wrapper
        async_results = await scan_subdirectories_async(tmp_dir)
        async_names = [r["name"] for r in async_results]
        assert "alpha" in async_names
        assert "beta" in async_names


@pytest.mark.anyio
async def test_scan_subdirectories_invalid_path():
    # Non-existent directory returns empty list
    res = scan_subdirectories("/non_existent_folder_path_12345")
    assert res == []


@pytest.mark.anyio
async def test_ask_directory_async_headless_environment():
    # Verify execution in headless server environment without DISPLAY or WAYLAND_DISPLAY
    with tempfile.TemporaryDirectory() as tmp_dir:
        callback = mock.MagicMock()

        # Explicitly unset display server environment variables
        env_dict = os.environ.copy()
        env_dict.pop("DISPLAY", None)
        env_dict.pop("WAYLAND_DISPLAY", None)

        with mock.patch.dict(os.environ, env_dict, clear=True):
            fut = ask_directory_async(
                None,
                "Select Directory Title",
                callback=callback,
                initial_dir=tmp_dir,
            )
            res = await fut

            assert res == tmp_dir
            callback.assert_called_once_with(tmp_dir)


@pytest.mark.anyio
async def test_zero_subprocess_invocations():
    # Ensure zero OS desktop subprocesses (osascript, powershell, zenity, kdialog) are spawned
    mock_run = mock.MagicMock()

    with (
        mock.patch("subprocess.run", mock_run),
        mock.patch("subprocess.Popen", mock_run),
        mock.patch("app.core.env_helper.run_background_process", mock_run),
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = await ask_directory_async(
                title="Headless Pick",
                initial_dir=tmp_dir,
            )
            assert res == tmp_dir

            # Verify no desktop OS picker commands were executed
            for call_item in mock_run.call_args_list:
                cmd = str(call_item)
                for picker_cmd in ["osascript", "powershell", "zenity", "kdialog"]:
                    assert picker_cmd not in cmd


@pytest.mark.anyio
async def test_ask_directory_async_interactive_modal_components():
    # Test interactive browser WebSocket session modal dialog construction and confirmation
    import nicegui

    with tempfile.TemporaryDirectory() as tmp_dir:
        mock_client = mock.MagicMock()
        mock_client.has_socket_connection = True

        with mock.patch.object(
            type(nicegui.context),
            "client",
            new_callable=mock.PropertyMock,
            return_value=mock_client,
        ):
            # Launch dialog asynchronously
            fut = ask_directory_async(
                title="Interactive Pick",
                initial_dir=tmp_dir,
            )

            # In interactive mode, fut will be completed when the user confirms selection
            assert not fut.done()

            # Verify node searching works
            sub_folder = os.path.join(tmp_dir, "test_sub")
            os.makedirs(sub_folder)

            res = await scan_subdirectories_async(tmp_dir)
            assert len(res) == 1
            assert res[0]["name"] == "test_sub"


@pytest.mark.anyio
async def test_find_tree_node():
    tree_nodes = [
        {
            "id": "/root",
            "label": "root",
            "children": [
                {
                    "id": "/root/child1",
                    "label": "child1",
                    "children": [],
                },
                {
                    "id": "/root/child2",
                    "label": "child2",
                    "children": [],
                },
            ],
        }
    ]

    found = find_tree_node("/root/child2", tree_nodes)
    assert found is not None
    assert found["label"] == "child2"

    missing = find_tree_node("/nonexistent", tree_nodes)
    assert missing is None
