"""
Advanced rate limiting system with per-service controls and coordination.

This module implements sophisticated rate limiting following 2024 best practices:
- Token bucket algorithm for smooth rate limiting
- Per-service configuration and isolation
- Sliding window tracking for accurate enforcement
- Async-safe implementation with thread safety
- Burst handling and request queuing
- Adaptive rate limiting based on service responses
"""

import time
import threading
import random
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass, field
from collections import deque
import logging

from .service_abstractions import ServiceConfig


@dataclass(frozen=True)
class RateLimitConfig:
    """Configuration for rate limiting behavior."""
    max_requests_per_minute: int
    burst_capacity: int = field(default_factory=lambda: 5)  # Allow burst of 5 requests
    window_size_seconds: int = 60
    retry_after_seconds: int = 60
    adaptive_scaling: bool = True
    backoff_multiplier: float = 1.5
    max_backoff_seconds: int = 300  # 5 minutes max


@dataclass
class RateLimitState:
    """Internal state tracking for rate limiter."""
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.time)
    request_times: deque = field(default_factory=deque)
    consecutive_failures: int = 0
    current_backoff: float = 0.0
    is_rate_limited: bool = False
    rate_limit_reset_time: Optional[float] = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter with sliding window validation.

    Implements the token bucket algorithm which allows for burst traffic
    while maintaining average rate limits over time.
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.state = RateLimitState()
        self.state.tokens = float(config.burst_capacity)
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def can_proceed(self) -> bool:
        """Check if request can proceed without blocking."""
        with self.state.lock:
            self._refill_tokens()
            self._cleanup_old_requests()
            self._check_rate_limit_status()

            return (
                self.state.tokens >= 1.0 and
                not self.state.is_rate_limited and
                len(self.state.request_times) < self.config.max_requests_per_minute
            )

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire permission to make a request.

        Args:
            timeout: Maximum time to wait for permission (None = no timeout)

        Returns:
            True if permission granted, False if timed out
        """
        start_time = time.time()

        while True:
            with self.state.lock:
                self._refill_tokens()
                self._cleanup_old_requests()
                self._check_rate_limit_status()

                # Check if we can proceed immediately
                if (self.state.tokens >= 1.0 and
                    not self.state.is_rate_limited and
                    len(self.state.request_times) < self.config.max_requests_per_minute):

                    # Consume token and record request
                    self.state.tokens -= 1.0
                    self.state.request_times.append(time.time())
                    self._logger.debug(f"Request acquired, tokens remaining: {self.state.tokens}")
                    return True

                # Check timeout
                if timeout is not None and (time.time() - start_time) >= timeout:
                    self._logger.warning("Rate limiter acquisition timed out")
                    return False

                # Calculate wait time
                wait_time = self._calculate_wait_time()

            # Wait outside the lock to allow other threads
            if wait_time > 0:
                time.sleep(min(wait_time, 1.0))  # Sleep in 1-second chunks
            else:
                time.sleep(0.1)  # Small delay to prevent busy waiting

    def report_response(self, status_code: int, retry_after: Optional[int] = None) -> None:
        """
        Report response status to adapt rate limiting behavior.

        Args:
            status_code: HTTP status code
            retry_after: Retry-After header value in seconds
        """
        with self.state.lock:
            if status_code == 429:  # Rate limited
                self.state.consecutive_failures += 1
                self.state.is_rate_limited = True

                if retry_after:
                    self.state.rate_limit_reset_time = time.time() + retry_after
                else:
                    # Apply exponential backoff
                    self.state.current_backoff = min(
                        self.config.retry_after_seconds * (self.config.backoff_multiplier ** self.state.consecutive_failures),
                        self.config.max_backoff_seconds
                    )
                    self.state.rate_limit_reset_time = time.time() + self.state.current_backoff

                self._logger.warning(f"Rate limited, reset time: {self.state.rate_limit_reset_time}")

            elif 200 <= status_code < 300:  # Success
                self.state.consecutive_failures = 0
                self.state.current_backoff = 0.0

            elif 500 <= status_code < 600:  # Server error
                self.state.consecutive_failures += 1
                # Don't mark as rate limited, but increase backoff for server errors
                if self.config.adaptive_scaling:
                    self.state.current_backoff = min(
                        1.0 * (self.config.backoff_multiplier ** self.state.consecutive_failures),
                        30.0  # Max 30 seconds for server errors
                    )

    def _refill_tokens(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.state.last_refill

        if elapsed > 0:
            # Calculate tokens to add (rate per second)
            tokens_per_second = self.config.max_requests_per_minute / 60.0
            tokens_to_add = elapsed * tokens_per_second

            # Add tokens but don't exceed burst capacity
            self.state.tokens = min(
                self.state.tokens + tokens_to_add,
                float(self.config.burst_capacity)
            )
            self.state.last_refill = now

    def _cleanup_old_requests(self) -> None:
        """Remove request times outside the sliding window."""
        cutoff_time = time.time() - self.config.window_size_seconds
        while (self.state.request_times and
               self.state.request_times[0] < cutoff_time):
            self.state.request_times.popleft()

    def _check_rate_limit_status(self) -> None:
        """Check if rate limit period has expired."""
        if (self.state.is_rate_limited and
            self.state.rate_limit_reset_time and
            time.time() >= self.state.rate_limit_reset_time):

            self.state.is_rate_limited = False
            self.state.rate_limit_reset_time = None
            self._logger.info("Rate limit period expired, resuming normal operation")

    def _calculate_wait_time(self) -> float:
        """Calculate how long to wait before next attempt with jitter."""
        wait_times = []

        # Wait for rate limit to reset
        if self.state.is_rate_limited and self.state.rate_limit_reset_time:
            wait_times.append(self.state.rate_limit_reset_time - time.time())

        # Wait for token refill
        if self.state.tokens < 1.0:
            tokens_needed = 1.0 - self.state.tokens
            tokens_per_second = self.config.max_requests_per_minute / 60.0
            wait_times.append(tokens_needed / tokens_per_second)

        # Wait for sliding window
        if len(self.state.request_times) >= self.config.max_requests_per_minute:
            oldest_request = self.state.request_times[0]
            window_reset = oldest_request + self.config.window_size_seconds
            wait_times.append(window_reset - time.time())

        # Add current backoff
        if self.state.current_backoff > 0:
            wait_times.append(self.state.current_backoff)

        base_wait = max(wait_times) if wait_times else 0.0

        # Add jitter to prevent retry storms (AWS recommended approach)
        if base_wait > 0:
            # Add ±25% jitter to prevent synchronized retries
            jitter_factor = random.uniform(0.75, 1.25)
            return base_wait * jitter_factor

        return base_wait

    def get_status(self) -> Dict[str, Any]:
        """Get current rate limiter status for monitoring."""
        with self.state.lock:
            return {
                'tokens_available': self.state.tokens,
                'max_tokens': self.config.burst_capacity,
                'requests_in_window': len(self.state.request_times),
                'max_requests_per_minute': self.config.max_requests_per_minute,
                'is_rate_limited': self.state.is_rate_limited,
                'consecutive_failures': self.state.consecutive_failures,
                'current_backoff': self.state.current_backoff,
                'rate_limit_reset_time': self.state.rate_limit_reset_time
            }


class ServiceRateLimitManager:
    """
    Manages rate limiters for multiple services with coordination.

    Provides centralized rate limiting management with per-service
    configuration and global coordination capabilities.
    Implements RateLimiterProtocol for dependency injection.
    """

    def __init__(self):
        self._rate_limiters: Dict[str, TokenBucketRateLimiter] = {}
        self._service_configs: Dict[str, RateLimitConfig] = {}
        self._global_lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def register_service(self, service_name: str, config: RateLimitConfig) -> None:
        """Register a service with its rate limiting configuration."""
        with self._global_lock:
            self._service_configs[service_name] = config
            self._rate_limiters[service_name] = TokenBucketRateLimiter(config)
            self._logger.info(f"Registered rate limiter for service '{service_name}': "
                            f"{config.max_requests_per_minute} req/min")

    def register_service_from_config(self, service_name: str, service_config: ServiceConfig) -> None:
        """Register a service using ServiceConfig."""
        rate_config = RateLimitConfig(
            max_requests_per_minute=service_config.rate_limit_per_minute,
            burst_capacity=max(5, service_config.rate_limit_per_minute // 10),
            adaptive_scaling=True
        )
        self.register_service(service_name, rate_config)

    def can_proceed(self, service_name: str) -> bool:
        """Check if service can proceed without blocking."""
        limiter = self._get_limiter(service_name)
        return limiter.can_proceed() if limiter else True

    def record_request(self, service_name: str, success: bool = True) -> None:
        """Record a request for rate limiting purposes."""
        # This is handled automatically by acquire() and report_response()
        # Provided for Protocol compatibility
        pass

    def get_wait_time(self, service_name: str) -> float:
        """Get the time to wait before the next request."""
        limiter = self._get_limiter(service_name)
        if not limiter:
            return 0.0

        with limiter.state.lock:
            return limiter._calculate_wait_time()

    def handle_rate_limit_response(self, service_name: str, retry_after: Optional[int] = None) -> None:
        """Handle a rate limit response from a service."""
        self.report_response(service_name, 429, retry_after)

    def acquire(self, service_name: str, timeout: Optional[float] = None) -> bool:
        """Acquire permission for service request."""
        limiter = self._get_limiter(service_name)
        if not limiter:
            self._logger.warning(f"No rate limiter registered for service '{service_name}'")
            return True

        return limiter.acquire(timeout)

    def report_response(self, service_name: str, status_code: int,
                       retry_after: Optional[int] = None) -> None:
        """Report response status for adaptive rate limiting."""
        limiter = self._get_limiter(service_name)
        if limiter:
            limiter.report_response(status_code, retry_after)

    def get_service_status(self, service_name: str) -> Optional[Dict[str, Any]]:
        """Get status for specific service."""
        limiter = self._get_limiter(service_name)
        return limiter.get_status() if limiter else None

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status for all registered services."""
        with self._global_lock:
            return {
                service_name: limiter.get_status()
                for service_name, limiter in self._rate_limiters.items()
            }

    def _get_limiter(self, service_name: str) -> Optional[TokenBucketRateLimiter]:
        """Get rate limiter for service name."""
        with self._global_lock:
            return self._rate_limiters.get(service_name)

    def reset_service(self, service_name: str) -> bool:
        """Reset rate limiter for a service."""
        with self._global_lock:
            if service_name in self._service_configs:
                config = self._service_configs[service_name]
                self._rate_limiters[service_name] = TokenBucketRateLimiter(config)
                self._logger.info(f"Reset rate limiter for service '{service_name}'")
                return True
            return False


