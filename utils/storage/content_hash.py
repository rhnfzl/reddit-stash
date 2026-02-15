"""
Standardised content hashing for cross-provider file comparison.

Uses BLAKE3 (fast, cryptographic) with SHA256 fallback.
1 MB read chunks keep memory usage constant regardless of file size.
"""

import hashlib
import hmac

CHUNK_SIZE = 1024 * 1024  # 1 MB

try:
    import blake3 as _blake3
    _HAS_BLAKE3 = True
except ImportError:
    _HAS_BLAKE3 = False


def compute_file_hash(file_path: str) -> str:
    """Compute a hex-digest hash of a local file (BLAKE3 or SHA256)."""
    if _HAS_BLAKE3:
        hasher = _blake3.blake3()
    else:
        hasher = hashlib.sha256()

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()


def compute_bytes_hash(data: bytes) -> str:
    """Compute a hex-digest hash of raw bytes (BLAKE3 or SHA256)."""
    if _HAS_BLAKE3:
        return _blake3.blake3(data).hexdigest()
    return hashlib.sha256(data).hexdigest()


def hashes_match(hash_a: str, hash_b: str) -> bool:
    """Constant-time comparison of two hex-digest hashes."""
    if not hash_a or not hash_b:
        return False
    return hmac.compare_digest(hash_a, hash_b)
