"""
Per-service circuit breakers for Reddit Stash media downloads, so media download
failures never impact core text processing.
"""

import time
import logging
from typing import TypeVar
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Lock


logger = logging.getLogger(__name__)

T = TypeVar('T')


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


@dataclass
class CircuitBreakerStats:
    """Current counters behind a circuit breaker's state."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float = 0.0


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

    def call(self, func: Callable[[], T],
             is_failure: Callable[[T], bool] | None = None) -> T | None:
        """
        Execute a function with circuit breaker protection.

        Args:
            func: Function to execute
            is_failure: Tells whether a returned value counts as a failure of the service

        Returns:
            What func returned, even if is_failure counted it as a failure.
            None if the circuit is open or func raised.
        """
        with self._lock:
            if self._should_reject_request():
                logger.warning(f"Circuit breaker {self.name} is OPEN, rejecting request")
                return None

            # Transition to half-open if we're testing recovery
            if self.stats.state == CircuitState.OPEN:
                self._transition_to_half_open()

            opened_at_start = self.stats.opened_at

        try:
            result = func()
        except Exception as e:
            self._on_failure(e)
            return None

        if is_failure is not None and is_failure(result):
            self._on_failure(result)
        else:
            self._on_success(opened_at_start)
        return result

    def is_rejecting(self) -> bool:
        """Check whether a call made now would be rejected."""
        with self._lock:
            return self._should_reject_request()

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
        logger.info(f"Circuit breaker {self.name} transitioning to HALF_OPEN")

    def _on_success(self, opened_at_start: float):
        """Handle successful operation."""
        with self._lock:
            # A call that began before the breaker last opened says nothing about recovery.
            if self.stats.opened_at != opened_at_start:
                return

            self.stats.failure_count = 0

            if self.stats.state == CircuitState.HALF_OPEN:
                self.stats.success_count += 1
                if self.stats.success_count >= self.config.success_threshold:
                    self._transition_to_closed()

    def _on_failure(self, error: object):
        """Handle failed operation."""
        with self._lock:
            self.stats.failure_count += 1

            logger.warning(f"Circuit breaker {self.name} recorded failure: {error}")

            if (self.stats.state == CircuitState.HALF_OPEN
                    or (self.stats.state == CircuitState.CLOSED
                        and self.stats.failure_count >= self.config.failure_threshold)):
                self._transition_to_open()

    def _transition_to_closed(self):
        """Transition circuit to closed (normal) state."""
        self.stats.state = CircuitState.CLOSED
        self.stats.failure_count = 0
        self.stats.success_count = 0
        logger.info(f"Circuit breaker {self.name} transitioning to CLOSED")

    def _transition_to_open(self):
        """Transition circuit to open (failing) state."""
        self.stats.state = CircuitState.OPEN
        self.stats.opened_at = time.time()
        logger.error(f"Circuit breaker {self.name} transitioning to OPEN after {self.stats.failure_count} failures")
