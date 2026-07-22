import collections
import ntpath
import posixpath
from unittest.mock import MagicMock, patch

from app.core.verifier import VerificationEngine


class VirtualFS:
    def __init__(self, os_system="Linux", base_dir=None):
        self.os_system = os_system
        self.files = {}
        self.dirs = set()
        self.symlinks = {}
        self.locked_files = set()
        self.unreadable_files = set()
        self.disk_usages = {}
        self.symlink_privilege = True
        self.path_module = ntpath if os_system == "Windows" else posixpath

        self.base_dir = (
            base_dir
            if base_dir is not None
            else ("C:\\app" if os_system == "Windows" else "/app")
        )
        self.dirs.add(self.base_dir)

        self.raise_on_disk_usage = set()
        self.raise_on_symlink = False
        self.raise_on_open_write = False

    def ismount(self, path):
        return path in self.disk_usages

    def disk_usage(self, path):
        if path in self.raise_on_disk_usage:
            raise OSError(f"Disk check failed for {path}")
        if path not in self.disk_usages:
            raise FileNotFoundError(f"No such device: {path}")
        Usage = collections.namedtuple("usage", "total used free")
        return Usage(*self.disk_usages[path])

    def abspath(self, path):
        if not self.path_module.isabs(path):
            return self.path_module.join(self.base_dir, path)
        return path

    def exists(self, path):
        path = self.abspath(path)
        return path in self.files or path in self.dirs or path in self.disk_usages

    def lexists(self, path):
        path = self.abspath(path)
        return self.exists(path) or path in self.symlinks

    def islink(self, path):
        path = self.abspath(path)
        return path in self.symlinks

    def getsize(self, path):
        path = self.abspath(path)
        return self.files.get(path, 0)

    def access(self, path, mode):
        path = self.abspath(path)
        if mode == __import__("os").R_OK and path in self.unreadable_files:
            return False
        return True

    def open(self, path, mode="r", *args, **kwargs):
        path = self.abspath(path)
        if mode == "w" and self.raise_on_open_write:
            raise PermissionError("Write denied")
        if path in self.locked_files:
            raise IOError("File is locked")
        if path in self.unreadable_files:
            raise PermissionError("Permission denied")

        if mode == "w":
            self.files[path] = 0

        mock = MagicMock()
        mock.__enter__.return_value = mock
        return mock

    def symlink(self, src, dst):
        if not self.symlink_privilege or self.raise_on_symlink:
            raise OSError("Privilege not held")
        self.symlinks[self.abspath(dst)] = self.abspath(src)

    def remove(self, path):
        path = self.abspath(path)
        if path in self.files:
            del self.files[path]
        if path in self.symlinks:
            del self.symlinks[path]


def run_with_vfs(vfs: VirtualFS, func, *args, **kwargs):
    with (
        patch("app.core.verifier.os.path.abspath", side_effect=vfs.abspath),
        patch("app.core.verifier.os.path.exists", side_effect=vfs.exists),
        patch("app.core.verifier.os.path.dirname", side_effect=vfs.path_module.dirname),
        patch(
            "app.core.verifier.os.path.basename", side_effect=vfs.path_module.basename
        ),
        patch("app.core.verifier.os.path.join", side_effect=vfs.path_module.join),
        patch("app.core.verifier.os.path.ismount", side_effect=vfs.ismount),
        patch("app.core.verifier.os.path.lexists", side_effect=vfs.lexists),
        patch("app.core.verifier.os.path.islink", side_effect=vfs.islink),
        patch("app.core.verifier.os.path.getsize", side_effect=vfs.getsize),
        patch(
            "app.core.verifier.os.path.splitdrive",
            side_effect=vfs.path_module.splitdrive,
        ),
        patch("app.core.verifier.os.access", side_effect=vfs.access),
        patch("app.core.verifier.os.symlink", side_effect=vfs.symlink),
        patch("app.core.verifier.os.remove", side_effect=vfs.remove),
        patch("app.core.verifier.platform.system", return_value=vfs.os_system),
        patch("app.core.verifier.os.sep", vfs.path_module.sep),
        patch("app.core.verifier.os.altsep", vfs.path_module.altsep),
        patch("app.core.verifier.shutil.disk_usage", side_effect=vfs.disk_usage),
        patch("builtins.open", side_effect=vfs.open),
    ):
        return func(*args, **kwargs)


