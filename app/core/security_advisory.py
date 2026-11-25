"""Pre-download security advisory client with thread-safe in-memory caching."""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from app.core.exceptions import ModelVerificationError
from app.core.network_guard import (
    _DEFAULT_TRUSTED_HOSTS,
    _build_proxy_opener,
    _is_allowed_outbound_host,
    _resolve_trusted_hosts,
    _validate_outbound_https_url,
)

logger = logging.getLogger(__name__)

DEFAULT_ADVISORY_ENDPOINT = "https://huggingface.co/api/models"
DEFAULT_CACHE_TTL = 3600  # 1 hour in seconds
MAX_API_TIMEOUT = 5.0  # Max 5 seconds timeout constraint

ALLOWED_ADVISORY_HOSTS: frozenset[str] = _DEFAULT_TRUSTED_HOSTS
_is_allowed_advisory_host = _is_allowed_outbound_host


def _validate_advisory_url(url: str, trusted_hosts=None) -> None:
    """Validate that the security advisory URL uses HTTPS and points to an allowed host."""
    return _validate_outbound_https_url(
        url,
        trusted_hosts=trusted_hosts,
        default_hosts=ALLOWED_ADVISORY_HOSTS,
        error_cls=ModelVerificationError,
        endpoint_label="security advisory",
        trusted_label="advisory",
    )


def _build_advisory_query_url(
    base_endpoint: str,
    norm_repo: Optional[str],
    norm_hash: Optional[str],
) -> str:
    """Construct the HTTPS query URL for either Hugging Face models API or a custom advisory mirror."""
    parsed = urllib.parse.urlsplit(base_endpoint.strip())
    clean_path = (parsed.path or "").rstrip("/")

    if clean_path.endswith("/api/models"):
        if norm_repo:
            repo_clean = norm_repo.strip("/")
            segments = [seg for seg in repo_clean.split("/") if seg]
            if (
                not segments
                or any(seg in {".", ".."} for seg in segments)
                or any(
                    ch in repo_clean
                    for ch in ("\\", "?", "#", "@", "\r", "\n", "\t", " ")
                )
            ):
                raise ModelVerificationError(
                    f"Invalid repository identifier for security advisory check: {norm_repo!r}"
                )
            quoted_repo = "/".join(urllib.parse.quote(seg, safe="") for seg in segments)
            base_no_query = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, clean_path, "", "")
            )
            query_params = [("securityStatus", "true")]
            if parsed.query:
                query_params = urllib.parse.parse_qsl(parsed.query) + query_params
            return (
                f"{base_no_query}/{quoted_repo}?{urllib.parse.urlencode(query_params)}"
            )

        params = {"securityStatus": "true"}
        if norm_hash:
            params["sha256"] = norm_hash
        sep = "&" if parsed.query else "?"
        return f"{base_endpoint}{sep}{urllib.parse.urlencode(params)}"

    params = {}
    if norm_repo:
        params["repo_id"] = norm_repo
    if norm_hash:
        params["file_hash"] = norm_hash
    if not params:
        return base_endpoint
    sep = "&" if parsed.query else "?"
    return f"{base_endpoint}{sep}{urllib.parse.urlencode(params)}"


