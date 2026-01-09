import logging
import random
import threading
import time
import urllib.parse
from collections import Counter
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from user_agents import get_random_user_agent

# Configure module logger
logger = logging.getLogger(__name__)


class TrafficGenerator:
    """
    Traffic generator that simulates human-like browsing behavior.

    This class handles visiting URLs on a target website with configurable
    visit counts and realistic browsing patterns.
    """

    # Class-level constants
    MAX_VISITS_CAP = 500
    MIN_DELAY_SECONDS = 5
    MAX_DELAY_SECONDS = 15
    REQUEST_TIMEOUT = 30
    MAX_PATH_HISTORY = 10
    MAX_URL_LIST_SIZE = 500

    def __init__(self, base_url, max_visits, session_id, socketio, url_list=None):
        """
        Initialize the traffic generator.

        Args:
            base_url (str): The starting URL
            max_visits (int): Maximum number of visits to generate
            session_id (str): Unique session identifier
            socketio: SocketIO instance for real-time updates
            url_list (list): Optional list of pre-discovered URLs to visit
        """
        self.base_url = base_url
        self.base_domain = urllib.parse.urlparse(base_url).netloc
        self.max_visits = min(max_visits, self.MAX_VISITS_CAP)
        self.visits_completed = 0
        self.urls_visited = []
        self.session_id = session_id
        self.socketio = socketio
        self._active = False
        self._lock = threading.RLock()  # Use RLock for reentrant locking

        # Pre-discovered URLs list (limited size)
        self.url_list = url_list[: self.MAX_URL_LIST_SIZE] if url_list else []
        self.url_weights = {}

        # Browsing pattern tracking
        self.current_path = []
        self.visit_history = Counter()

        # Set up instance-specific logger
        self.logger = logging.getLogger(f"{__name__}.{session_id}")

        # Create requests session for maintaining cookies
        self.session = requests.Session()

        # Calculate URL weights if we have pre-discovered URLs
        if self.url_list:
            self._calculate_url_weights()

    def is_active(self):
        """Check if traffic generation is active (thread-safe)."""
        with self._lock:
            return self._active

    def stop(self):
        """Stop traffic generation (thread-safe)."""
        with self._lock:
            self._active = False
        self.logger.info(
            f"Traffic generation stopped after {self.visits_completed} visits"
        )

    def start(self):
        """Start traffic generation process."""
        with self._lock:
            if self._active:
                self.logger.warning("Traffic generation already active")
                return
            self._active = True

        self.logger.info(
            f"Starting traffic generation for {self.base_url} "
            f"with {self.max_visits} visits"
        )

        try:
            self._emit_start_event()
            self._run_traffic_generation()
        except Exception as e:
            self.logger.error(f"Unexpected error during traffic generation: {e}")
            self._emit_error_event(f"Unexpected error: {str(e)}")
        finally:
            with self._lock:
                self._active = False
            self._emit_complete_event()

    def _emit_start_event(self):
        """Emit the generation start event."""
        self.socketio.emit(
            "generation_start",
            {
                "session_id": self.session_id,
                "base_url": self.base_url,
                "max_visits": self.max_visits,
                "url_count": len(self.url_list),
            },
        )

    def _emit_error_event(self, message):
        """Emit an error event."""
        self.socketio.emit(
            "generation_error", {"session_id": self.session_id, "message": message}
        )

    def _emit_complete_event(self):
        """Emit the generation complete event."""
        self.logger.info(
            f"Traffic generation completed with {self.visits_completed} visits"
        )

        stats = self._calculate_statistics()

        self.socketio.emit(
            "generation_complete",
            {
                "session_id": self.session_id,
                "visits_completed": self.visits_completed,
                "urls_visited": self.urls_visited[-100:],  # Limit response size
                "statistics": stats,
            },
        )

    def _run_traffic_generation(self):
        """Main traffic generation loop."""
        # Visit the base URL first
        success = self._visit_url(self.base_url)

        if not success:
            self.logger.error(f"Failed to visit base URL: {self.base_url}")
            self._emit_error_event(
                "Failed to access the base URL. Please check the URL and try again."
            )
            return

        # Continue visiting random links until max visits reached or stopped
        consecutive_failures = 0
        max_consecutive_failures = 5

        while self.is_active() and self.visits_completed < self.max_visits:
            next_url = self._choose_next_url()

            if next_url:
                success = self._visit_url(next_url)

                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

                    # Try alternative URLs on failure
                    if self.url_list:
                        for _ in range(3):
                            alt_url = self._choose_next_url()
                            if alt_url and self._visit_url(alt_url):
                                consecutive_failures = 0
                                break
            else:
                self.logger.warning("No URLs available, returning to base URL")
                self._visit_url(self.base_url)

            # Check for too many consecutive failures
            if consecutive_failures >= max_consecutive_failures:
                self.logger.error(
                    f"Too many consecutive failures ({consecutive_failures}), stopping"
                )
                break

            # Random delay to mimic human behavior
            self._wait_between_requests()

    def _wait_between_requests(self):
        """Wait a random interval between requests."""
        delay = random.uniform(self.MIN_DELAY_SECONDS, self.MAX_DELAY_SECONDS)
        self.logger.info(f"Waiting {delay:.1f} seconds before next request")

        self.socketio.emit(
            "waiting_update",
            {
                "session_id": self.session_id,
                "wait_time": round(delay, 1),
                "next_action": "Selecting next URL",
            },
        )

        # Sleep in smaller intervals to allow for quick stop
        elapsed = 0
        while elapsed < delay and self.is_active():
            sleep_time = min(0.5, delay - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time

    def _calculate_statistics(self):
        """Calculate statistics about the traffic generation session."""
        if not self.urls_visited:
            return {}

        # Count visits per URL
        url_counts = Counter(visit["url"] for visit in self.urls_visited)
        most_visited = url_counts.most_common(5)

        # Calculate average time between visits
        avg_time_between = self._calculate_avg_time_between_visits()

        # Calculate status code distribution
        status_counts = Counter(visit["status_code"] for visit in self.urls_visited)

        return {
            "most_visited_urls": most_visited,
            "avg_time_between_visits": round(avg_time_between, 2),
            "status_code_distribution": {str(k): v for k, v in status_counts.items()},
            "unique_urls_visited": len(set(v["url"] for v in self.urls_visited)),
            "total_visits": len(self.urls_visited),
        }

    def _calculate_avg_time_between_visits(self):
        """Calculate average time between visits."""
        if len(self.urls_visited) <= 1:
            return 0

        try:
            timestamps = [
                datetime.strptime(visit["timestamp"], "%Y-%m-%d %H:%M:%S")
                for visit in self.urls_visited
            ]
            time_diffs = [
                (timestamps[i] - timestamps[i - 1]).total_seconds()
                for i in range(1, len(timestamps))
            ]
            return sum(time_diffs) / len(time_diffs) if time_diffs else 0
        except (ValueError, KeyError) as e:
            self.logger.warning(f"Error calculating time between visits: {e}")
            return 0

    def _calculate_url_weights(self):
        """Calculate weights for URL selection based on URL structure."""
        for url in self.url_list:
            parsed = urllib.parse.urlparse(url)
            path_parts = [p for p in parsed.path.split("/") if p]
            depth = len(path_parts)

            # Base weight inversely proportional to depth
            weight = max(1, 10 - depth)

            # Adjust weight based on URL features
            url_lower = url.lower()
            if "index" in url_lower or url.endswith("/"):
                weight += 3
            if "category" in url_lower or "tag" in url_lower:
                weight += 2
            if "product" in url_lower or "item" in url_lower:
                weight += 1

            self.url_weights[url] = weight

    def _choose_next_url(self):
        """
        Choose the next URL to visit based on various strategies.

        Returns:
            str: URL to visit next
        """
        strategies = ["weighted", "recency", "backtracking", "popular", "random"]
        weights = [0.4, 0.2, 0.2, 0.1, 0.1]

        strategy = random.choices(strategies, weights=weights)[0]
        self.logger.debug(f"Using {strategy} selection strategy")

        url = None

        if strategy == "weighted" and self.url_list and self.url_weights:
            url = self._choose_weighted_url()
        elif strategy == "recency" and self.urls_visited:
            url = self._choose_recent_url()
        elif strategy == "backtracking" and len(self.current_path) > 1:
            url = self._choose_backtrack_url()
        elif strategy == "popular" and self.url_list:
            url = self._choose_popular_url()

        # Fallback to random selection
        if not url:
            url = self._choose_random_url()

        return url or self.base_url

    def _choose_weighted_url(self):
        """Choose URL based on pre-calculated weights."""
        urls = list(self.url_weights.keys())
        weights = list(self.url_weights.values())
        return random.choices(urls, weights=weights)[0] if urls else None

    def _choose_recent_url(self):
        """Choose URL from recently visited pages."""
        recent_urls = [v["url"] for v in self.urls_visited[-5:]]
        potential_urls = []

        for url in recent_urls:
            links = self._extract_links(url)
            potential_urls.extend(links)

        return random.choice(potential_urls) if potential_urls else None

    def _choose_backtrack_url(self):
        """Choose URL by backtracking (simulating browser back button)."""
        self.logger.debug("Backtracking to previous page")
        self.current_path.pop()
        return self.current_path[-1] if self.current_path else None

    def _choose_popular_url(self):
        """Choose from popular pages (index pages, etc.)."""
        index_pages = [
            url for url in self.url_list if url.endswith("/") or "index" in url.lower()
        ]
        return random.choice(index_pages) if index_pages else None

    def _choose_random_url(self):
        """Choose a random URL."""
        if self.url_list:
            return random.choice(self.url_list)

        if self.urls_visited:
            last_url = self.urls_visited[-1]["url"]
            links = self._extract_links(last_url)
            if links:
                return random.choice(links)

        return None

    def _visit_url(self, url):
        """
        Visit a specific URL and record the visit.

        Args:
            url (str): URL to visit

        Returns:
            bool: Success status
        """
        try:
            headers = self._build_request_headers()

            self.logger.info(f"Visiting URL: {url}")

            self.socketio.emit(
                "visit_preparing",
                {"session_id": self.session_id, "url": url, "using_proxy": False},
            )

            response = self.session.get(
                url, headers=headers, timeout=self.REQUEST_TIMEOUT, allow_redirects=True
            )

            visit_data = self._record_visit(response)
            self._update_browsing_state(url, response)
            self._emit_visit_update(visit_data)

            return True

        except requests.exceptions.Timeout:
            self.logger.warning(f"Timeout visiting URL: {url}")
            self._emit_visit_error(url, "Request timed out")
            return False
        except requests.exceptions.ConnectionError as e:
            self.logger.warning(f"Connection error visiting URL {url}: {e}")
            self._emit_visit_error(url, "Connection error")
            return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request error visiting URL {url}: {e}")
            self._emit_visit_error(url, str(e))
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error visiting URL {url}: {e}")
            return False

    def _build_request_headers(self):
        """Build HTTP headers for the request."""
        referer = self.urls_visited[-1]["url"] if self.urls_visited else self.base_url

        return {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": referer,
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }

    def _record_visit(self, response):
        """Record a successful visit."""
        content_type = response.headers.get("Content-Type", "")
        page_title = None

        if "text/html" in content_type:
            page_title = self._extract_title(response.text)

        visit_data = {
            "url": response.url,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "status_code": response.status_code,
            "using_proxy": False,
            "content_type": content_type,
            "page_title": page_title,
        }

        self.urls_visited.append(visit_data)
        self.visits_completed += 1

        return visit_data

    def _update_browsing_state(self, url, response):
        """Update browsing state after a visit."""
        # Update path history
        self.current_path.append(url)
        if len(self.current_path) > self.MAX_PATH_HISTORY:
            self.current_path = self.current_path[-self.MAX_PATH_HISTORY :]

        # Update visit counter
        self.visit_history[url] += 1

        # Discover new links periodically
        if not self.url_list or (
            self.visits_completed % 10 == 0
            and len(self.url_list) < self.MAX_URL_LIST_SIZE
        ):
            self._discover_new_links(url)

    def _discover_new_links(self, url):
        """Discover and add new links from a URL."""
        new_links = self._extract_links(url)
        for link in new_links:
            if (
                link not in self.url_list
                and len(self.url_list) < self.MAX_URL_LIST_SIZE
            ):
                self.url_list.append(link)
                self.url_weights[link] = 5  # Default weight

    def _emit_visit_update(self, visit_data):
        """Emit visit update event."""
        self.socketio.emit(
            "visit_update",
            {
                "session_id": self.session_id,
                "visit": visit_data,
                "visits_completed": self.visits_completed,
                "max_visits": self.max_visits,
                "discovered_urls_count": len(self.url_list),
            },
        )

    def _emit_visit_error(self, url, error):
        """Emit visit error event."""
        self.socketio.emit(
            "visit_error", {"session_id": self.session_id, "url": url, "error": error}
        )

    def _extract_title(self, html_content):
        """Extract the page title from HTML content."""
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                return title_tag.string.strip()[:200]  # Limit title length
        except Exception as e:
            self.logger.debug(f"Error extracting title: {e}")
        return "No title"

    def _extract_links(self, url):
        """
        Extract links from a web page.

        Args:
            url (str): URL to extract links from

        Returns:
            list: List of valid URLs found on the page
        """
        try:
            headers = {"User-Agent": get_random_user_agent()}

            response = self.session.get(url, headers=headers, timeout=15)

            if response.status_code != 200:
                self.logger.debug(
                    f"Got status code {response.status_code} when extracting links from {url}"
                )
                return []

            soup = BeautifulSoup(response.text, "html.parser")
            links = self._parse_links_from_soup(soup, url)

            self.logger.debug(f"Found {len(links)} links on {url}")

            if links:
                self.socketio.emit(
                    "links_discovered",
                    {
                        "session_id": self.session_id,
                        "source_url": url,
                        "link_count": len(links),
                    },
                )

            return links

        except requests.exceptions.RequestException as e:
            self.logger.debug(f"Request error extracting links from {url}: {e}")
            return []
        except Exception as e:
            self.logger.debug(f"Error extracting links from {url}: {e}")
            return []

    def _parse_links_from_soup(self, soup, base_url):
        """Parse links from BeautifulSoup object."""
        links = []
        excluded_extensions = (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".pdf",
            ".zip",
            ".rar",
            ".exe",
            ".dmg",
            ".mp3",
            ".mp4",
            ".avi",
            ".mov",
            ".css",
            ".js",
            ".svg",
            ".ico",
            ".webp",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
        )

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]

            if not href or href == "#" or href.startswith("javascript:"):
                continue

            absolute_url = urllib.parse.urljoin(base_url, href)
            parsed_url = urllib.parse.urlparse(absolute_url)

            # Only keep links to the same domain
            if parsed_url.netloc != self.base_domain:
                continue

            # Filter out non-webpage links
            if absolute_url.lower().endswith(excluded_extensions):
                continue

            # Remove fragments
            clean_url = absolute_url.split("#")[0]
            if clean_url and clean_url not in links:
                links.append(clean_url)

        return links[:100]  # Limit number of links extracted
