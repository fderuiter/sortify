"""Per-context network sandbox for offline model and worker execution.

Importing this module has no side effects. ``install()`` wraps the stdlib
``socket`` connect and name-resolution functions once per process; the
wrappers pass every call straight through unless the current context has
entered the sandbox via ``block_external_network()`` (or a worker that sets
``_thread_local.sandboxed``). Inside the sandbox, only loopback, private,
link-local and ``.local`` hosts are reachable.
"""

import ipaddress
import socket
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterable
from contextlib import contextmanager

from app.core.concurrency import ContextVarLocal

# Sandbox state for the current context. ``sandboxed`` and ``reason`` are read
# by the socket wrappers below; worker pools copy them into their tasks.
_thread_local = ContextVarLocal()

# The unwrapped stdlib functions, captured before ``install()`` replaces them.
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex
_real_gethostbyaddr = socket.gethostbyaddr
_real_getnameinfo = socket.getnameinfo
_real_getfqdn = socket.getfqdn

_LOCAL_NAMES = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
_DEFAULT_TRUSTED_HOSTS: frozenset[str] = frozenset({"huggingface.co", "hf.co"})

_install_lock = threading.Lock()
_installed = False
_local_hostname: str | None = None


def _normalize_trusted_hosts(
    trusted_hosts: Iterable[object] | None = None,
    default_hosts: Iterable[str] = _DEFAULT_TRUSTED_HOSTS,
) -> frozenset[str]:
    """Normalize optional configured trusted hostnames alongside default hosts."""
    combined = set(default_hosts)
    if not trusted_hosts:
        return frozenset(combined)
    for raw_host in trusted_hosts:
        if not isinstance(raw_host, str):
            continue
        candidate = raw_host.strip().lower().rstrip(".")
        if "://" in candidate:
            parsed_cand = urllib.parse.urlsplit(candidate)
            candidate = (parsed_cand.hostname or "").strip().lower().rstrip(".")
        if (
            not candidate
            or candidate.startswith(".")
            or candidate in _LOCAL_NAMES
            or candidate.endswith((".local", ".localhost"))
        ):
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            combined.add(candidate)
    return frozenset(combined)


def _is_allowed_outbound_host(
    hostname: str,
    trusted_hosts: Iterable[object] | None = None,
    default_hosts: Iterable[str] = _DEFAULT_TRUSTED_HOSTS,
) -> bool:
    """Check if hostname matches an allowed outbound host or trusted subdomain."""
    host = hostname.lower().rstrip(".")
    if not host or host.startswith("."):
        return False
    allowed_set = _normalize_trusted_hosts(trusted_hosts, default_hosts=default_hosts)
    return host in allowed_set or any(
        host.endswith(f".{allowed}") for allowed in allowed_set
    )


def _resolve_trusted_hosts(
    settings: object = None,
    default_hosts: Iterable[str] = _DEFAULT_TRUSTED_HOSTS,
) -> frozenset[str]:
    """Resolve normalized trusted hostnames from settings or AppSettings fallback."""
    raw_hosts = getattr(settings, "TRUSTED_MODEL_HOSTS", None)
    if raw_hosts is None:
        try:
            from app.config import AppSettings

            raw_hosts = AppSettings().TRUSTED_MODEL_HOSTS
        except (OSError, ValueError, RuntimeError):
            raw_hosts = None
    return _normalize_trusted_hosts(raw_hosts, default_hosts=default_hosts)


