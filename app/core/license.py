"""License verification and cryptographic key management module.

Provides offline asymmetric cryptography validation for Pro tier license keys.
"""

import base64
import datetime
import json
import logging
import uuid
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

logger = logging.getLogger(__name__)

# Default embedded Ed25519 key pair for Sortify license signing/verification
DEFAULT_PUBLIC_KEY_HEX = "66741450655cf9c1b80562845f1c3a4ee0fde3ba7db71da11296c8a3652f8f1b"
DEFAULT_PRIVATE_KEY_HEX = "4a7adb98950a5f1d138c9e09983fbf35e164223d52c9216262fa11069b8fdce4"


def mask_license_key(key: str) -> str:
    """Return a masked display representation of a license key string.

    Format: XXXX-XXXX-****-1234 or SORTIFY-PRO-****-5678
    """
    if not key or not isinstance(key, str):
        return ""

    clean_key = key.strip()
    if len(clean_key) <= 8:
        return "****"

    if "-" in clean_key:
        parts = clean_key.split("-")
        if len(parts) >= 3:
            prefix = "-".join(parts[:2])
            last_part = parts[-1]
            suffix = last_part[-4:] if len(last_part) >= 4 else last_part
            return f"{prefix}-****-****-{suffix}"

    # Fallback masking
    prefix = clean_key[:4]
    suffix = clean_key[-4:]
    return f"{prefix}-****-****-{suffix}"


def generate_license_key(
    owner: str,
    tier: str = "Pro",
    expires: Optional[str] = None,
    private_key_hex: Optional[str] = None,
) -> str:
    """Generate a cryptographically signed license key string for a user/organization.

    Args:
        owner: Registered license owner name or email.
        tier: Subscription tier ("Pro" or "Community").
        expires: ISO date string for expiration (e.g. "2099-12-31"). Defaults to 2099-12-31.
        private_key_hex: Optional custom private key hex string. Defaults to embedded key.

    Returns
    -------
        Formatted license key string.
    """
    if not expires:
        expires = "2099-12-31"

    payload = {
        "owner": owner,
        "tier": tier,
        "expires": expires,
        "issued": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "nonce": uuid.uuid4().hex[:8],
    }

    payload_json = json.dumps(payload, sort_keys=True)
    payload_bytes = payload_json.encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    priv_hex = private_key_hex or DEFAULT_PRIVATE_KEY_HEX
    priv_key_bytes = bytes.fromhex(priv_hex)
    priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(priv_key_bytes)

    sig_bytes = priv_key.sign(payload_bytes)
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode("utf-8").rstrip("=")

    return f"SORTIFY-{tier.upper()}-{payload_b64}.{sig_b64}"


def validate_license_key(
    key_str: str, public_key_hex: Optional[str] = None
) -> Dict[str, Any]:
    """Validate a license key string offline using asymmetric cryptography.

    Args:
        key_str: License key string to validate.
        public_key_hex: Optional custom public key hex string. Defaults to embedded key.

    Returns
    -------
        Dict containing valid (bool), tier (str), owner (str), expires (str), message (str), and masked_key (str).
    """
    if not key_str or not isinstance(key_str, str) or not key_str.strip():
        return {
            "valid": False,
            "tier": "Community",
            "owner": "",
            "expires": "",
            "message": "License key cannot be empty.",
            "masked_key": "",
        }

    clean_key = key_str.strip()
    masked = mask_license_key(clean_key)

    if "." not in clean_key or not clean_key.startswith("SORTIFY-"):
        return {
            "valid": False,
            "tier": "Community",
            "owner": "",
            "expires": "",
            "message": "Invalid license key format.",
            "masked_key": masked,
        }

    try:
        # Split prefix and signature
        parts = clean_key.split(".")
        if len(parts) != 2:
            return {
                "valid": False,
                "tier": "Community",
                "owner": "",
                "expires": "",
                "message": "Malformed license key structure.",
                "masked_key": masked,
            }

        prefix_and_payload, sig_b64 = parts[0], parts[1]
        prefix_parts = prefix_and_payload.split("-", 2)
        if len(prefix_parts) < 3:
            return {
                "valid": False,
                "tier": "Community",
                "owner": "",
                "expires": "",
                "message": "Invalid license key prefix.",
                "masked_key": masked,
            }

        payload_b64 = prefix_parts[2]

        # Restore base64 padding if needed
        payload_b64_padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        sig_b64_padded = sig_b64 + "=" * (-len(sig_b64) % 4)

        payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
        sig_bytes = base64.urlsafe_b64decode(sig_b64_padded)

        # Cryptographic verification
        pub_hex = public_key_hex or DEFAULT_PUBLIC_KEY_HEX
        pub_key_bytes = bytes.fromhex(pub_hex)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)

        pub_key.verify(sig_bytes, payload_bytes)

        # Parse payload JSON
        payload = json.loads(payload_bytes.decode("utf-8"))
        owner = payload.get("owner", "Unknown")
        tier = payload.get("tier", "Pro")
        expires = payload.get("expires", "")

        # Check expiration date
        if expires and expires.lower() != "never":
            try:
                exp_date = datetime.datetime.strptime(expires, "%Y-%m-%d").date()
                today = datetime.datetime.now(datetime.timezone.utc).date()
                if exp_date < today:
                    return {
                        "valid": False,
                        "tier": "Community",
                        "owner": owner,
                        "expires": expires,
                        "message": f"License key expired on {expires}.",
                        "masked_key": masked,
                    }
            except ValueError:
                pass  # Ignore unparseable non-standard expiration string format

        return {
            "valid": True,
            "tier": tier,
            "owner": owner,
            "expires": expires,
            "message": f"Successfully validated {tier} Tier license for {owner}.",
            "masked_key": masked,
        }

    except InvalidSignature:
        return {
            "valid": False,
            "tier": "Community",
            "owner": "",
            "expires": "",
            "message": "License key signature verification failed.",
            "masked_key": masked,
        }
    except Exception as e:
        logger.warning(f"Error during license verification: {e}")
        return {
            "valid": False,
            "tier": "Community",
            "owner": "",
            "expires": "",
            "message": f"Invalid license key payload: {e}",
            "masked_key": masked,
        }


__all__ = [
    "mask_license_key",
    "generate_license_key",
    "validate_license_key",
]
