"""Recovery UI views for offline recovery code and restore screen."""

from pathlib import Path

from nicegui import ui

from app.core.crypto import SessionCrypto
from app.core.db_conn import clear_connection_cache


def show_recovery_code_onboarding(parent_app, recovery_code: str):
    """Show the generated recovery code to the user during initial setup."""
    with ui.dialog() as dialog, ui.card().classes("w-96 p-6"):
        dialog.props("persistent")
        ui.label("Your Offline Recovery Code").classes("text-xl font-bold mb-2").props(
            'aria-label="Recovery Code Title"'
        )
        ui.label(
            "This 24-character code is required to recover your database if your system keychain is ever reset. "
            "Please copy or write this code down and store it in a safe offline location. "
            "This code will NOT be saved on your computer."
        ).classes("text-sm mb-4")

        # Show the code in a prominent box
        with ui.row().classes(
            "w-full justify-center items-center bg-gray-100 p-3 rounded mb-4"
        ):
            ui.label(recovery_code).classes(
                "font-mono text-lg font-bold tracking-wider select-all"
            ).props('aria-label="Recovery Code Label"')

        # A copy button
        def copy_to_clipboard():
            ui.run_javascript(f"navigator.clipboard.writeText('{recovery_code}')")
            ui.notify("Recovery code copied to clipboard!", type="positive")

        with ui.row().classes("w-full justify-between items-center"):
            ui.button("Copy to Clipboard", on_click=copy_to_clipboard).props(
                'aria-label="Copy to Clipboard Button"'
            )
            ui.button("I have saved it", on_click=dialog.close).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Acknowledge Button"')

    dialog.open()


def show_recovery_screen(
    parent_app, session_id: str, session_info: dict, action_type: str, db_path: Path
):
    """Show the dedicated database recovery screen on the main UI thread."""
    key_path = db_path.parent / "secret.key"
    crypto = SessionCrypto(key_path, db_path)

    with ui.dialog() as recovery_dialog, ui.card().classes("w-full max-w-lg p-6"):
        recovery_dialog.props("persistent")

        ui.label("Database Protection Error").classes(
            "text-xl font-bold text-red-500 mb-2"
        ).props('aria-label="Recovery Dialog Title"')
        ui.label(
            "The database decryption key is missing or invalid (potentially due to a keychain reset). "
            "To restore access, please enter your 24-character offline recovery code."
        ).classes("text-sm mb-4")

        # Input field for 24-character code
        recovery_input = (
            ui.input(
                label="Recovery Code",
                placeholder="Enter your 24-character recovery code...",
                validation=lambda v: (
                    "Must be exactly 24 alphanumeric characters"
                    if len(v) != 24 or not v.isalnum()
                    else None
                ),
            )
            .classes("w-full mb-4")
            .props('aria-label="Recovery Code Input"')
        )

        error_label = (
            ui.label("")
            .classes("text-sm text-red-500 mb-4")
            .props('aria-label="Error Label"')
        )
        error_label.set_visibility(False)

        async def attempt_restore():
            code = (recovery_input.value or "").strip()
            if len(code) != 24 or not code.isalnum():
                error_label.set_text(
                    "Please enter a valid 24-character alphanumeric code."
                )
                error_label.set_visibility(True)
                return

            # Verify and restore
            if crypto.verify_recovery_code(code):
                error_label.set_visibility(False)
                # Securely write key back to keyring
                crypto.save_recovered_key(code)
                clear_connection_cache()
                recovery_dialog.close()
                ui.notify(
                    "Database decrypted and restored successfully!", type="positive"
                )

                # Retry the original action
                if action_type == "resume" and session_info:
                    parent_app.resume_session(session_info)
                elif action_type == "revert" and session_info:
                    parent_app.revert_session(session_info)
                else:
                    parent_app.start_analysis()
            else:
                error_label.set_text(
                    "Invalid recovery code. The code does not match this database."
                )
                error_label.set_visibility(True)

        async def rebuild_db_action():
            # Double-confirmation dialog
            with ui.dialog() as confirm_dialog_1, ui.card().classes("p-6"):
                ui.label("Permanent Deletion Confirmation").classes(
                    "text-lg font-bold text-red-500 mb-2"
                )
                ui.label(
                    "Are you sure you want to permanently delete your existing encrypted database? This action is irreversible."
                ).classes("mb-4")

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=confirm_dialog_1.close).props(
                        'aria-label="Cancel Confirmation Button"'
                    )

                    async def confirm_2():
                        confirm_dialog_1.close()
                        with ui.dialog() as confirm_dialog_2, ui.card().classes("p-6"):
                            ui.label("FINAL WARNING: Permanent Data Loss").classes(
                                "text-lg font-bold text-red-500 mb-2"
                            )
                            ui.label(
                                "Please confirm once more. All your historical records and custom rules in this session database will be permanently deleted."
                            ).classes("mb-4")

                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button(
                                    "Cancel", on_click=confirm_dialog_2.close
                                ).props('aria-label="Cancel Final Confirmation Button"')

                                async def execute_rebuild():
                                    confirm_dialog_2.close()
                                    # Completely delete the locked database file, WAL/SHM files, and any secret.key
                                    db_base = str(db_path)
                                    for ext in ["", "-wal", "-shm"]:
                                        f_path = Path(f"{db_base}{ext}")
                                        if f_path.exists():
                                            try:
                                                f_path.unlink()
                                            except Exception:
                                                pass
                                    if key_path.exists():
                                        try:
                                            key_path.unlink()
                                        except Exception:
                                            pass

                                    # Delete from keyring if possible
                                    try:
                                        import keyring

                                        keyring.delete_password(
                                            crypto.keyring_service,
                                            crypto.keyring_account,
                                        )
                                    except Exception:
                                        pass

                                    clear_connection_cache()
                                    recovery_dialog.close()
                                    ui.notify(
                                        "Inaccessible database deleted. Initializing a fresh database...",
                                        type="warning",
                                    )

                                    # Start a brand new analysis/setup sequence
                                    if parent_app.app_session:
                                        parent_app.app_session.close()
                                        parent_app.app_session = None
                                    parent_app.start_analysis()

                                ui.button(
                                    "Delete Permanently", on_click=execute_rebuild
                                ).classes("bg-red-500 text-white").props(
                                    'aria-label="Confirm Rebuild Button"'
                                )
                        confirm_dialog_2.open()

                    ui.button("Proceed", on_click=confirm_2).classes(
                        "bg-orange-500 text-white"
                    ).props('aria-label="Proceed Rebuild Button"')
            confirm_dialog_1.open()

        with ui.row().classes("w-full justify-between items-center mt-4"):
            ui.button(
                "Rebuild Database (Starts Fresh)", on_click=rebuild_db_action
            ).classes("bg-red-500 text-white").props(
                'aria-label="Rebuild Database Button"'
            )
            ui.button("Restore Access", on_click=attempt_restore).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Restore Access Button"')

    recovery_dialog.open()