# === TESTS ===


def test_windows_path_limit():
    vfs = VirtualFS(os_system="Windows", base_dir="C:\\app")
    vfs.disk_usages["C:\\"] = (1000, 500, 500)
    vfs.files["C:\\app\\test.txt"] = 100

    long_dest = "a" * 255
    plan = {"test.txt": {"__type__": "file", "target_filename": long_dest}}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "C:\\app", plan)

    # Autocorrect should fix the path, so no errors should be returned
    assert errors == {}


def test_linux_path_limit():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.files["/app/test.txt"] = 100

    # Filename limit
    long_dest = "a" * 256
    plan = {"test.txt": {"__type__": "file", "target_filename": long_dest}}
    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    # Autocorrect should fix it
    assert errors == {}

    # Path limit
    deep_path = "a" * 200
    for i in range(20):
        deep_path += "/" + ("a" * 200)

    plan_nested = {deep_path: {"test.txt": None}}
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan_nested)
    assert errors["test.txt"] == "Path exceeds 4096 characters"


def test_insufficient_disk_space():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.disk_usages["/mnt/backup"] = (1000, 990, 10)  # 10 free
    vfs.files["/app/big.txt"] = 100

    # Absolute path as category to force cross-volume move
    plan = {"/mnt/backup": {"big.txt": None}}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    assert "big.txt" in errors
    assert errors["big.txt"] == "Insufficient disk space"


def test_disk_usage_exception_logging():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.disk_usages["/mnt/backup"] = (1000, 500, 500)
    vfs.files["/app/big.txt"] = 100
    vfs.raise_on_disk_usage.add("/mnt/backup")

    plan = {"/mnt/backup": {"big.txt": None}}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    # Shouldn't fail the verification completely but log the error
    assert "big.txt" not in errors


