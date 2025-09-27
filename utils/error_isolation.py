"""
Error isolation architecture for Reddit Stash media downloads.

This module provides robust error isolation to ensure that media download failures
never impact core text processing functionality. Implements circuit breaker patterns,
graceful degradation, and comprehensive error boundaries.
"""

import time
import logging
from typing import Dict, Any, Optional, Callable, TypeVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from threading import Lock


T = TypeVar('T')
R = TypeVar('R')


class CircuitState(Enum):
    """States of a circuit breaker."""
    CLOSED = "closed"        # Normal operation
    OPEN = "open"           # Failing, rejecting requests
    HALF_OPEN = "half_open" # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""
    failure_threshold: int = 5          # Failures before opening
    recovery_timeout: float = 60.0     # Seconds before trying half-open
    success_threshold: int = 3          # Successes needed to close from half-open
    timeout: float = 30.0              # Operation timeout in seconds


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker monitoring."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    opened_at: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    total_successes: int = 0


class CircuitBreaker:
    """
    Circuit breaker implementation for service resilience.

    Prevents cascading failures by temporarily blocking calls to failing services
    and allowing them time to recover. Based on the classic circuit breaker pattern
    with configurable thresholds and recovery mechanisms.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self.stats = CircuitBreakerStats()
        self._lock = Lock()

    def __call__(self, func: Callable[..., T]) -> Callable[..., Optional[T]]:
        """Decorator for protecting functions with circuit breaker."""
        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[T]:
            return self.call(func, *args, **kwargs)
        return wrapper

    def call(self, func: Callable[..., T], *args, **kwargs) -> Optional[T]:
        """
        Execute a function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result if successful, None if circuit is open
        """
        with self._lock:
            self.stats.total_requests += 1

            if self._should_reject_request():
                logging.warning(f"Circuit breaker {self.name} is OPEN, rejecting request")
                return None

            # Transition to half-open if we're testing recovery
            if self.stats.state == CircuitState.OPEN and self._should_attempt_reset():
                self._transition_to_half_open()

        try:
            # Execute the function with timeout protection
            start_time = time.time()
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time

            # Check for timeout
            if execution_time > self.config.timeout:
                raise TimeoutError(f"Operation timed out after {execution_time:.2f} seconds")

            self._on_success()
            return result

        except Exception as e:
            self._on_failure(e)
            return None

    def _should_reject_request(self) -> bool:
        """Check if request should be rejected due to circuit state."""
        return self.stats.state == CircuitState.OPEN and not self._should_attempt_reset()

    def _should_attempt_reset(self) -> bool:
        """Check if circuit should attempt to reset from open to half-open."""
        if self.stats.state != CircuitState.OPEN:
            return False
        return time.time() - self.stats.opened_at >= self.config.recovery_timeout

    def _transition_to_half_open(self):
        """Transition circuit from open to half-open state."""
        self.stats.state = CircuitState.HALF_OPEN
        self.stats.success_count = 0
        logging.info(f"Circuit breaker {self.name} transitioning to HALF_OPEN")

    def _on_success(self):
        """Handle successful operation."""
        with self._lock:
            self.stats.total_successes += 1
            self.stats.failure_count = 0

            if self.stats.state == CircuitState.HALF_OPEN:
                self.stats.success_count += 1
                if self.stats.success_count >= self.config.success_threshold:
                    self._transition_to_closed()
            elif self.stats.state == CircuitState.OPEN:
                # Shouldn't happen, but handle gracefully
                self._transition_to_closed()

    def _on_failure(self, error: Exception):
        """Handle failed operation."""
        with self._lock:
            self.stats.total_failures += 1
            self.stats.failure_count += 1
            self.stats.last_failure_time = time.time()

            logging.warning(f"Circuit breaker {self.name} recorded failure: {error}")

            if self.stats.state in [CircuitState.CLOSED, CircuitState.HALF_OPEN]:
                if self.stats.failure_count >= self.config.failure_threshold:
                    self._transition_to_open()

    def _transition_to_closed(self):
        """Transition circuit to closed (normal) state."""
        self.stats.state = CircuitState.CLOSED
        self.stats.failure_count = 0
        self.stats.success_count = 0
        logging.info(f"Circuit breaker {self.name} transitioning to CLOSED")

    def _transition_to_open(self):
        """Transition circuit to open (failing) state."""
        self.stats.state = CircuitState.OPEN
        self.stats.opened_at = time.time()
        logging.error(f"Circuit breaker {self.name} transitioning to OPEN after {self.stats.failure_count} failures")

    def get_stats(self) -> Dict[str, Any]:
        """Get current circuit breaker statistics."""
        return {
            'name': self.name,
            'state': self.stats.state.value,
            'failure_count': self.stats.failure_count,
            'success_count': self.stats.success_count,
            'total_requests': self.stats.total_requests,
            'total_failures': self.stats.total_failures,
            'total_successes': self.stats.total_successes,
            'last_failure_time': self.stats.last_failure_time,
            'opened_at': self.stats.opened_at if self.stats.state == CircuitState.OPEN else None,
            'config': {
                'failure_threshold': self.config.failure_threshold,
                'recovery_timeout': self.config.recovery_timeout,
                'success_threshold': self.config.success_threshold,
                'timeout': self.config.timeout
            }
        }


class MediaServiceError(Exception):
    """Base exception for media service errors."""
    pass


class MediaServiceUnavailable(MediaServiceError):
    """Raised when a media service is temporarily unavailable."""
    pass


class MediaDownloadFailed(MediaServiceError):
    """Raised when media download fails for a specific item."""
    pass


class ErrorBoundary:
    """
    Error boundary for isolating media operations from core functionality.

    Ensures that failures in media download operations never propagate to
    affect text processing or other core operations.
    """

    def __init__(self, name: str):
        self.name = name
        self._error_count = 0
        self._last_error_time = 0.0
        self._errors: list = []

    def execute_safely(self, operation: Callable[[], T],
                      fallback_value: Optional[T] = None) -> Optional[T]:
        """
        Execute an operation with error isolation.

        Args:
            operation: Function to execute safely
            fallback_value: Value to return if operation fails

        Returns:
            Operation result or fallback value
        """
        try:
            return operation()
        except Exception as e:
            self._record_error(e)
            logging.warning(f"Error boundary {self.name} caught error: {e}")
            return fallback_value

    def _record_error(self, error: Exception):
        """Record error for monitoring and debugging."""
        self._error_count += 1
        self._last_error_time = time.time()
        self._errors.append({
            'timestamp': time.time(),
            'error_type': type(error).__name__,
            'error_message': str(error),
            'count': self._error_count
        })

        # Keep only last 100 errors to prevent memory bloat
        if len(self._errors) > 100:
            self._errors = self._errors[-100:]

    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics for monitoring."""
        return {
            'name': self.name,
            'error_count': self._error_count,
            'last_error_time': self._last_error_time,
            'recent_errors': self._errors[-10:] if self._errors else []
        }


