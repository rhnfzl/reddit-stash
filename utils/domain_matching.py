"""Safe hostname matching helpers."""


def domain_matches(hostname, expected_domain):
    """Return whether a hostname is an exact domain or one of its subdomains."""
    host = (hostname or '').rstrip('.').lower()
    expected = (expected_domain or '').rstrip('.').lower()
    return bool(expected) and (host == expected or host.endswith(f'.{expected}'))
