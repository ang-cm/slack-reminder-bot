from flask import Flask, request, jsonify
import os
import json
import traceback
import requests
import time
import logging
import sys
from logging.handlers import RotatingFileHandler
from functools import wraps
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from urllib.parse import urljoin

app = Flask(__name__)

# Configure logging
def setup_logging():
    """Configure logging for the application"""
    logger = logging.getLogger()
    
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, log_level, logging.INFO))

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        'zendesk_listener.log',
        maxBytes=10 * 1024 * 1024,
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

logger = setup_logging()

# ENV Vars
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")
REMINDER_BOT_URL = os.environ.get("REMINDER_BOT_URL")
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN")
ZENDESK_DOMAIN = os.environ.get("ZENDESK_DOMAIN", "finally.zendesk.com")

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Token authentication
def require_token(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
        if not WEBHOOK_SECRET_TOKEN:
            logger.warning("WEBHOOK_SECRET_TOKEN not configured! Webhook endpoints are unsecured.")
            return f(*args, **kwargs)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning(f"Unauthorized webhook access from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split(" ")[1]
        if token != WEBHOOK_SECRET_TOKEN:
            logger.warning(f"Invalid token provided from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated_function

@app.route("/")
def home():
    return "Zendesk listener is live!"

@app.route("/health", methods=["GET"])
def health_check():
    status = {
        "status": "healthy",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "environment": {
            "slack_token_configured": bool(SLACK_BOT_TOKEN),
            "slack_channel_configured": bool(SLACK_CHANNEL_ID),
            "reminder_bot_url_configured": bool(REMINDER_BOT_URL),
            "webhook_secret_configured": bool(WEBHOOK_SECRET_TOKEN)
        }
    }

    try:
        auth_test = slack_client.auth_test()
        status["slack_connection"] = {
            "status": "ok",
            "bot_name": auth_test.get("user"),
            "team": auth_test.get("team")
        }
    except SlackApiError as e:
        status["slack_connection"] = {
            "status": "error",
            "error": str(e)
        }
        status["status"] = "degraded"

    if REMINDER_BOT_URL:
        try:
            res = requests.get(urljoin(REMINDER_BOT_URL, "/health"), timeout=5)
            status["reminder_bot_connection"] = {
                "status": "ok" if res.status_code == 200 else "error",
                "status_code": res.status_code
            }
            if res.status_code != 200:
                status["status"] = "degraded"
        except requests.RequestException as e:
            status["reminder_bot_connection"] = {
                "status": "error",
                "error": str(e)
            }
            status["status"] = "degraded"
    else:
        status["reminder_bot_connection"] = {
            "status": "unconfigured"
        }
        status["status"] = "degraded"

    return jsonify(status), (200 if status["status"] == "healthy" else 503)

@app.route("/zendesk_hook", methods=["POST", "OPTIONS"])
@require_token
def zendesk_hook():
    logger.info("=== Incoming Webhook ===")
    logger.info(f"Request Method: {request.method}")
    logger.info(f"Headers: {dict(request.headers)}")

    if request.method == "OPTIONS":
        response = jsonify({"status": "OK (preflight)"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'POST,OPTIONS')
        return response, 200

    try:
        try:
            data = request.get_json(force=True)
            logger.info(f"Parsed JSON: {data}")
        except Exception as e:
            logger.error(f"JSON parse error: {e}")
            logger.error(f"Raw body: {request.data.decode('utf-8')}")
            return jsonify({"error": "Invalid JSON"}), 400

        ticket_id = data.get("ticket_id")
        assignee_email = data.get("assignee_email")

        if not ticket_id or not assignee_email:
            logger.error("Missing ticket_id or assignee_email")
            return jsonify({"error": "Missing fields"}), 400

        logger.info(f"✔️ Ticket ID: {ticket_id}")
        logger.info(f"✔️ Assignee Email: {assignee_email}")

        search_key = f"#{ticket_id}"
        now = int(time.time())

        try:
            response = slack_client.conversations_history(
                channel=SLACK_CHANNEL_ID,
                latest=str(now),
                oldest=str(now - 900),
                limit=50
            )
            messages = response.get("messages", [])
            if not any(search_key in message.get("text", "") for message in messages):
                logger.info("No match in 15 minutes, trying 60 minute range...")
                response = slack_client.conversations_history(
                    channel=SLACK_CHANNEL_ID,
                    latest=str(now),
                    oldest=str(now - 3600),
                    limit=100
                )
                messages = response.get("messages", [])
        except SlackApiError as err:
            logger.error(f"Slack API error: {err.response['error']}")
            return jsonify({"error": "Slack API failure"}), 500

        message_ts = None
        for message in messages:
            if search_key in message.get("text", ""):
                message_ts = message.get("ts")
                logger.info(f"[+] Found message_ts: {message_ts}")
                break

        if not message_ts:
            logger.warning(f"No Slack message found for ticket #{ticket_id}")
            return jsonify({"error": "Message not found"}), 404

        ticket_url = f"https://{ZENDESK_DOMAIN}/agent/tickets/{ticket_id}"
        payload = {
            "ticket_id": ticket_id,
            "assignee_email": assignee_email,
            "message_ts": message_ts,
            "ticket_url": ticket_url
        }

        headers = {}
        if WEBHOOK_SECRET_TOKEN:
            headers["Authorization"] = f"Bearer {WEBHOOK_SECRET_TOKEN}"

        logger.info(f"[→] Sending to reminder bot: {urljoin(REMINDER_BOT_URL, '/new_ticket')}")

        retry_count = 0
        while retry_count < 3:
            try:
                res = requests.post(
                    urljoin(REMINDER_BOT_URL, "/new_ticket"),
                    json=payload,
                    headers=headers,
                    timeout=10
                )
                logger.info(f"[✓] Reminder bot responded: {res.status_code} {res.text}")
                if res.status_code == 200:
                    return jsonify({"status": "Webhook processed"}), 200
                elif res.status_code >= 500:
                    retry_count += 1
                    logger.warning(f"Server error, retrying ({retry_count}/3)")
                    time.sleep(2)
                else:
                    return jsonify({"error": res.text}), res.status_code
            except requests.RequestException as e:
                retry_count += 1
                logger.error(f"Retry {retry_count} failed: {e}")
                time.sleep(2)

        logger.error("All retries failed to reach reminder bot")
        return jsonify({"error": "Failed after retries"}), 502

    except Exception as e:
        logger.error("Fatal error during webhook processing")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    try:
        auth_test = slack_client.auth_test()
        logger.info(f"Slack bot: {auth_test['user']} @ {auth_test['team']}")
        channel_info = slack_client.conversations_info(channel=SLACK_CHANNEL_ID)
        logger.info(f"Connected to Slack channel: {channel_info['channel']['name']}")
    except SlackApiError as e:
        logger.error(f"Slack setup error: {e}")

    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Running Zendesk Slack Listener on port {port}")
    app.run(host="0.0.0.0", port=port)
