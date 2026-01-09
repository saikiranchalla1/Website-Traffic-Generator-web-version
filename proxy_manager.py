"""
Proxy Manager Module

Provides functionality to manage and use proxy servers for HTTP requests.
Includes caching, validation, and automatic failover to direct connections.
"""

import logging
import os
import random
import threading
from datetime import datetime, timedelta

import requests

# Configure module logger
logger = logging.getLogger(__name__)

# Constants
PROXY_CACHE_TTL_MINUTES = 30
PROXY_TEST_TIMEOUT = 5
MAX_FAILED_ATTEMPTS = 3
MAX_PROXIES_TO_CHECK = 20
MAX_WORKING_PROXIES = 5


class ProxyCache:
    """Thread-safe proxy cache with automatic expiration."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proxies = []
        self._last_updated = None
        self._failed_attempts = 0
        self._disabled = False

    @property
    def proxies(self):
        """Get the list of cached proxies."""
        with self._lock:
            return list(self._proxies)

    @proxies.setter
    def proxies(self, value):
        """Set the list of cached proxies."""
        with self._lock:
            self._proxies = list(value) if value else []

    @property
    def last_updated(self):
        """Get the last update timestamp."""
        with self._lock:
            return self._last_updated

    @last_updated.setter
    def last_updated(self, value):
        """Set the last update timestamp."""
        with self._lock:
            self._last_updated = value

    @property
    def failed_attempts(self):
        """Get the number of failed attempts."""
        with self._lock:
            return self._failed_attempts

    def increment_failures(self):
        """Increment the failure counter."""
        with self._lock:
            self._failed_attempts += 1
            if self._failed_attempts >= MAX_FAILED_ATTEMPTS:
                self._disabled = True
                logger.warning(
                    f"Proxy connection failed {MAX_FAILED_ATTEMPTS} times. "
                    "Disabling proxies for this session."
                )
            return self._failed_attempts

    def reset_failures(self):
        """Reset the failure counter."""
        with self._lock:
            self._failed_attempts = 0

    @property
    def is_disabled(self):
        """Check if proxies are disabled."""
        with self._lock:
            return self._disabled

    @is_disabled.setter
    def is_disabled(self, value):
        """Set the disabled state."""
        with self._lock:
            self._disabled = value

    def is_stale(self):
        """Check if the cache needs to be refreshed."""
        with self._lock:
            if self._last_updated is None:
                return True
            ttl = timedelta(minutes=PROXY_CACHE_TTL_MINUTES)
            return datetime.now() - self._last_updated > ttl

    def reset(self):
        """Reset the cache to initial state."""
        with self._lock:
            self._failed_attempts = 0
            self._disabled = False
            self._proxies = []
            self._last_updated = None


# Global proxy cache instance
_proxy_cache = ProxyCache()


def get_random_proxy():
    """
    Get a random working proxy, or None to use direct connection.

    Returns:
        str: Proxy in format "http://ip:port" or None to use direct connection
    """
    # Check if proxies are disabled via environment variable
    if os.environ.get("DISABLE_PROXIES", "false").lower() == "true":
        logger.info(
            "Proxy usage is disabled by environment variable. Using direct connection."
        )
        return None

    # Check if proxies are disabled due to failures
    if _proxy_cache.is_disabled:
        logger.info(
            "Proxies are disabled due to multiple connection failures. Using direct connection."
        )
        return None

    # Refresh proxy list if stale or empty
    if _proxy_cache.is_stale() or not _proxy_cache.proxies:
        refresh_proxy_list()

    # Return a random proxy if available
    proxies = _proxy_cache.proxies
    if proxies:
        proxy = random.choice(proxies)
        logger.info(f"Using proxy: {proxy}")
        return proxy

    # No proxies available, increment failure counter
    attempts = _proxy_cache.increment_failures()
    logger.warning(
        f"No working proxies found (attempt {attempts} of {MAX_FAILED_ATTEMPTS}). "
        "Using direct connection."
    )

    return None


def refresh_proxy_list():
    """Refresh the list of working proxies."""
    logger.info("Refreshing proxy list...")

    proxies = []

    # Try to get proxies from environment variable first
    env_proxies = os.environ.get("PROXY_LIST", "")
    if env_proxies:
        proxies = _validate_env_proxies(env_proxies)

    # If no environment proxies, try to fetch from free proxy lists
    if not proxies and os.environ.get("FETCH_FREE_PROXIES", "true").lower() == "true":
        proxies = _fetch_free_proxies()

    # Update the cache
    _proxy_cache.proxies = proxies
    _proxy_cache.last_updated = datetime.now()

    if proxies:
        logger.info(f"Proxy list refreshed. Found {len(proxies)} working proxies.")
        _proxy_cache.reset_failures()
    else:
        logger.warning("No working proxies found. Will use direct connection.")


def _validate_env_proxies(env_proxies):
    """Validate proxies from environment variable."""
    valid_proxies = []
    proxy_list = [p.strip() for p in env_proxies.split(",") if p.strip()]

    for proxy in proxy_list:
        if is_proxy_working(proxy):
            valid_proxies.append(proxy)

    return valid_proxies


def _fetch_free_proxies():
    """
    Fetch proxies from free proxy lists.

    Returns:
        list: List of working proxies
    """
    working_proxies = []
    proxies_checked = 0

    try:
        # Try to get from public API (example only - may not work)
        response = requests.get(
            "https://www.proxy-list.download/api/v1/get?type=http", timeout=10
        )

        if response.status_code != 200:
            logger.warning(
                f"Proxy list API returned status code {response.status_code}"
            )
            return []

        potential_proxies = response.text.strip().split("\r\n")

        for proxy in potential_proxies:
            if not proxy:  # Skip empty lines
                continue

            proxy_url = f"http://{proxy}"
            logger.info(f"Testing proxy: {proxy_url}")

            proxies_checked += 1
            if is_proxy_working(proxy_url):
                working_proxies.append(proxy_url)
                logger.info(f"Found working proxy: {proxy_url}")

                # Limit to MAX_WORKING_PROXIES to avoid taking too long
                if len(working_proxies) >= MAX_WORKING_PROXIES:
                    break

            # Stop checking after MAX_PROXIES_TO_CHECK
            if proxies_checked >= MAX_PROXIES_TO_CHECK:
                break

    except requests.exceptions.Timeout:
        logger.warning("Timeout fetching proxy list")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error fetching proxies: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching proxies: {e}")

    return working_proxies


def is_proxy_working(proxy_url):
    """
    Test if a proxy is working by making a test request.

    Args:
        proxy_url (str): Proxy URL to test

    Returns:
        bool: True if proxy is working, False otherwise
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return False

    try:
        # Normalize proxy URL
        if not proxy_url.startswith(("http://", "https://")):
            proxy_url = f"http://{proxy_url}"

        proxies = {"http": proxy_url, "https": proxy_url}

        # Test proxy with a request to a reliable service
        response = requests.get(
            "https://httpbin.org/ip", proxies=proxies, timeout=PROXY_TEST_TIMEOUT
        )

        if response.status_code == 200:
            logger.info(f"Proxy {proxy_url} is working")
            return True

        logger.info(f"Proxy {proxy_url} returned status code {response.status_code}")
        return False

    except requests.exceptions.Timeout:
        logger.debug(f"Proxy {proxy_url} timed out")
        return False
    except requests.exceptions.RequestException as e:
        logger.debug(f"Proxy {proxy_url} is not working: {e}")
        return False
    except Exception as e:
        logger.debug(f"Unexpected error testing proxy {proxy_url}: {e}")
        return False


def reset_proxy_failures():
    """Reset the failure counter and re-enable proxies."""
    _proxy_cache.reset()
    logger.info("Proxy failure counter reset. Proxies re-enabled.")


def get_proxy_status():
    """
    Get the current status of the proxy system.

    Returns:
        dict: Status information about the proxy system
    """
    return {
        "is_disabled": _proxy_cache.is_disabled,
        "failed_attempts": _proxy_cache.failed_attempts,
        "proxy_count": len(_proxy_cache.proxies),
        "last_updated": (
            _proxy_cache.last_updated.isoformat() if _proxy_cache.last_updated else None
        ),
        "is_stale": _proxy_cache.is_stale(),
    }