def _validate_outbound_https_url(
    url: str,
    *,
    trusted_hosts: Iterable[object] | None = None,
    default_hosts: Iterable[str] = _DEFAULT_TRUSTED_HOSTS,
    error_cls: type[Exception] = ValueError,
    endpoint_label: str = "outbound",
    trusted_label: str | None = None,
) -> None:
    """Validate that an outbound URL uses HTTPS and targets a non-local trusted host."""
    title_label = endpoint_label[:1].upper() + endpoint_label[1:]
    effective_trusted_label = trusted_label or endpoint_label

    if not isinstance(url, str) or not url.strip():
        raise error_cls(f"{title_label} URL must be a non-empty string.")

    cleaned_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except ValueError as err:
        raise error_cls(f"Malformed {endpoint_label} URL: {err}") from err

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise error_cls(
            f"Unsupported URL scheme '{parsed.scheme}'. Only HTTPS is permitted."
        )

    hostname = parsed.hostname
    if not hostname or not hostname.strip():
        raise error_cls("URL hostname is missing or empty.")

    host_clean = hostname.strip().lower().rstrip(".")
    if not host_clean or host_clean.startswith("."):
        raise error_cls(f"Invalid URL hostname: {hostname}")

    try:
        ip = ipaddress.ip_address(host_clean)
    except ValueError:
        pass
    else:
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise error_cls(
                f"Private or local IP addresses are not permitted: {hostname}"
            )
        raise error_cls(
            f"IP address literals are not permitted as {endpoint_label} hosts: {hostname}"
        )

    if host_clean in _LOCAL_NAMES or host_clean.endswith((".local", ".localhost")):
        raise error_cls(f"Local or loopback hostnames are not permitted: {hostname}")

    if not _is_allowed_outbound_host(
        host_clean,
        trusted_hosts=trusted_hosts,
        default_hosts=default_hosts,
    ):
        raise error_cls(
            f"Untrusted {endpoint_label} host: {hostname}. "
            f"Only trusted {effective_trusted_label} hosts are permitted."
        )


def _build_proxy_opener(
    proxy: str | None = None,
    *,
    error_cls: type[Exception] = ValueError,
) -> urllib.request.OpenerDirector:
    """Build a urllib opener for an optional proxy and reject failed decryptions."""
    p_str = str(proxy).strip() if proxy is not None else ""
    if p_str:
        if "<DECRYPTION_FAILED>" in p_str:
            raise error_cls("Invalid proxy configuration: decryption failed.")
        handler = urllib.request.ProxyHandler({"http": p_str, "https": p_str})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _get_local_hostname() -> str:
    """Return this machine's lower-cased hostname, resolved lazily once."""
    global _local_hostname
    if _local_hostname is None:
        try:
            _local_hostname = socket.gethostname().lower()
        except OSError:
            _local_hostname = ""
    return _local_hostname


def _is_sandboxed() -> bool:
    return bool(getattr(_thread_local, "sandboxed", False))


def _is_local_address(host: str) -> bool:
    """Check if the given host/IP is local/loopback/unspecified/private/link-local."""
    host_lower = host.lower()
    if host_lower in _LOCAL_NAMES:
        return True

    local_hostname = _get_local_hostname()
    if local_hostname and host_lower == local_hostname:
        return True

    if host_lower.endswith((".local", ".localhost")):
        return True

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified or ip.is_private or ip.is_link_local
    except ValueError:
        pass

    # Resolving an unknown name could leak it or hang, so never do it inside
    # the sandbox: treat it as external.
    if _is_sandboxed():
        return False

    try:
        for info in socket.getaddrinfo(host, None):
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if ip.is_loopback or ip.is_unspecified or ip.is_private or ip.is_link_local:
                return True
    except (OSError, UnicodeError):
        # Unresolvable (gaierror) or not encodable as IDNA: not provably local.
        return False

    return False


def _check_connect(address) -> None:
    if _is_sandboxed() and isinstance(address, tuple) and address:
        host = str(address[0])
        if not _is_local_address(host):
            reason = getattr(_thread_local, "reason", "worker execution")
            raise PermissionError(
                f"External network connections are blocked during {reason}: {host}"
            )


def _blocks_lookup(host) -> bool:
    return (
        _is_sandboxed() and host not in (None, "") and not _is_local_address(str(host))
    )


def _arg(args, kwargs, name):
    return args[0] if args else kwargs.get(name)


def _name_not_known() -> socket.gaierror:
    return socket.gaierror(
        getattr(socket, "EAI_NONAME", -2), "Name or service not known"
    )


def safe_connect(self, address):
    """Connect a socket, raising PermissionError for external hosts if sandboxed."""
    _check_connect(address)
    return _real_connect(self, address)


