"""
HTTP backend selection and redirect-safe fetching for media downloads.

Every hop of a redirect chain is validated and pinned to the public addresses that
passed validation, so a redirect or a DNS change cannot reach an internal host.
"""

import socket
import threading
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests
    from curl_cffi import CurlOpt
    from curl_cffi.requests import RequestsError, Timeout, ConnectionError
    HTTPAdapter = None
    CURL_CFFI_AVAILABLE = True
except ImportError:
    # Fallback to standard requests if curl_cffi is not available
    import requests
    from requests.adapters import HTTPAdapter
    from requests.exceptions import Timeout, ConnectionError, RequestException as RequestsError
    CurlOpt = None
    CURL_CFFI_AVAILABLE = False

from utils.media_services.url_security import get_url_validator

__all__ = [
    'CURL_CFFI_AVAILABLE', 'ConnectionError', 'HTTPAdapter', 'RequestsError', 'Timeout',
    'RedirectFaultError', 'UnsafeRedirectError', 'get_with_safe_redirects', 'requests',
]

CROSS_ORIGIN_REDIRECT_HEADER_ALLOWLIST = frozenset({
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "range",
    "user-agent",
})

_dns_pin_lock = threading.RLock()


class UnsafeRedirectError(ValueError):
    """Raised when a redirect target fails URL security validation."""


class RedirectFaultError(ValueError):
    """Raised when a server's redirects cannot be followed; a later run may fare better."""


def get_with_safe_redirects(
    session: Any,
    url: str,
    request_kwargs: dict[str, Any],
    max_redirects: int,
    method: str = "get",
) -> tuple[Any, str]:
    """Fetch a URL while validating every redirect target before requesting it."""
    current_url = url
    validator = get_url_validator()

    for redirect_count in range(max_redirects + 1):
        if not validator.is_safe_for_download(current_url):
            if validator.validate_url(current_url).dns_failed:
                raise ConnectionError(f"Could not resolve host: {current_url}")
            raise UnsafeRedirectError(f"URL failed security validation: {current_url}")

        resolved_addresses = validator.resolve_public_addresses(current_url)
        if not resolved_addresses:
            # The check above just passed, so DNS changed between two lookups. No request
            # is sent, and a later attempt validates again.
            raise ConnectionError(f"URL could not be pinned to a public address: {current_url}")

        request_kwargs, dns_pin = pin_resolved_addresses(
            current_url,
            request_kwargs,
            resolved_addresses,
        )

        with dns_pin:
            response = getattr(session, method)(
                current_url,
                **{**request_kwargs, "allow_redirects": False},
            )
        status_code = getattr(response, "status_code", None)
        location = response.headers.get("location")
        if status_code not in {301, 302, 303, 307, 308}:
            return response, current_url

        if not location:
            response.close()
            raise RedirectFaultError("Redirect response is missing a location header")
        if redirect_count == max_redirects:
            response.close()
            raise RedirectFaultError("Redirect limit exceeded")

        response.close()
        next_url = urljoin(current_url, location)
        if _get_origin(current_url) != _get_origin(next_url):
            headers = request_kwargs.get("headers")
            if headers:
                request_kwargs = {
                    **request_kwargs,
                    "headers": {
                        name: value
                        for name, value in headers.items()
                        if name.lower() in CROSS_ORIGIN_REDIRECT_HEADER_ALLOWLIST
                    },
                }
        current_url = next_url

    raise RedirectFaultError("Redirect limit exceeded")


def _get_origin(url: str) -> tuple[str, str, int | None]:
    """Return the canonical origin tuple for redirect credential checks."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    return scheme, hostname, parsed.port or default_port


def pin_resolved_addresses(
    url: str,
    request_kwargs: dict[str, Any],
    addresses: tuple[str, ...],
) -> tuple[dict[str, Any], AbstractContextManager[None]]:
    """Pin a request to the addresses that passed URL security validation."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeRedirectError(f"URL has no hostname: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if CURL_CFFI_AVAILABLE and CurlOpt is not None:
        curl_options = dict(request_kwargs.get("curl_options", {}))
        curl_options[CurlOpt.RESOLVE] = [
            f"{hostname}:{port}:{_format_resolve_address(address)}"
            for address in addresses
        ]
        return {**request_kwargs, "curl_options": curl_options}, nullcontext()

    return request_kwargs, pin_standard_requests(hostname, addresses)


def _format_resolve_address(address: str) -> str:
    """Format IPv6 addresses for libcurl's host:port:address resolve syntax."""
    return f"[{address}]" if ":" in address else address


@contextmanager
def pin_standard_requests(hostname: str, addresses: tuple[str, ...]) -> Iterator[None]:
    """Temporarily pin standard-requests DNS while retaining the URL hostname for TLS."""
    original_getaddrinfo = socket.getaddrinfo
    normalized_hostname = hostname.rstrip(".").lower()

    # Same parameters as socket.getaddrinfo, which this stands in for.
    def pinned_getaddrinfo(host: bytes | str | None, port: bytes | str | int | None,
                           family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> list[tuple]:
        if str(host).rstrip(".").lower() != normalized_hostname:
            return original_getaddrinfo(host, port, family, type, proto, flags)

        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                type or socket.SOCK_STREAM,
                proto or socket.IPPROTO_TCP,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    with _dns_pin_lock:
        socket.getaddrinfo = pinned_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo
