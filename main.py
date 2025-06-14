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

# Track ticket reminders
tickets = {}

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.get_json()

    # ✅ Handle Slack URL verification challenge
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # 🎯 Handle reactions
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if event.get("type") == "reaction_added":
            ts = event.get("item", {}).get("ts")
            reaction = event.get("reaction")

            print(f"Reaction '{reaction}' added to message {ts}")

            if reaction == "white_check_mark":
                for ticket_id, info in list(tickets.items()):
                    if info["ts"] == ts:
                        print(f"✅ Ticket {ticket_id} resolved. Stopping reminders.")
                        del tickets[ticket_id]

    return "", 200

@app.route("/new_ticket", methods=["POST"])
def new_ticket():
    data = request.json
    ticket_id = data.get("ticket_id")
    assignee_slack_id = data.get("assignee_slack_id")
    message_ts = data.get("message_ts")

    if not (ticket_id and assignee_slack_id and message_ts):
        return {"error": "Missing required fields"}, 400

    tickets[ticket_id] = {
        "ts": message_ts,
        "assignee_slack_id": assignee_slack_id,
        "last_reminder": datetime.now()
    }

    print(f"[+] Tracking ticket {ticket_id} for <@{assignee_slack_id}>")
    return {"status": "ok"}, 200

def check_reminders():
    for ticket_id, info in list(tickets.items()):
        try:
            res = client.reactions_get(channel=channel_id, timestamp=info['ts'])
            reactions = res['message'].get('reactions', [])
            if any(r['name'] == 'white_check_mark' for r in reactions):
                print(f"✅ Ticket {ticket_id} marked complete by emoji.")
                del tickets[ticket_id]
            else:
                now = datetime.now()
                if now - info['last_reminder'] >= timedelta(hours=4):
