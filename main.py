from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import os

app = Flask(__name__)

# Load environment variables
slack_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
channel_id = os.environ.get("CHANNEL_ID")
client = WebClient(token=slack_token)

# Zendesk email → Slack user ID map
ASSIGNEE_MAP = {
    "daniel.molina@finally.com": "U06RX9U53AL",
    "julio.matta@finally.com": "U06PUTV0C64",
    "nelson.perez@finally.com": "U06QTUZ4DN3",
    "jean.dejesus@finally.com": "U078HJLK6QL",
    "leila.ghazzaoui@finally.com": "U0788V1V65U",
    "samuel.aguirre@finally.com": "U078FEXLW5R",
    "jose.perez@finally.com": "U07FRMSKEMN",
    "frances.rivera@finally.com": "U08BUM31GS3",
    "angelica.calderon@finally.com": "U07HHE1N54J"
}

# Active reminders
tickets = {}

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.get_json()

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if event.get("type") == "reaction_added":
            ts = event.get("item", {}).get("ts")
            reaction = event.get("reaction")
            print(f"Reaction '{reaction}' added to message {ts}")

            if reaction == "white_check_mark":
                for ticket_id, info in list(tickets.items()):
                    if info["ts"] == ts:
                        print(f"✅ Ticket {ticket_id} resolved via emoji. Stopping reminders.")
                        del tickets[ticket_id]

    return "", 200

@app.route("/new_ticket", methods=["POST"])
def new_ticket():
    try:
        data = request.get_json()
        print("📥 Incoming new ticket payload:", data)

        ticket_id = data.get("ticket_id")
        assignee_email = data.get("assignee_email")
        message_ts = data.get("message_ts")

        if not all([ticket_id, assignee_email, message_ts]):
            print("[!] Missing required fields")
            return jsonify({"error": "Missing required fields"}), 400

        slack_id = ASSIGNEE_MAP.get(assignee_email.lower())
        if not slack_id:
            print(f"[!] No Slack ID found for: {assignee_email}")
            return jso