def _parse_advisory_payload(
    resp_data: object, asset_label: str
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Extract advisory flag, ID, severity, and details from HF metadata or custom mirror JSON."""
    if isinstance(resp_data, dict):
        if resp_data.get("disabled") is True:
            return (
                True,
                "HF-REPO-DISABLED",
                "HIGH",
                f"Repository '{asset_label}' is disabled on Hugging Face.",
            )

        sec_status = resp_data.get("securityRepoStatus")
        if isinstance(sec_status, dict):
            files_with_issues = sec_status.get("filesWithIssues")
            if isinstance(files_with_issues, list) and files_with_issues:
                first_issue = files_with_issues[0]
                if isinstance(first_issue, dict):
                    issue_path = first_issue.get("path") or "unknown"
                    issue_level = str(
                        first_issue.get("level")
                        or first_issue.get("severity")
                        or "HIGH"
                    )
                    adv_id = str(
                        first_issue.get("id")
                        or first_issue.get("advisory_id")
                        or f"HF-SCAN-{issue_level.upper()}"
                    )
                    det = str(
                        first_issue.get("message")
                        or first_issue.get("details")
                        or f"Security scan flagged '{issue_path}' ({issue_level})."
                    )
                    return (True, adv_id, issue_level, det)
                return (
                    True,
                    "HF-SCAN-ISSUE",
                    "HIGH",
                    f"Repository security scan flagged issues: {first_issue}",
                )

            repo_scan_state = str(sec_status.get("status") or "").strip().lower()
            if (
                repo_scan_state
                in {"infected", "unsafe", "flagged", "malware", "quarantined"}
                or sec_status.get("infected") is True
                or sec_status.get("hasIssues") is True
            ):
                return (
                    True,
                    f"HF-REPO-{repo_scan_state.upper() or 'FLAGGED'}",
                    "CRITICAL",
                    str(
                        sec_status.get("message")
                        or sec_status.get("details")
                        or f"Repository '{asset_label}' flagged by Hugging Face security scan."
                    ),
                )

        advisories_list = resp_data.get("advisories") or resp_data.get(
            "securityAdvisories"
        )
        has_adv = bool(
            resp_data.get("has_advisory")
            or resp_data.get("vulnerable")
            or advisories_list
        )
        adv_id = resp_data.get("advisory_id") or (
            resp_data.get("id") if has_adv else None
        )
        sev = resp_data.get("severity") or resp_data.get("rating")
        det = resp_data.get("details") or resp_data.get("message")

        if not adv_id and isinstance(advisories_list, list) and advisories_list:
            first_adv = advisories_list[0]
            if isinstance(first_adv, dict):
                adv_id = first_adv.get("id") or first_adv.get("advisory_id")
                sev = first_adv.get("severity") or first_adv.get("rating")
                det = (
                    first_adv.get("details")
                    or first_adv.get("summary")
                    or first_adv.get("message")
                )
        return (has_adv, adv_id, sev, det)

    if isinstance(resp_data, list) and resp_data:
        if all(
            isinstance(item, dict)
            and (
                "securityRepoStatus" in item
                or (
                    "modelId" in item
                    and "advisory_id" not in item
                    and "severity" not in item
                )
            )
            for item in resp_data
        ):
            for item in resp_data:
                parsed_item = _parse_advisory_payload(item, asset_label)
                if parsed_item[0]:
                    return parsed_item
            return (False, None, None, None)

        first_adv = resp_data[0]
        if isinstance(first_adv, dict):
            return (
                True,
                first_adv.get("id") or first_adv.get("advisory_id"),
                first_adv.get("severity") or first_adv.get("rating"),
                first_adv.get("details")
                or first_adv.get("summary")
                or first_adv.get("message"),
            )
        if isinstance(first_adv, str):
            return (True, first_adv, None, "Security advisory listed.")
        return (True, None, None, "Security advisory listed.")

    return (False, None, None, None)


_HF_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?(?:huggingface\.co|hf\.co)/([^/]+/[^/]+)(?:/.*)?$",
    re.IGNORECASE,
)


@dataclass
class SecurityAdvisoryResult:
    """Dataclass encapsulating security advisory verification results."""

    has_advisory: bool
    advisory_id: Optional[str] = None
    severity: Optional[str] = None
    details: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class SecurityAdvisoryClient:
    """Thread-safe client to query remote security advisory endpoints with in-memory caching."""

    _instance: Optional["SecurityAdvisoryClient"] = None
    _lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> "SecurityAdvisoryClient":
        """Retrieve the singleton instance of SecurityAdvisoryClient."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance of SecurityAdvisoryClient."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.clear_cache()
                cls._instance = None

    def __init__(
        self,
        settings=None,
        endpoint: Optional[str] = None,
        ttl: Optional[float] = None,
        fallback_policy: Optional[str] = None,
    ):
        """Initialize SecurityAdvisoryClient with configurable endpoint, TTL, and fallback policy."""
        self._cache: dict[
            tuple[Optional[str], Optional[str]], tuple[SecurityAdvisoryResult, float]
        ] = {}
        self._cache_lock = threading.RLock()
        self._settings = settings
        self._endpoint = endpoint
        self._ttl = ttl
        self._fallback_policy = fallback_policy
        trusted = self._get_trusted_hosts()
        if self._endpoint is not None:
            _validate_advisory_url(self._endpoint, trusted_hosts=trusted)
        elif self._settings is not None and hasattr(
            self._settings, "SECURITY_ADVISORY_ENDPOINT"
        ):
            _validate_advisory_url(
                self._settings.SECURITY_ADVISORY_ENDPOINT, trusted_hosts=trusted
            )

    def _get_trusted_hosts(self) -> frozenset[str]:
        """Return normalized trusted hostnames including any configured TRUSTED_MODEL_HOSTS."""
        return _resolve_trusted_hosts(
            self._settings, default_hosts=ALLOWED_ADVISORY_HOSTS
        )

    @property
    def endpoint(self) -> str:
        """Get effective security advisory API endpoint."""
        if self._endpoint:
            return self._endpoint
        if self._settings is None:
            try:
                from app.config import AppSettings

                self._settings = AppSettings()
            except Exception:
                logger.warning(
                    "Could not load AppSettings; using advisory defaults", exc_info=True
                )
        if self._settings:
            return getattr(
                self._settings, "SECURITY_ADVISORY_ENDPOINT", DEFAULT_ADVISORY_ENDPOINT
            )
        return DEFAULT_ADVISORY_ENDPOINT

    @property
    def ttl(self) -> float:
        """Get effective cache TTL in seconds."""
        if self._ttl is not None:
            return float(self._ttl)
        if self._settings is None:
            try:
                from app.config import AppSettings

                self._settings = AppSettings()
            except Exception:
                logger.warning(
                    "Could not load AppSettings; using advisory defaults", exc_info=True
                )
        if self._settings:
            return float(
                getattr(
                    self._settings, "SECURITY_ADVISORY_CACHE_TTL", DEFAULT_CACHE_TTL
                )
            )
        return float(DEFAULT_CACHE_TTL)

    @property
    def fallback_policy(self) -> str:
        """Get effective fallback policy ('strict' or 'permissive')."""
        if self._fallback_policy:
            return self._fallback_policy.strip().lower()
        if self._settings is None:
            try:
                from app.config import AppSettings

                self._settings = AppSettings()
            except Exception:
                logger.warning(
                    "Could not load AppSettings; using advisory defaults", exc_info=True
                )
        if self._settings:
            pol = getattr(self._settings, "SECURITY_ADVISORY_FALLBACK_POLICY", "strict")
            return str(pol).strip().lower()
        return "strict"

    @classmethod
    def extract_repo_id(cls, url_or_id: str) -> str:
        """Extract standardized Hugging Face repository identifier from URL or model ID string."""
        if not url_or_id:
            return ""
        s = url_or_id.strip()
        match = _HF_URL_PATTERN.match(s)
        if match:
            return match.group(1)
        # If it's already a repo string or model name (e.g., 'sentence-transformers/all-MiniLM-L6-v2')
        if s.startswith(("http://", "https://")):
            # Parse path fallback
            parsed = urllib.parse.urlparse(s)
            path_parts = [p for p in parsed.path.split("/") if p]
            if len(path_parts) >= 2:
                return f"{path_parts[0]}/{path_parts[1]}"
            elif len(path_parts) == 1:
                return path_parts[0]
        return s

    def cache_advisory(
        self,
        repo_id: Optional[str],
        file_hash: Optional[str],
        result: SecurityAdvisoryResult,
    ) -> None:
        """Manually store or update a security advisory result in the in-memory cache."""
        norm_repo = self.extract_repo_id(repo_id) if repo_id else None
        norm_hash = file_hash.strip().lower() if file_hash else None
        key = (norm_repo, norm_hash)
        with self._cache_lock:
            self._cache[key] = (result, time.time())

    def clear_cache(self) -> None:
        """Clear all entries from the in-memory advisory cache."""
        with self._cache_lock:
            self._cache.clear()

    def get_cached_advisory(
        self, repo_id: Optional[str], file_hash: Optional[str]
    ) -> Optional[SecurityAdvisoryResult]:
        """Retrieve cached advisory result if present and within TTL."""
        norm_repo = self.extract_repo_id(repo_id) if repo_id else None
        norm_hash = file_hash.strip().lower() if file_hash else None
        key = (norm_repo, norm_hash)
        now = time.time()
        with self._cache_lock:
            # 1. Exact key match
            if key in self._cache:
                res, ts = self._cache[key]
                if now - ts <= self.ttl:
                    return res
                else:
                    del self._cache[key]

            # 2. Check repo-level cached entry (norm_repo, None)
            if norm_repo and (norm_repo, None) in self._cache:
                res, ts = self._cache[(norm_repo, None)]
                if now - ts <= self.ttl:
                    return res
                else:
                    del self._cache[(norm_repo, None)]

            # 3. Check hash-level cached entry (None, norm_hash)
            if norm_hash and (None, norm_hash) in self._cache:
                res, ts = self._cache[(None, norm_hash)]
                if now - ts <= self.ttl:
                    return res
                else:
                    del self._cache[(None, norm_hash)]

            # 4. Fallback search across all active entries
            for k, (res, ts) in list(self._cache.items()):
                if now - ts > self.ttl:
                    del self._cache[k]
                    continue
                if (norm_repo and k[0] == norm_repo) or (
                    norm_hash and k[1] == norm_hash
                ):
                    return res

        return None

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """Build an independent urllib opener configured with active proxy settings."""
        from app.core.downloader import NetworkError

        proxy_val = getattr(self._settings, "PROXY", None)
        if proxy_val is None:
            try:
                from app.config import AppSettings

                app_settings = AppSettings()
                if self._settings is None:
                    self._settings = app_settings
                proxy_val = getattr(app_settings, "PROXY", "")
            except Exception as err:  # noqa: BLE001 - optional settings initialization fallback
                logger.debug(
                    "Failed to load AppSettings for proxy configuration: %s", err
                )
                proxy_val = ""
        return _build_proxy_opener(proxy_val, error_cls=NetworkError)

    def check_advisory(
        self,
        repo_id: Optional[str] = None,
        file_hash: Optional[str] = None,
        timeout: float = MAX_API_TIMEOUT,
        fallback_policy: Optional[str] = None,
        raise_on_advisory: bool = True,
    ) -> SecurityAdvisoryResult:
        """Verify remote security advisories for repository identifier and/or file SHA-256 hash.

        Parameters
        ----------
        repo_id : str, optional
            Model repository identifier or remote URL.
        file_hash : str, optional
            SHA-256 hash of target model file.
        timeout : float
            Lookup API timeout limit (capped at 5.0s).
        fallback_policy : str, optional
            Override policy on network timeout/failure ('strict' or 'permissive').
        raise_on_advisory : bool
            If True, raises ModelVerificationError when an active advisory is detected.

        Returns
        -------
        SecurityAdvisoryResult
            The verification result object.

        Raises
        ------
        ModelVerificationError
            If an active advisory is detected or if lookup fails under strict policy.
        """
        if not repo_id and not file_hash:
            raise ValueError(
                "Either repo_id or file_hash must be provided to check_advisory."
            )

        norm_repo = self.extract_repo_id(repo_id) if repo_id else None
        norm_hash = file_hash.strip().lower() if file_hash else None
        eff_timeout = min(float(timeout), MAX_API_TIMEOUT)

        # 1. Fast In-Memory Cache Lookup (< 50ms)
        cached_result = self.get_cached_advisory(norm_repo, norm_hash)
        if cached_result is not None:
            if cached_result.has_advisory and raise_on_advisory:
                details_msg = (
                    f"Active security advisory '{cached_result.advisory_id or 'FLAGGED'}' "
                    f"detected for asset '{norm_repo or norm_hash}' (severity: {cached_result.severity or 'CRITICAL'}): "
                    f"{cached_result.details or 'Security alert issued.'}"
                )
                raise ModelVerificationError(details_msg)
            return cached_result

        # Resolved only on a cache miss: the default policy loads AppSettings
        # from disk, which a cached lookup has no use for.
        eff_fallback = (
            fallback_policy.strip().lower() if fallback_policy else self.fallback_policy
        )

        base_endpoint = self.endpoint
        trusted_hosts = self._get_trusted_hosts()
        _validate_advisory_url(base_endpoint, trusted_hosts=trusted_hosts)

        endpoint_url = _build_advisory_query_url(base_endpoint, norm_repo, norm_hash)
        _validate_advisory_url(endpoint_url, trusted_hosts=trusted_hosts)

        # 2. Remote Security Advisory API Query
        # Bypassing thread-local socket sandboxing safely during advisory lookup
        from app.core.network_guard import unblock_external_network

        with unblock_external_network(reason="security advisory query execution"):
            try:
                req = urllib.request.Request(
                    endpoint_url,
                    headers={
                        "User-Agent": "Smart-AutoSorter-SecurityClient/1.0",
                        "Accept": "application/json",
                    },
                )

                opener = self._build_opener()

                with opener.open(req, timeout=eff_timeout) as resp:
                    raw_data = resp.read()
                    if not raw_data:
                        raise ValueError(
                            "Empty response payload from security advisory endpoint."
                        )
                    try:
                        text_data = raw_data.decode("utf-8")
                    except UnicodeDecodeError as err:
                        raise ValueError(
                            f"Failed to decode security advisory response payload as UTF-8: {err}"
                        ) from err
                    try:
                        resp_data = json.loads(text_data)
                    except (json.JSONDecodeError, ValueError) as json_err:
                        raise ValueError(
                            f"Non-JSON or malformed payload received from security advisory endpoint: {json_err}"
                        ) from json_err

                has_adv, adv_id, sev, det = _parse_advisory_payload(
                    resp_data, str(norm_repo or norm_hash or "unknown")
                )

                result = SecurityAdvisoryResult(
                    has_advisory=has_adv,
                    advisory_id=adv_id,
                    severity=sev,
                    details=det,
                    timestamp=time.time(),
                )
                self.cache_advisory(norm_repo, norm_hash, result)

                if has_adv and raise_on_advisory:
                    details_msg = (
                        f"Active security advisory '{adv_id or 'FLAGGED'}' "
                        f"detected for asset '{norm_repo or norm_hash}' (severity: {sev or 'CRITICAL'}): "
                        f"{det or 'Security alert issued.'}"
                    )
                    raise ModelVerificationError(details_msg)

                return result

            except ModelVerificationError:
                raise
            except Exception as e:
                logger.warning(
                    "Security advisory query failed for '%s': %s",
                    norm_repo or norm_hash,
                    e,
                )
                if eff_fallback == "strict":
                    raise ModelVerificationError(
                        f"Pre-download security advisory lookup failed for '{norm_repo or norm_hash}' "
                        f"under strict security policy: {e}"
                    ) from e
                return SecurityAdvisoryResult(
                    has_advisory=False,
                    details=f"Query failed ({e}), proceeding under permissive fallback policy.",
                    timestamp=time.time(),
                )
