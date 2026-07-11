"""
URL security and validation module for Reddit Stash.

This module provides comprehensive URL validation and security checks to prevent
malicious URLs from being processed. Implements 2024 security best practices
including domain blocklisting, URL sanitization, and input validation.
"""

import ipaddress
import logging
import socket
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse, urlunparse
from typing import Set, Optional, Dict, Any
from dataclasses import dataclass
from .constants import MAX_URL_LENGTH, MIN_URL_LENGTH, URL_VALIDATION_TIMEOUT


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


@dataclass
class ValidationResult:
    """Result of URL validation with details about any security issues."""
    is_valid: bool
    cleaned_url: Optional[str] = None
    issues: list = None
    risk_level: str = "low"  # low, medium, high

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class URLSecurityValidator:
    """
    Comprehensive URL security validator for preventing malicious URLs.

    Features:
    - Scheme validation (http/https only)
    - Domain blocklisting for known malicious domains
    - URL length limits and sanitization
    - Pattern detection for suspicious URLs
    - Input validation and normalization
    """

    # Known malicious/suspicious domains (this would be expanded in production)
    BLOCKED_DOMAINS = {
        # Example blocked domains - in production this would be a larger list
        'malware.com',
        'phishing.example',
        'suspicious.test',
        'localhost',  # Block localhost for security
        '127.0.0.1',  # Block loopback
        '0.0.0.0',    # Block null route
    }

    # Suspicious URL patterns
    SUSPICIOUS_PATTERNS = [
        re.compile(r'[<>"\']'),              # HTML/script injection
        re.compile(r'javascript:', re.IGNORECASE),  # JavaScript schemes
        re.compile(r'data:', re.IGNORECASE), # Data URLs
        re.compile(r'file:', re.IGNORECASE), # File URLs
        re.compile(r'\.\./', re.IGNORECASE), # Directory traversal
        re.compile(r'%2e%2e%2f', re.IGNORECASE),  # URL-encoded traversal
    ]

    def __init__(self, additional_blocked_domains: Optional[Set[str]] = None):
        """
        Initialize the URL security validator.

        Args:
            additional_blocked_domains: Additional domains to block beyond defaults
        """
        self.blocked_domains = self.BLOCKED_DOMAINS.copy()
        if additional_blocked_domains:
            self.blocked_domains.update(domain.lower() for domain in additional_blocked_domains)

        logger.info(f"URL Security Validator initialized with {len(self.blocked_domains)} blocked domains")

    def validate_url(self, url: str) -> ValidationResult:
        """
        Perform comprehensive validation of a URL.

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

        # Check URL length
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

        # Parse URL
        try:
            parsed = urlparse(url)
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                issues=[f"Failed to parse URL: {str(e)}"],
                risk_level="high"
            )

        # Validate scheme
        if parsed.scheme.lower() not in ['http', 'https']:
            issues.append(f"Invalid scheme '{parsed.scheme}' - only http/https allowed")
            risk_level = "high"

        if parsed.username is not None or parsed.password is not None:
            issues.append("Credentials in URLs are not allowed")
            risk_level = "high"

        # Validate domain
        domain_issues, domain_risk = self._validate_domain(parsed.hostname)
        issues.extend(domain_issues)
        if domain_risk == "high":
            risk_level = "high"
        elif domain_risk == "medium" and risk_level == "low":
            risk_level = "medium"

        # Check for suspicious patterns
        pattern_issues, pattern_risk = self._check_suspicious_patterns(url)
        issues.extend(pattern_issues)
        if pattern_risk == "high":
            risk_level = "high"
        elif pattern_risk == "medium" and risk_level == "low":
            risk_level = "medium"

        # Clean and normalize URL if no high-risk issues
        cleaned_url = None
        if risk_level != "high":
            cleaned_url = self._clean_url(parsed)

        is_valid = len(issues) == 0 or risk_level == "low"

        return ValidationResult(
            is_valid=is_valid,
            cleaned_url=cleaned_url,
            issues=issues,
            risk_level=risk_level
        )

    def _validate_domain(self, hostname: Optional[str]) -> tuple[list, str]:
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

        # Check against blocked domains
        if hostname in self.blocked_domains:
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
            addresses = {result[4][0] for result in _resolve_hostname(hostname)}
        except OSError as error:
            issues.append(f"Could not resolve domain '{hostname}': {error}")
            return issues, "high"

        if not addresses:
            issues.append(f"Could not resolve domain '{hostname}'")
            return issues, "high"

        for resolved_address in addresses:
            if not ipaddress.ip_address(resolved_address).is_global:
                issues.append(
                    "Resolved address is not publicly routable: "
                    f"{resolved_address}"
                )
                return issues, "high"

        return issues, risk_level

    def _is_valid_domain_format(self, domain: str) -> bool:
        """
        Check if domain has a valid format.

        Args:
            domain: Domain to validate

        Returns:
            True if domain format is valid
        """
        # Basic domain validation - can be enhanced
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
            ipaddress.ip_address(hostname)
        except ValueError:
            if not self._is_valid_domain_format(hostname):
                return ()

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None

        if address is not None:
            return (str(address),) if address.is_global else ()

        try:
            addresses = {result[4][0] for result in _resolve_hostname(hostname)}
        except OSError:
            return ()

        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            return ()
        return tuple(sorted(addresses))

    def _check_suspicious_patterns(self, url: str) -> tuple[list, str]:
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
        """
        Clean and normalize a parsed URL.

        Args:
            parsed_url: Already parsed URL object

        Returns:
            Cleaned URL string
        """
        # Reconstruct URL with normalized components
        cleaned = urlunparse((
            parsed_url.scheme.lower(),
            parsed_url.netloc.lower(),
            parsed_url.path,
            parsed_url.params,
            parsed_url.query,
            ''  # Remove fragment for security
        ))

        return cleaned

    def is_safe_for_download(self, url: str) -> bool:
        """
        Quick check if URL is safe for downloading.

        Args:
            url: URL to check

        Returns:
            True if URL is safe to download from
        """
        result = self.validate_url(url)
        return result.is_valid and result.risk_level in ["low", "medium"]

    def add_blocked_domain(self, domain: str) -> None:
        """
        Add a domain to the blocked domains list.

        Args:
            domain: Domain to block
        """
        self.blocked_domains.add(domain.lower())
        logger.info(f"Added {domain} to blocked domains list")

    def remove_blocked_domain(self, domain: str) -> bool:
        """
        Remove a domain from the blocked domains list.

        Args:
            domain: Domain to unblock

        Returns:
            True if domain was removed, False if it wasn't blocked
        """
        domain_lower = domain.lower()
        if domain_lower in self.blocked_domains:
            self.blocked_domains.remove(domain_lower)
            logger.info(f"Removed {domain} from blocked domains list")
            return True
        return False

    def get_security_report(self, url: str) -> Dict[str, Any]:
        """
        Generate a detailed security report for a URL.

        Args:
            url: URL to analyze

        Returns:
            Dictionary with detailed security analysis
        """
        result = self.validate_url(url)

        report = {
            "url": url,
            "is_valid": result.is_valid,
            "risk_level": result.risk_level,
            "issues": result.issues,
            "cleaned_url": result.cleaned_url,
            "timestamp": __import__('time').time(),
        }

        if result.cleaned_url:
            parsed = urlparse(result.cleaned_url)
            report["analysis"] = {
                "scheme": parsed.scheme,
                "domain": parsed.netloc,
                "path": parsed.path,
                "has_query": bool(parsed.query),
                "domain_blocked": parsed.netloc.lower() in self.blocked_domains,
            }

        return report


# Global validator instance
_global_validator = None


def get_url_validator() -> URLSecurityValidator:
    """
    Get the global URL validator instance.

    Returns:
        Global URLSecurityValidator instance
    """
    global _global_validator
    if _global_validator is None:
        _global_validator = URLSecurityValidator()
    return _global_validator


def validate_url(url: str) -> ValidationResult:
    """
    Convenience function to validate a URL using the global validator.

    Args:
        url: URL to validate

    Returns:
        ValidationResult
    """
    return get_url_validator().validate_url(url)


def is_safe_url(url: str) -> bool:
    """
    Convenience function to check if URL is safe for downloading.

    Args:
        url: URL to check

    Returns:
        True if URL is safe
    """
    return get_url_validator().is_safe_for_download(url)
