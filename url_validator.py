"""
URL Validator Module

Provides functionality to validate URL format and accessibility.
"""

import logging
import random
import time
from urllib.parse import urlparse

import requests
import validators

from user_agents import get_random_user_agent

# Configure module logger
logger = logging.getLogger(__name__)

# Constants
DEFAULT_TIMEOUT = 10
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")
MIN_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 1.5


def validate_url(url, timeout=DEFAULT_TIMEOUT):
    """
    Validate if a URL is properly formatted and accessible.

    Args:
        url (str): The URL to validate
        timeout (int): Request timeout in seconds

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    # Input validation
    if not url or not isinstance(url, str):
        return False, "URL is required"

    url = url.strip()

    # Check URL length
    if len(url) > MAX_URL_LENGTH:
        return False, f"URL is too long (maximum {MAX_URL_LENGTH} characters)"

    # Check if URL is properly formatted using validators library
    if not validators.url(url):
        return (
            False,
            "Invalid URL format. Please enter a valid URL including http:// or https://",
        )

    # Check if URL has a valid scheme
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ALLOWED_SCHEMES:
        return False, "Only HTTP and HTTPS protocols are supported"

    # Validate hostname is present
    if not parsed_url.netloc:
        return False, "Invalid URL: hostname is required"

    # Try to access the URL to verify it's active
    return _check_url_accessibility(url, timeout)


def _check_url_accessibility(url, timeout):
    """
    Check if a URL is accessible by making a HEAD request.

    Args:
        url (str): The URL to check
        timeout (int): Request timeout in seconds

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    try:
        headers = _build_request_headers()

        # Add random delay to mimic human behavior
        delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        time.sleep(delay)

        # Try HEAD request first (faster)
        response = requests.head(
            url, headers=headers, timeout=timeout, allow_redirects=True
        )

        # Some servers don't support HEAD, fall back to GET if needed
        if response.status_code == 405:  # Method Not Allowed
            logger.debug(f"HEAD not supported for {url}, trying GET")
            response = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                stream=True,  # Don't download body
            )
            response.close()

        return _evaluate_response(response)

    except requests.exceptions.ConnectTimeout:
        logger.warning(f"Connection timeout for URL: {url}")
        return False, "Connection timed out. The website might be down or too slow."

    except requests.exceptions.ReadTimeout:
        logger.warning(f"Read timeout for URL: {url}")
        return False, "Request timed out while reading the response."

    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Connection error for URL {url}: {e}")
        return False, "Failed to establish a connection. The website might be down."

    except requests.exceptions.TooManyRedirects:
        logger.warning(f"Too many redirects for URL: {url}")
        return False, "Too many redirects. The URL might be in a redirect loop."

    except requests.exceptions.SSLError as e:
        logger.warning(f"SSL error for URL {url}: {e}")
        return (
            False,
            "SSL certificate error. The website's security certificate may be invalid.",
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error for URL {url}: {e}")
        return False, f"An error occurred while validating the URL: {str(e)}"

    except Exception as e:
        logger.error(f"Unexpected error validating URL {url}: {e}")
        return False, "An unexpected error occurred during validation"


def _build_request_headers():
    """Build HTTP headers for the validation request."""
    return {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _evaluate_response(response):
    """
    Evaluate HTTP response and return validation result.

    Args:
        response: requests.Response object

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    status_code = response.status_code

    if 200 <= status_code < 300:
        return True, "URL is valid and accessible"

    if 300 <= status_code < 400:
        return True, "URL is valid but redirects to another location"

    if status_code == 401:
        return True, "URL is valid but requires authentication"

    if status_code == 403:
        return True, "URL is valid but access is forbidden"

    if status_code == 404:
        return False, "URL not found (404). The page may have been moved or deleted."

    if 400 <= status_code < 500:
        return False, f"URL returned client error (status code {status_code})"

    if 500 <= status_code < 600:
        return (
            False,
            f"URL returned server error (status code {status_code}). The server may be experiencing issues.",
        )

    return False, f"URL returned unexpected status code {status_code}"


def is_safe_url(url, allowed_hosts=None):
    """
    Check if a URL is safe for redirection.

    Args:
        url (str): The URL to check
        allowed_hosts (set): Set of allowed hostnames (optional)

    Returns:
        bool: True if URL is safe, False otherwise
    """
    if not url:
        return False

    # Check for dangerous schemes
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme not in ALLOWED_SCHEMES:
        return False

    # Check for javascript: or data: URLs
    if url.lower().startswith(("javascript:", "data:", "vbscript:")):
        return False

    # Check allowed hosts if provided
    if allowed_hosts and parsed.netloc:
        if parsed.netloc not in allowed_hosts:
            return False

    return True