def safe_connect_ex(self, address):
    """Connect_ex a socket, raising PermissionError for external hosts if sandboxed."""
    _check_connect(address)
    return _real_connect_ex(self, address)


def safe_getaddrinfo(*args, **kwargs):
    """Resolve addresses, raising socket.gaierror for external lookups if sandboxed."""
    if _blocks_lookup(_arg(args, kwargs, "host")):
        raise _name_not_known()
    return _real_getaddrinfo(*args, **kwargs)


def safe_gethostbyname(*args, **kwargs):
    """Resolve a host by name, raising socket.gaierror for external lookups if sandboxed."""
    if _blocks_lookup(_arg(args, kwargs, "hostname")):
        raise _name_not_known()
    return _real_gethostbyname(*args, **kwargs)


def safe_gethostbyname_ex(*args, **kwargs):
    """Resolve a host by name (extended), raising socket.gaierror if sandboxed and external."""
    if _blocks_lookup(_arg(args, kwargs, "hostname")):
        raise _name_not_known()
    return _real_gethostbyname_ex(*args, **kwargs)


def safe_gethostbyaddr(*args, **kwargs):
    """Resolve a host by address, raising socket.herror for external lookups if sandboxed."""
    if _blocks_lookup(_arg(args, kwargs, "ip_address")):
        raise socket.herror(1, "Unknown host")
    return _real_gethostbyaddr(*args, **kwargs)


def safe_getnameinfo(*args, **kwargs):
    """Resolve name info, raising socket.gaierror for external lookups if sandboxed."""
    sockaddr = _arg(args, kwargs, "sockaddr")
    host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
    if _blocks_lookup(host):
        raise _name_not_known()
    return _real_getnameinfo(*args, **kwargs)


def safe_getfqdn(*args, **kwargs):
    """Resolve a fully qualified name; external names are returned unresolved if sandboxed."""
    name = _arg(args, kwargs, "name")
    if _blocks_lookup(name):
        return str(name)
    return _real_getfqdn(*args, **kwargs)


def install() -> None:
    """Install the sandbox-aware socket wrappers. Idempotent and thread-safe."""
    global _installed
    with _install_lock:
        if _installed:
            return
        socket.socket.connect = safe_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = safe_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = safe_getaddrinfo  # ty: ignore[invalid-assignment]
        socket.gethostbyname = safe_gethostbyname  # ty: ignore[invalid-assignment]
        socket.gethostbyname_ex = safe_gethostbyname_ex  # ty: ignore[invalid-assignment]
        socket.gethostbyaddr = safe_gethostbyaddr  # ty: ignore[invalid-assignment]
        socket.getnameinfo = safe_getnameinfo  # ty: ignore[invalid-assignment]
        socket.getfqdn = safe_getfqdn  # ty: ignore[invalid-assignment]
        _installed = True


def uninstall() -> None:
    """Restore the original stdlib socket functions."""
    global _installed
    with _install_lock:
        socket.socket.connect = _real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = _real_getaddrinfo
        socket.gethostbyname = _real_gethostbyname
        socket.gethostbyname_ex = _real_gethostbyname_ex
        socket.gethostbyaddr = _real_gethostbyaddr
        socket.getnameinfo = _real_getnameinfo
        socket.getfqdn = _real_getfqdn
        _installed = False


def is_installed() -> bool:
    """Return True if the socket wrappers are currently installed."""
    return _installed


@contextmanager
def block_external_network(reason="worker execution"):
    """Block outgoing non-local network traffic for the current context."""
    install()
    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    old_reason = getattr(_thread_local, "reason", "worker execution")
    _thread_local.sandboxed = True
    _thread_local.reason = reason
    try:
        yield
    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = old_reason


@contextmanager
def unblock_external_network(reason="external request"):
    """Temporarily permit outgoing external network traffic for the current context."""
    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    old_reason = getattr(_thread_local, "reason", "worker execution")
    _thread_local.sandboxed = False
    _thread_local.reason = reason
    try:
        yield
    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = old_reason
