"""
URL security and validation module for Reddit Stash.

Rejects URLs that are malformed, use a disallowed scheme or point at a blocked
or private host, before anything is downloaded from them.
"""

import ipaddress
import logging
import socket
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass, field
from utils.constants import DNS_CACHE_SECONDS, MAX_URL_LENGTH, MIN_URL_LENGTH, URL_VALIDATION_TIMEOUT


logger = logging.getLogger(__name__)
_dns_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="url-dns")


def _resolve_hostname(hostname: str):
    """Resolve a hostname within the URL validation timeout."""
    future = _dns_executor.submit(
        socket.getaddrinfo,
        hostname,
        None,
        type=socket.SOCK_STREAM,
    )
    try:
        return future.result(timeout=URL_VALIDATION_TIMEOUT)
    except FuturesTimeoutError as error:
        future.cancel()
        raise OSError(f"DNS resolution timed out for '{hostname}'") from error


_UNRESOLVED_RISK = "unresolved"  # High risk to callers, reported apart from a real rejection
_RISK_RANK = {"low": 0, "medium": 1, _UNRESOLVED_RISK: 2, "high": 3}


@dataclass
class ValidationResult:
    """Result of URL validation with details about any security issues."""
    is_valid: bool
    cleaned_url: str | None = None
    issues: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low, medium, high. Only low is valid; the others say how bad a rejection is
    dns_failed: bool = False  # The host did not resolve, which says nothing about its safety


