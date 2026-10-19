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
from typing import Optional, Tuple

from app.core.exceptions import ModelVerificationError

logger = logging.getLogger(__name__)

DEFAULT_ADVISORY_ENDPOINT = "https://security.huggingface.co/api/advisories"
DEFAULT_CACHE_TTL = 3600  # 1 hour in seconds
MAX_API_TIMEOUT = 5.0  # Max 5 seconds timeout constraint

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
            Tuple[Optional[str], Optional[str]], Tuple[SecurityAdvisoryResult, float]
        ] = {}
        self._cache_lock = threading.RLock()
        self._settings = settings
        self._endpoint = endpoint
        self._ttl = ttl
        self._fallback_policy = fallback_policy

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
            pol = getattr(
                self._settings, "SECURITY_ADVISORY_FALLBACK_POLICY", "permissive"
            )
            return str(pol).strip().lower()
        return "permissive"

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
        if s.startswith("http://") or s.startswith("https://"):
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

        # 2. Remote Security Advisory API Query
        # Bypassing thread-local socket sandboxing safely during advisory lookup
        from app.core.shared_registry import _thread_local

        was_sandboxed = getattr(_thread_local, "sandboxed", False)
        old_reason = getattr(_thread_local, "reason", "worker execution")
        _thread_local.sandboxed = False
        _thread_local.reason = "security advisory query execution"

        try:
            params = {}
            if norm_repo:
                params["repo_id"] = norm_repo
            if norm_hash:
                params["file_hash"] = norm_hash

            query_str = urllib.parse.urlencode(params)
            endpoint_url = (
                f"{self.endpoint}?{query_str}" if query_str else self.endpoint
            )

            req = urllib.request.Request(
                endpoint_url,
                headers={
                    "User-Agent": "Smart-AutoSorter-SecurityClient/1.0",
                    "Accept": "application/json",
                },
            )

            try:
                from app.core.downloader import DownloadManager

                opener = DownloadManager.get_instance().get_opener()
            except Exception:
                logger.warning(
                    "DownloadManager opener unavailable; using default urllib opener",
                    exc_info=True,
                )
                opener = urllib.request.build_opener()

            with opener.open(req, timeout=eff_timeout) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))

            has_adv = False
            adv_id = None
            sev = None
            det = None

            if isinstance(resp_data, dict):
                has_adv = bool(
                    resp_data.get("has_advisory")
                    or resp_data.get("vulnerable")
                    or resp_data.get("advisories")
                )
                adv_id = resp_data.get("advisory_id") or resp_data.get("id")
                sev = resp_data.get("severity") or resp_data.get("rating")
                det = resp_data.get("details") or resp_data.get("message")

                if (
                    not adv_id
                    and isinstance(resp_data.get("advisories"), list)
                    and resp_data["advisories"]
                ):
                    first_adv = resp_data["advisories"][0]
                    if isinstance(first_adv, dict):
                        adv_id = first_adv.get("id")
                        sev = first_adv.get("severity")
                        det = first_adv.get("details") or first_adv.get("summary")

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
                f"Security advisory query failed for '{norm_repo or norm_hash}': {e}"
            )
            if eff_fallback == "strict":
                raise ModelVerificationError(
                    f"Pre-download security advisory lookup failed for '{norm_repo or norm_hash}' "
                    f"under strict security policy: {e}"
                ) from e
            else:
                fallback_result = SecurityAdvisoryResult(
                    has_advisory=False,
                    details=f"Query failed ({e}), proceeding under permissive fallback policy.",
                    timestamp=time.time(),
                )
                self.cache_advisory(norm_repo, norm_hash, fallback_result)
                return fallback_result

        finally:
            _thread_local.sandboxed = was_sandboxed
            _thread_local.reason = old_reason
