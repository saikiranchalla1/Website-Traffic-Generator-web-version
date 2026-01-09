import getpass
import logging
import os
import threading
import time
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO

from traffic_generator import TrafficGenerator
from url_analyzer import discover_urls
from url_validator import validate_url

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Use environment variable for secret key, with secure fallback
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

# Security configurations
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

socketio = SocketIO(app, cors_allowed_origins="*")

# Active traffic generation sessions with thread-safe access
active_sessions = {}
sessions_lock = threading.Lock()

# Rate limiting: track requests per IP
request_counts = {}
rate_limit_lock = threading.Lock()
RATE_LIMIT_MAX_REQUESTS = 100
RATE_LIMIT_WINDOW_SECONDS = 60


def get_current_user():
    """Get the current system user"""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def get_current_time():
    """Get the current UTC time formatted"""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def rate_limit(f):
    """Rate limiting decorator to prevent abuse"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        current_time = time.time()

        with rate_limit_lock:
            # Clean up old entries
            cutoff_time = current_time - RATE_LIMIT_WINDOW_SECONDS
            request_counts[client_ip] = [
                t for t in request_counts.get(client_ip, []) if t > cutoff_time
            ]

            # Check rate limit
            if len(request_counts.get(client_ip, [])) >= RATE_LIMIT_MAX_REQUESTS:
                logger.warning(f"Rate limit exceeded for IP: {client_ip}")
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Rate limit exceeded. Please try again later.",
                        }
                    ),
                    429,
                )

            # Record this request
            if client_ip not in request_counts:
                request_counts[client_ip] = []
            request_counts[client_ip].append(current_time)

        return f(*args, **kwargs)

    return decorated_function


def cleanup_completed_sessions():
    """Clean up completed sessions to prevent memory leaks"""
    with sessions_lock:
        completed_sessions = [
            session_id
            for session_id, session_data in active_sessions.items()
            if not session_data["generator"].is_active()
        ]
        for session_id in completed_sessions:
            logger.info(f"Cleaning up completed session: {session_id}")
            del active_sessions[session_id]


def validate_url_input(url):
    """Validate URL input for security"""
    if not url or not isinstance(url, str):
        return False, "URL is required"

    url = url.strip()
    if len(url) > 2048:
        return False, "URL is too long"

    if not url.startswith(("http://", "https://")):
        return False, "URL must start with http:// or https://"

    return True, url


@app.route("/")
def index():
    """Main page with disclaimer and input form"""
    if not session.get("disclaimer_accepted", False):
        return redirect(url_for("disclaimer"))
    return render_template(
        "index.html", current_user=get_current_user(), current_time=get_current_time()
    )


@app.route("/disclaimer")
def disclaimer():
    """Disclaimer page that users must accept before using the tool"""
    return render_template(
        "disclaimer.html",
        current_user=get_current_user(),
        current_time=get_current_time(),
    )


@app.route("/accept-disclaimer", methods=["POST"])
def accept_disclaimer():
    """Mark the disclaimer as accepted"""
    session["disclaimer_accepted"] = True
    return redirect(url_for("index"))


@app.route("/validate-url", methods=["POST"])
@rate_limit
def validate_url_endpoint():
    """Endpoint to validate a URL before starting traffic generation"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"valid": False, "message": "Invalid request data"}), 400

        url = data.get("url", "")

        # Validate URL input
        is_valid_input, result = validate_url_input(url)
        if not is_valid_input:
            return jsonify({"valid": False, "message": result})

        url = result  # Use cleaned URL

        # Validate the URL is accessible
        is_valid, message = validate_url(url)
        return jsonify({"valid": is_valid, "message": message})

    except Exception as e:
        logger.error(f"Error validating URL: {str(e)}")
        return (
            jsonify({"valid": False, "message": "An error occurred during validation"}),
            500,
        )


