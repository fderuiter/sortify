"""Re-exports for callers of the former shared registry module (#711).

The model registry lives in ``app.core.model_registry``, the worker pool and
audio guard in ``app.core.worker_pool``, the context-propagating thread helpers
in ``app.core.concurrency`` and the network sandbox in
``app.core.network_guard``. New code should import from those modules.
"""

from app.core.concurrency import (  # noqa: F401
    ContextPropagatingThread,
    ContextPropagatingThreadPoolExecutor,
    ContextVarLocal,
)
from app.core.model_registry import SharedModelRegistry  # noqa: F401
from app.core.network_guard import (  # noqa: F401
    _is_local_address,
    _thread_local,
    block_external_network,
    safe_connect,
    safe_connect_ex,
    safe_getaddrinfo,
    safe_getfqdn,
    safe_gethostbyaddr,
    safe_gethostbyname,
    safe_gethostbyname_ex,
    safe_getnameinfo,
    unblock_external_network,
)
from app.core.network_guard import install as apply_global_socket_sandbox  # noqa: F401
from app.core.worker_pool import AudioConcurrencyGuard, SharedWorkerPool  # noqa: F401
