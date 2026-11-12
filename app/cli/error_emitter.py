"""Centralized CLI error formatting and emission helper."""

import json
import sys
from typing import Any, NoReturn


def emit_cli_error(
    message: str | BaseException,
    *,
    status: str = "error",
    exit_code: int = 1,
    json_mode: bool = False,
    extra: dict[str, Any] | None = None,
    stream: Any = None,
) -> NoReturn:
    """Format and emit a CLI error payload or text message, then terminate the process.

    Parameters
    ----------
    message : str or BaseException
        The error message string or exception instance to format.
    status : str, default="error"
        Status indicator included in structured JSON output ('error' or 'failed').
    exit_code : int, default=1
        Process exit code to pass to sys.exit (e.g., 1 for operations, 2 for arguments).
    json_mode : bool, default=False
        Whether structured JSON output was requested.
    extra : dict of str to Any, optional
        Additional contextual fields to include in the JSON payload (e.g. job_id).
    stream : Any, optional
        Explicit stream override for writing output (defaults to sys.stdout for JSON,
        sys.stderr for plain text).
    """
    raw_msg = (
        str(message).strip()
        if str(message).strip()
        else (
            type(message).__name__
            if isinstance(message, BaseException)
            else "An unexpected error occurred."
        )
    )

    if raw_msg.startswith("Error: "):
        human_msg = raw_msg
        clean_msg = raw_msg[len("Error: ") :].strip()
    elif raw_msg.lower().startswith("error"):
        human_msg = raw_msg
        clean_msg = raw_msg
    else:
        human_msg = f"Error: {raw_msg}"
        clean_msg = raw_msg

    if json_mode:
        from app.cli import json_default

        payload: dict[str, Any] = {
            "status": status,
            "message": clean_msg or human_msg,
        }
        if extra:
            payload.update(extra)

        target_stream = stream if stream is not None else sys.stdout
        target_stream.write(json.dumps(payload, indent=2, default=json_default) + "\n")
        if hasattr(target_stream, "flush"):
            target_stream.flush()
    else:
        target_stream = stream if stream is not None else sys.stderr
        print(human_msg, file=target_stream)

    sys.exit(exit_code)
