"""Recovery test fixtures: a real ContentRecoveryService, and answers from archive APIs."""

import logging
from unittest.mock import Mock, patch

import requests

from utils.content_recovery.recovery_config import RecoveryConfig
from utils.content_recovery.recovery_service import ContentRecoveryService

WAYBACK = 'utils.content_recovery.providers.wayback_provider.rate_limit_manager'
RATE_LIMIT_TOKEN = 'utils.rate_limiter.rate_limit_manager.acquire'


def fake_response(status_code=200, payload=None, reason='OK', headers=None):
    response = requests.Response()
    response.status_code = status_code
    response.reason = reason
    response.url = 'https://archive.example/lookup'
    response.headers.update(headers or {})
    response.json = lambda: payload
    return response


def fake_session(*responses):
    return Mock(headers={}, **{'get.side_effect': list(responses)})


def recovery_service(providers=None, cache_manager=None, **recovery_config):
    """Return a service using the given providers and cache, or those its config enables."""
    settings = {
        'use_wayback_machine': False,
        'use_arctic_shift': False,
        'use_pullpush': False,
        **recovery_config,
    }
    cache = cache_manager or Mock(**{'get_cached_result.return_value': None})
    logging.disable(logging.WARNING)  # "no providers enabled" is expected before they are assigned
    try:
        with patch('utils.content_recovery.recovery_service.RecoveryCacheManager', return_value=cache):
            service = ContentRecoveryService(RecoveryConfig(**settings))
    finally:
        logging.disable(logging.NOTSET)
    if providers is not None:
        service.providers = providers
    return service