def test_locked_and_unreadable_files():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.files["/app/locked.txt"] = 100
    vfs.files["/app/unreadable.txt"] = 100
    vfs.files["/app/missing.txt"] = 100

    vfs.locked_files.add("/app/locked.txt")
    vfs.unreadable_files.add("/app/unreadable.txt")

    # Missing files just get false from lexists
    del vfs.files["/app/missing.txt"]

    plan = {"locked.txt": None, "unreadable.txt": None, "missing.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)

    assert errors["locked.txt"] == "File is locked or inaccessible"
    assert errors["unreadable.txt"] == "File is locked or inaccessible"
    assert errors["missing.txt"] == "File is locked or inaccessible"


@patch("app.core.verifier.LinkManager.get_link_info")
def test_symlink_privilege_blocked(mock_get_link_info):
    vfs = VirtualFS(os_system="Windows", base_dir="C:\\app")
    vfs.disk_usages["C:\\"] = (1000, 500, 500)
    vfs.files["C:\\app\\symlink.txt"] = 100
    vfs.symlink_privilege = False

    mock_get_link_info.return_value = {"type": "symlink", "target": "C:\\target.txt"}

    plan = {"symlink.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "C:\\app", plan)

    assert (
        errors["symlink.txt"]
        == "Operating system blocks link modification due to permission constraints"
    )


@patch("app.core.verifier.LinkManager.get_link_info")
def test_symlink_privilege_granted_and_accessible(mock_get_link_info):
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.symlinks["/app/symlink.txt"] = "/app/target.txt"
    vfs.symlink_privilege = True

    mock_get_link_info.return_value = {"type": "symlink", "target": "/app/target.txt"}

    plan = {"symlink.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)

    # Symlink is checked with os.access, we simulate it being accessible
    assert "symlink.txt" not in errors


@patch("app.core.verifier.LinkManager.get_link_info")
def test_symlink_privilege_check_exception_during_write(mock_get_link_info):
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.symlinks["/app/symlink.txt"] = "/app/target.txt"
    vfs.raise_on_open_write = True

    mock_get_link_info.return_value = {"type": "symlink"}

    plan = {"symlink.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    assert (
        errors["symlink.txt"]
        == "Operating system blocks link modification due to permission constraints"
    )


@patch("app.core.verifier.LinkManager.get_link_info")
def test_symlink_privilege_check_exception_during_symlink(mock_get_link_info):
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.symlinks["/app/symlink.txt"] = "/app/target.txt"
    vfs.raise_on_symlink = True

    mock_get_link_info.return_value = {"type": "symlink"}

    plan = {"symlink.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    assert (
        errors["symlink.txt"]
        == "Operating system blocks link modification due to permission constraints"
    )


def test_get_volume_linux_fallback():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    # Only root is a mount
    vfs.disk_usages["/"] = (1000, 500, 500)

    # Add files that don't exist
    plan = {"nonexistent.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)

    # It should trace up to '/' to find the volume
    # Then it will error because nonexistent.txt doesn't exist
    assert errors["nonexistent.txt"] == "File is locked or inaccessible"


def test_get_volume_linux_no_mount():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    # Empty disk usages, so no mount point exists!
    vfs.disk_usages.clear()

    plan = {"file.txt": None}

    engine = VerificationEngine()
    # It will hit the breaks in get_volume
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)

    # Since there are no disk usages, net_change won't trigger any errors.
    # But getsize on file.txt will just be 0
    # And access will be false since it doesn't exist
    assert errors["file.txt"] == "File is locked or inaccessible"


def test_get_volume_linux_no_exists_loop():
    vfs = VirtualFS(os_system="Linux", base_dir="/nonexistent_base")
    vfs.dirs.clear()  # clear the base_dir from exists
    vfs.disk_usages.clear()

    plan = {"file.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/nonexistent_base", plan)

    assert errors["file.txt"] == "File is locked or inaccessible"


def test_sufficient_disk_space():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.disk_usages["/mnt/backup"] = (1000, 500, 500)  # 500 free
    vfs.files["/app/small.txt"] = 100

    plan = {"/mnt/backup": {"small.txt": None}}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    # usage.free (500) >= net_change (100), so 115->111 branch taken
    assert "small.txt" not in errors


def test_insufficient_space_different_file():
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.disk_usages["/mnt/backup"] = (1000, 990, 10)  # 10 free
    vfs.files["/app/big.txt"] = 100
    vfs.files["/mnt/backup/other.txt"] = 100

    plan = {
        "/mnt/backup": {
            "big.txt": None
        },  # this causes insufficient space on /mnt/backup
        "/": {
            "other.txt": None
        },  # this is moving from backup to /, freeing space on backup, but let's just make it a move
    }

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    # While checking /mnt/backup, big.txt is moving TO it, but other.txt is moving FROM it to / (so it's not moving TO /mnt/backup).
    # That triggers 117->116 branch (the if self._get_volume(dst) == vol evaluates to False for other.txt)
    assert "big.txt" in errors


@patch("app.core.verifier.LinkManager.get_link_info")
def test_multiple_symlinks(mock_get_link_info):
    vfs = VirtualFS(os_system="Linux", base_dir="/app")
    vfs.disk_usages["/"] = (1000, 500, 500)
    vfs.symlinks["/app/symlink1.txt"] = "/app/target1.txt"
    vfs.symlinks["/app/symlink2.txt"] = "/app/target2.txt"
    vfs.symlink_privilege = True

    # Return symlink for all
    mock_get_link_info.return_value = {"type": "symlink", "target": "/app/target.txt"}

    plan = {"symlink1.txt": None, "symlink2.txt": None}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "/app", plan)
    # The second symlink will find symlink_privilege is not None (Branch 139->141)
    assert not errors


def test_windows_normal_path():
    vfs = VirtualFS(os_system="Windows", base_dir="C:\\app")
    vfs.disk_usages["C:\\"] = (1000, 500, 500)
    vfs.files["C:\\app\\test.txt"] = 100

    plan = {"test.txt": {"__type__": "file", "target_filename": "normal.txt"}}

    engine = VerificationEngine()
    errors = run_with_vfs(vfs, engine.verify_plan, "C:\\app", plan)
    # len(dst) < 260 triggers Branch 148->156
    assert not errors