class MediaServiceManager:
    """
    Manager for media services with comprehensive error isolation.

    Coordinates multiple media services while ensuring failures are contained
    and don't affect core application functionality.
    """

    def __init__(self):
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._error_boundaries: Dict[str, ErrorBoundary] = {}
        self._service_configs: Dict[str, CircuitBreakerConfig] = {}

    def register_service(self, service_name: str,
                        config: Optional[CircuitBreakerConfig] = None):
        """Register a media service with error protection."""
        if config is None:
            config = CircuitBreakerConfig()

        self._service_configs[service_name] = config
        self._circuit_breakers[service_name] = CircuitBreaker(service_name, config)
        self._error_boundaries[service_name] = ErrorBoundary(service_name)

    def execute_with_protection(self, service_name: str,
                               operation: Callable[[], T],
                               fallback_value: Optional[T] = None) -> Optional[T]:
        """
        Execute a media operation with full error protection.

        Args:
            service_name: Name of the service
            operation: Operation to execute
            fallback_value: Fallback value if operation fails

        Returns:
            Operation result or fallback value
        """
        if service_name not in self._circuit_breakers:
            # Auto-register with default config
            self.register_service(service_name)

        circuit_breaker = self._circuit_breakers[service_name]
        error_boundary = self._error_boundaries[service_name]

        # Execute with circuit breaker protection
        result = circuit_breaker.call(operation)

        # If circuit breaker returned None (failed or circuit open), use fallback
        if result is None:
            error_boundary._record_error(MediaServiceUnavailable(f"Service {service_name} failed or unavailable"))
            return fallback_value

        return result

    def is_service_available(self, service_name: str) -> bool:
        """Check if a service is currently available."""
        if service_name not in self._circuit_breakers:
            return True  # Unknown services are assumed available

        circuit_breaker = self._circuit_breakers[service_name]
        return circuit_breaker.stats.state != CircuitState.OPEN

    def get_service_health(self) -> Dict[str, Any]:
        """Get health status of all managed services."""
        health = {}
        for service_name in self._circuit_breakers:
            circuit_stats = self._circuit_breakers[service_name].get_stats()
            error_stats = self._error_boundaries[service_name].get_error_stats()

            health[service_name] = {
                'available': self.is_service_available(service_name),
                'circuit_breaker': circuit_stats,
                'error_boundary': error_stats
            }

        return health

    def reset_service(self, service_name: str):
        """Manually reset a service's error state."""
        if service_name in self._circuit_breakers:
            circuit_breaker = self._circuit_breakers[service_name]
            circuit_breaker._transition_to_closed()
            logging.info(f"Manually reset service {service_name}")


# Global service manager instance
_service_manager = None

def get_service_manager() -> MediaServiceManager:
    """Get the global media service manager instance."""
    global _service_manager
    if _service_manager is None:
        _service_manager = MediaServiceManager()
    return _service_manager


def isolate_media_operation(service_name: str, fallback_value: Optional[T] = None):
    """
    Decorator for isolating media operations with circuit breaker protection.

    Args:
        service_name: Name of the media service
        fallback_value: Value to return if operation fails

    Example:
        @isolate_media_operation('imgur', fallback_value=DownloadResult(status=DownloadStatus.FAILED))
        def download_imgur_image(url: str) -> DownloadResult:
            # Implementation that might fail
            pass
    """
    def decorator(func: Callable[..., T]) -> Callable[..., Optional[T]]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Optional[T]:
            manager = get_service_manager()
            return manager.execute_with_protection(
                service_name,
                lambda: func(*args, **kwargs),
                fallback_value
            )
        return wrapper
    return decorator