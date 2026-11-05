"""
Standardised content hashing for cross-provider file comparison.

Uses BLAKE3 (fast, cryptographic). It is a hard requirement: a silent switch to
another digest would make every file look changed against stored hashes.
1 MB read chunks keep memory usage constant regardless of file size.
"""

import hmac

import blake3

CHUNK_SIZE = 1024 * 1024  # 1 MB


def compute_file_hash(file_path: str) -> str:
    """Compute a hex-digest hash of a local file (BLAKE3)."""
    hasher = blake3.blake3()

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)

    return hasher.hexdigest()


def compute_bytes_hash(data: bytes) -> str:
    """Compute a hex-digest hash of raw bytes (BLAKE3)."""
    return blake3.blake3(data).hexdigest()


def hashes_match(hash_a: str, hash_b: str) -> bool:
    """Constant-time comparison of two hex-digest hashes."""
    if not hash_a or not hash_b:
        return False
    return hmac.compare_digest(hash_a, hash_b)