class URLSecurityValidator:
    """
    Checks whether a URL is safe to download from.

    Features:
    - Scheme validation (http/https only)
    - Domain blocklisting for known malicious domains
    - URL length limits and sanitization
    - Pattern detection for suspicious URLs
    - Input validation and normalization
    """

    # Rejected by name, before any lookup. IP literals are covered by the address checks.
    BLOCKED_DOMAINS = {'localhost', 'malware.com'}

    # Suspicious URL patterns
    SUSPICIOUS_PATTERNS = [
        re.compile(r'[<>"\']'),              # HTML/script injection
        re.compile(r'javascript:', re.IGNORECASE),  # JavaScript schemes
        re.compile(r'data:', re.IGNORECASE), # Data URLs
        re.compile(r'file:', re.IGNORECASE), # File URLs
        re.compile(r'\.\./', re.IGNORECASE), # Directory traversal
        re.compile(r'%2e%2e%2f', re.IGNORECASE),  # URL-encoded traversal
    ]

    def __init__(self) -> None:
        self._dns_answers: dict[str, tuple[float, list]] = {}
        self._dns_host_locks: dict[str, threading.Lock] = {}
        self._dns_locks_guard = threading.Lock()

    def _lookup(self, hostname: str) -> list:
        """Resolve a host, sharing one lookup between callers.

        A gallery checks each image several times from parallel workers, which is enough
        lookups to trip a resolver's rate limit. Every caller still checks the addresses,
        and downloads are pinned to them. Failed lookups are not remembered.
        """
        with self._dns_locks_guard:
            host_lock = self._dns_host_locks.setdefault(hostname, threading.Lock())
        with host_lock:
            answer = self._dns_answers.get(hostname)
            if answer and answer[0] > time.monotonic():
                return answer[1]
            results = _resolve_hostname(hostname)
            self._dns_answers[hostname] = (time.monotonic() + DNS_CACHE_SECONDS, results)
            return results

    def validate_url(self, url: str) -> ValidationResult:
        """
        Validate a URL.

        Args:
            url: URL to validate

        Returns:
            ValidationResult with validation status and details
        """
        if not url or not isinstance(url, str):
            return ValidationResult(
                is_valid=False,
                issues=["URL is empty or not a string"],
                risk_level="high"
            )

        # Initial cleanup
        url = url.strip()

        if len(url) > MAX_URL_LENGTH:
            return ValidationResult(
                is_valid=False,
                issues=[f"URL too long ({len(url)} > {MAX_URL_LENGTH})"],
                risk_level="medium"
            )

        if len(url) < MIN_URL_LENGTH:
            return ValidationResult(
                is_valid=False,
                issues=[f"URL too short ({len(url)} < {MIN_URL_LENGTH})"],
                risk_level="low"
            )

        issues = []
        risk_level = "low"

        try:
            parsed = urlparse(url)
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                issues=[f"Failed to parse URL: {str(e)}"],
                risk_level="high"
            )

        if parsed.scheme.lower() not in ['http', 'https']:
            issues.append(f"Invalid scheme '{parsed.scheme}' - only http/https allowed")
            risk_level = "high"

        if parsed.username is not None or parsed.password is not None:
            issues.append("Credentials in URLs are not allowed")
            risk_level = "high"

        domain_issues, domain_risk = self._validate_domain(parsed.hostname)
        pattern_issues, pattern_risk = self._scan_suspicious_patterns(url)
        issues.extend(domain_issues + pattern_issues)

        # A failed lookup says nothing about safety, so it must not excuse a real rejection.
        worst_risk = max(risk_level, domain_risk, pattern_risk, key=_RISK_RANK.__getitem__)
        dns_failed = worst_risk == _UNRESOLVED_RISK

        is_valid = not issues
        cleaned_url = self._clean_url(parsed) if is_valid else None

        return ValidationResult(
            is_valid=is_valid,
            cleaned_url=cleaned_url,
            issues=issues,
            risk_level="high" if dns_failed else worst_risk,
            dns_failed=dns_failed
        )

    def _validate_domain(self, hostname: str | None) -> tuple[list, str]:
        """
        Validate a hostname and every address it resolves to.

        Returns:
            Tuple of (issues_list, risk_level)
        """
        issues = []
        risk_level = "low"

        if not hostname:
            issues.append("Missing domain/host")
            return issues, "high"

        hostname = hostname.lower()

        if hostname in self.BLOCKED_DOMAINS:
            issues.append(f"Domain '{hostname}' is blocked")
            return issues, "high"

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None

        if address is not None:
            if not address.is_global:
                issues.append(
                    f"Private/internal IP address not publicly routable: {hostname}"
                )
                return issues, "high"
            return issues, risk_level

        # Basic domain format validation
        if not self._is_valid_domain_format(hostname):
            issues.append(f"Invalid domain format: {hostname}")
            return issues, "medium"

        try:
            addresses = {result[4][0] for result in self._lookup(hostname)}
        except OSError as error:
            issues.append(f"Could not resolve domain '{hostname}': {error}")
            return issues, _UNRESOLVED_RISK

        if not addresses:
            issues.append(f"Could not resolve domain '{hostname}'")
            return issues, _UNRESOLVED_RISK

        for resolved_address in addresses:
            if not ipaddress.ip_address(resolved_address).is_global:
                issues.append(
                    "Resolved address is not publicly routable: "
                    f"{resolved_address}"
                )
                return issues, "high"

        return issues, risk_level

    def _is_valid_domain_format(self, domain: str) -> bool:
        """Check the hostname shape only; nothing is looked up."""
        domain_pattern = re.compile(
            r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$'
        )
        return bool(domain_pattern.match(domain)) and len(domain) <= 253

    def resolve_public_addresses(self, url: str) -> tuple[str, ...]:
        """Resolve a previously validated URL to only globally routable addresses."""
        try:
            hostname = urlparse(url).hostname
        except ValueError:
            return ()
        if not hostname:
            return ()

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if not self._is_valid_domain_format(hostname):
                return ()
        else:
            return (str(address),) if address.is_global else ()

        try:
            addresses = {result[4][0] for result in self._lookup(hostname)}
        except OSError:
            return ()

        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            return ()
        return tuple(sorted(addresses))

    def _scan_suspicious_patterns(self, url: str) -> tuple[list, str]:
        """
        Check URL for suspicious patterns.

        Returns:
            Tuple of (issues_list, risk_level)
        """
        issues = []
        risk_level = "low"

        for pattern in self.SUSPICIOUS_PATTERNS:
            if pattern.search(url):
                issues.append(f"Suspicious pattern detected: {pattern.pattern}")
                risk_level = "high"

        return issues, risk_level

    def _clean_url(self, parsed_url) -> str:
        """Lowercase the scheme and host and drop the fragment, which servers never receive."""
        return urlunparse((
            parsed_url.scheme.lower(),
            parsed_url.netloc.lower(),
            parsed_url.path,
            parsed_url.params,
            parsed_url.query,
            '',
        ))

    def is_safe_for_download(self, url: str) -> bool:
        """Whether validate_url accepts the URL."""
        return self.validate_url(url).is_valid


_global_validator = URLSecurityValidator()


def get_url_validator() -> URLSecurityValidator:
    """Get the shared URL validator, which holds no state of its own."""
    return _global_validator