@app.route("/discover-urls", methods=["POST"])
@rate_limit
def discover_urls_endpoint():
    """Endpoint to discover URLs on a website"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "Invalid request data"}), 400

        url = data.get("url", "")

        # Validate URL input
        is_valid_input, result = validate_url_input(url)
        if not is_valid_input:
            return jsonify({"success": False, "message": result})

        url = result  # Use cleaned URL

        # Discover URLs (limit to 100 for performance)
        urls = discover_urls(url, max_depth=1, max_urls=100)

        return jsonify(
            {
                "success": True,
                "url_count": len(urls),
                "urls": urls[:100],
            }
        )

    except Exception as e:
        logger.error(f"Error discovering URLs: {str(e)}")
        return (
            jsonify(
                {
                    "success": False,
                    "message": "An error occurred while discovering URLs",
                }
            ),
            500,
        )


@app.route("/dashboard")
def dashboard():
    """Dashboard page for visualizing traffic generation"""
    url = request.args.get("url", "")
    visit_count = request.args.get("visits", 0, type=int)

    # Validate input
    is_valid_input, result = validate_url_input(url)
    if not is_valid_input or visit_count <= 0 or visit_count > 500:
        return redirect(url_for("index"))

    url = result  # Use cleaned URL

    return render_template(
        "dashboard.html",
        url=url,
        max_visits=visit_count,
        start_time=get_current_time(),
        current_user=get_current_user(),
        current_time=get_current_time(),
    )


@app.route("/start-traffic", methods=["POST"])
@rate_limit
def start_traffic():
    """Start traffic generation process"""
    try:
        # Clean up completed sessions first
        cleanup_completed_sessions()

        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "Invalid request data"}), 400

        url = data.get("url", "")
        visit_count = data.get("visits", 0)
        url_list = data.get("url_list", [])

        # Validate URL input
        is_valid_input, result = validate_url_input(url)
        if not is_valid_input:
            return jsonify({"success": False, "message": result})

        url = result  # Use cleaned URL

        # Validate visit count
        try:
            visit_count = int(visit_count)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Invalid visit count"})

        if visit_count <= 0 or visit_count > 500:
            return jsonify(
                {"success": False, "message": "Visit count must be between 1 and 500"}
            )

        # Validate url_list
        if not isinstance(url_list, list):
            url_list = []

        # Sanitize url_list
        url_list = [
            u
            for u in url_list
            if isinstance(u, str) and u.startswith(("http://", "https://"))
        ][
            :200
        ]  # Limit to 200 URLs

        # Generate unique session ID
        session_id = f"{int(time.time())}-{os.urandom(4).hex()}"

        # Create traffic generator with discovered URL list if available
        generator = TrafficGenerator(url, visit_count, session_id, socketio, url_list)

        # Start generation in a separate thread
        thread = threading.Thread(target=generator.start, daemon=True)
        thread.start()

        # Store active session with thread-safe access
        with sessions_lock:
            active_sessions[session_id] = {
                "generator": generator,
                "thread": thread,
                "url": url,
                "max_visits": visit_count,
                "started_at": get_current_time(),
            }

        logger.info(f"Started traffic generation session: {session_id}")
        return jsonify({"success": True, "session_id": session_id})

    except Exception as e:
        logger.error(f"Error starting traffic generation: {str(e)}")
        return (
            jsonify(
                {
                    "success": False,
                    "message": "An error occurred while starting traffic generation",
                }
            ),
            500,
        )


@app.route("/stop-traffic", methods=["POST"])
@rate_limit
def stop_traffic():
    """Stop an active traffic generation session"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "Invalid request data"}), 400

        session_id = data.get("session_id", "")

        if not session_id or not isinstance(session_id, str):
            return jsonify({"success": False, "message": "Invalid session ID"})

        with sessions_lock:
            if session_id in active_sessions:
                active_sessions[session_id]["generator"].stop()
                del active_sessions[session_id]
                logger.info(f"Stopped traffic generation session: {session_id}")
                return jsonify({"success": True})

        return jsonify({"success": False, "message": "Session not found"})

    except Exception as e:
        logger.error(f"Error stopping traffic generation: {str(e)}")
        return (
            jsonify(
                {
                    "success": False,
                    "message": "An error occurred while stopping traffic generation",
                }
            ),
            500,
        )


@app.route("/session-status/<session_id>")
def session_status(session_id):
    """Get the status of a traffic generation session"""
    try:
        # Validate session_id
        if not session_id or not isinstance(session_id, str) or len(session_id) > 50:
            return jsonify({"active": False, "message": "Invalid session ID"})

        with sessions_lock:
            if session_id in active_sessions:
                generator = active_sessions[session_id]["generator"]
                return jsonify(
                    {
                        "active": generator.is_active(),
                        "visits_completed": generator.visits_completed,
                        "max_visits": generator.max_visits,
                        "urls_visited": generator.urls_visited[
                            -50:
                        ],  # Limit response size
                    }
                )

        return jsonify({"active": False, "message": "Session not found"})

    except Exception as e:
        logger.error(f"Error getting session status: {str(e)}")
        return jsonify({"active": False, "message": "An error occurred"})


@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    logger.error(f"Internal server error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500


@socketio.on("connect")
def handle_connect():
    """Handle client connection to WebSocket"""
    logger.debug(f"Client connected: {request.sid}")


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection from WebSocket"""
    logger.debug(f"Client disconnected: {request.sid}")


if __name__ == "__main__":
    # Get configuration from environment variables
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5050))

    logger.info(f"Starting server on {host}:{port}")

    # Port 5050 is used instead of 5000 to avoid conflict with macOS AirPlay Receiver
    socketio.run(app, debug=debug_mode, host=host, port=port)
