"""Settings for the recovery providers and their cache, from the [Recovery] section."""

import logging
from dataclasses import dataclass

from utils.env_config import load_settings


@dataclass(frozen=True)
class RecoveryConfig:
    """Recovery provider switches and limits from the [Recovery] section."""
    use_wayback_machine: bool = True
    use_arctic_shift: bool = True
    use_pullpush: bool = True
    timeout_seconds: int = 10
    cache_duration_hours: int = 24


def load_recovery_config() -> RecoveryConfig:
    """Read the recovery settings; a malformed value raises ValueError."""
    parser = load_settings()
    return RecoveryConfig(
        use_wayback_machine=parser.getboolean('Recovery', 'use_wayback_machine', fallback=True),
        use_arctic_shift=parser.getboolean('Recovery', 'use_arctic_shift', fallback=True),
        # The settings key keeps its historical name; it switches the PullPush provider
        use_pullpush=parser.getboolean('Recovery', 'use_pushshift_api', fallback=True),
        timeout_seconds=parser.getint('Recovery', 'timeout_seconds', fallback=10),
        cache_duration_hours=parser.getint('Recovery', 'cache_duration_hours', fallback=24),
    )


@dataclass
class CacheConfig:
    """Configuration for cache behavior."""
    max_cache_entries: int = 10000
    max_cache_size_mb: int = 100
    cleanup_interval_minutes: int = 60
    enable_background_cleanup: bool = True


def load_cache_config() -> CacheConfig:
    """Load cache configuration from settings.ini with fallbacks to defaults."""
    # Use defaults if config file doesn't exist or section is missing
    cache_config = CacheConfig()

    try:
        config = load_settings()

        if config.has_section('Recovery'):
            recovery_section = config['Recovery']
            cache_config.max_cache_entries = recovery_section.getint('max_cache_entries', cache_config.max_cache_entries)
            cache_config.max_cache_size_mb = recovery_section.getint('max_cache_size_mb', cache_config.max_cache_size_mb)
            cache_config.cleanup_interval_minutes = recovery_section.getint('cleanup_interval_minutes', cache_config.cleanup_interval_minutes)
            cache_config.enable_background_cleanup = recovery_section.getboolean('enable_background_cleanup', cache_config.enable_background_cleanup)

    except Exception as e:
        # Log warning but continue with defaults
        logging.getLogger(__name__).warning(f"Failed to load cache configuration: {e}. Using defaults.")

    return cache_config