# Global rate limit manager instance
rate_limit_manager = ServiceRateLimitManager()


def rate_limited(service_name: str, timeout: Optional[float] = None):
    """
    Decorator to automatically apply rate limiting to functions.

    Args:
        service_name: Name of the service for rate limiting
        timeout: Maximum time to wait for rate limit clearance

    Usage:
        @rate_limited('imgur', timeout=30)
        def download_imgur_image(url):
            # Function implementation
            pass
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            # Acquire rate limit permission
            if not rate_limit_manager.acquire(service_name, timeout):
                raise RuntimeError(f"Rate limit timeout for service '{service_name}'")

            try:
                result = func(*args, **kwargs)
                # Report success if result looks like HTTP response
                if hasattr(result, 'status') and hasattr(result.status, 'value'):
                    # Assume it's a DownloadResult with DownloadStatus
                    if result.status.value in ['SUCCESS', 'COMPLETED']:
                        rate_limit_manager.report_response(service_name, 200)
                    else:
                        rate_limit_manager.report_response(service_name, 500)
                return result
            except Exception as e:
                # Try to extract status code from exception
                status_code = 500
                retry_after = None

                if hasattr(e, 'response'):
                    status_code = getattr(e.response, 'status_code', 500)
                    if hasattr(e.response, 'headers'):
                        retry_after_header = e.response.headers.get('retry-after')
                        if retry_after_header:
                            try:
                                retry_after = int(retry_after_header)
                            except ValueError:
                                pass

                rate_limit_manager.report_response(service_name, status_code, retry_after)
                raise

        return wrapper
    return decorator


def setup_default_rate_limiters() -> None:
    """Setup rate limiters for known services with reasonable defaults."""
    # Reddit rate limits (conservative, PRAW handles most of this)
    reddit_config = RateLimitConfig(
        max_requests_per_minute=100,
        burst_capacity=10,
        adaptive_scaling=True
    )
    rate_limit_manager.register_service('reddit', reddit_config)

    # Imgur rate limits (extremely conservative due to strict IP limits and rolling window)
    imgur_config = RateLimitConfig(
        max_requests_per_minute=4,  # Very conservative - provides 260 req/hour buffer for rolling window
        burst_capacity=2,  # Reduced burst to be extra safe
        adaptive_scaling=True,
        retry_after_seconds=120,  # Longer backoff for Imgur
        backoff_multiplier=2.0
    )
    rate_limit_manager.register_service('imgur', imgur_config)

    # Generic HTTP rate limits for unknown services
    generic_config = RateLimitConfig(
        max_requests_per_minute=30,
        burst_capacity=5,
        adaptive_scaling=True
    )
    rate_limit_manager.register_service('generic', generic_config)

    # Content Recovery Services Rate Limits

    # Wayback Machine - no hard limits but be respectful
    wayback_config = RateLimitConfig(
        max_requests_per_minute=60,  # Be gentle but reasonable
        burst_capacity=5,
        adaptive_scaling=True,
        retry_after_seconds=30
    )
    rate_limit_manager.register_service('wayback_machine', wayback_config)

    # PullPush.io - strict rate limits (15 req/min soft, 30 req/min hard)
    pullpush_config = RateLimitConfig(
        max_requests_per_minute=12,  # Stay well under 15 req/min soft limit
        burst_capacity=3,
        adaptive_scaling=True,
        retry_after_seconds=300,  # 5 minutes backoff if rate limited
        backoff_multiplier=3.0  # Aggressive backoff for strict API
    )
    rate_limit_manager.register_service('pullpush_io', pullpush_config)

    # Reddit Previews - subject to Reddit rate limiting
    reddit_previews_config = RateLimitConfig(
        max_requests_per_minute=30,  # Conservative for Reddit services
        burst_capacity=5,
        adaptive_scaling=True,
        retry_after_seconds=60
    )
    rate_limit_manager.register_service('reddit_previews', reddit_previews_config)

    # Reveddit - no specific limits documented, be conservative
    reveddit_config = RateLimitConfig(
        max_requests_per_minute=20,  # Conservative approach
        burst_capacity=3,
        adaptive_scaling=True,
        retry_after_seconds=60
    )
    rate_limit_manager.register_service('reveddit', reveddit_config)


# Initialize default rate limiters on module load
setup_default_rate_limiters()