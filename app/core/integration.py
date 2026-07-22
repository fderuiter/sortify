"""Windows Context Menu integration module.

Provides utilities to register or unregister the application
in the Windows Explorer context menu.
"""

import os
import sys


def is_admin():
    """Check if the current process has administrative privileges."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def register_context_menu(enable: bool):
    """Register or unregister context menu handles."""
    if sys.platform != "win32":
        raise OSError("Context menu integration is only available on Windows.")

    import ctypes

    # We will invoke a separate process for elevation if not admin
    if not is_admin():
        # Elevate and run the registration script
        script_path = os.path.abspath(__file__)
        action = "enable" if enable else "disable"

        # Relaunch with elevation
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script_path}" {action}', None, 1
        )
        if int(ret) <= 32:
            raise RuntimeError(
                f"Failed to elevate privileges. ShellExecuteW returned {ret}"
            )
        return True

    # If we are admin, do the registry changes
    import winreg

    if getattr(sys, "frozen", False):
        app_exe = f'"{sys.executable}"'
    else:
        main_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "main.py")
        )
        app_exe = f'"{sys.executable}" "{main_script}"'

    command_str = f'{app_exe} "%1"'
    bg_command_str = f'{app_exe} "%V"'

    prog_name = "Open in Smart Auto-Sorter"

    def _set_keys(root_key, path, enable_flag, cmd):
        try:
            if enable_flag:
                # Create key
                key = winreg.CreateKey(root_key, path)
                winreg.SetValue(key, "", winreg.REG_SZ, prog_name)
                # Set icon (optional)
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, sys.executable)

                cmd_key = winreg.CreateKey(key, "command")
                winreg.SetValue(cmd_key, "", winreg.REG_SZ, cmd)

                winreg.CloseKey(cmd_key)
                winreg.CloseKey(key)
            else:
                # Delete key
                winreg.DeleteKey(root_key, f"{path}\\command")
                winreg.DeleteKey(root_key, path)
        except FileNotFoundError:
            pass
        except Exception as e:
            raise e

    try:
        # Register for directories (Right clicking a folder)
        _set_keys(
            winreg.HKEY_CLASSES_ROOT,
            r"Directory\shell\SmartAutoSorter",
            enable,
            command_str,
        )

        # Register for directory background (Right clicking inside an empty space in a folder)
        _set_keys(
            winreg.HKEY_CLASSES_ROOT,
            r"Directory\Background\shell\SmartAutoSorter",
            enable,
            bg_command_str,
        )
    except Exception as e:
        raise RuntimeError(f"Registry operation failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        action = sys.argv[1]
        try:
            register_context_menu(action == "enable")
        except Exception as e:
            import logging

            logging.basicConfig(filename="reg_error.log", level=logging.ERROR)
            logging.error(str(e))
