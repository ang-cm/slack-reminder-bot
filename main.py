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

# 🎯 Your hardcoded Zendesk email → Slack user ID map
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


# Store active ticket reminders
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
                        print(f"✅ Ticket {ticket_id} resolved. Stopping reminders.")
                        del tickets[ticket_id]

    return "", 200

@app.route("/new_ticket", methods=["POST"])
def new_ticket():
    data = request.json
    ticket_id = data.get("ticket_id")
    assignee_email = data.get("assignee_email")
    message_ts = data.get("message_ts")

    if not (ticket_id and assignee_email and message_ts):
        return {"error": "Missing required fields"}, 400

    assignee_slack_id = ASSIGNEE_MAP.get(assignee_email.lower())
    if not assignee_slack_id:
        return {"error": f"No Slack ID mapped for {assignee_email}"}, 400

    tickets[ticket_id] = {
        "ts": message_ts,
        "assignee_slack_id": assignee_slack_id,
        "last_reminder": datetime.now()
    }

    print(f"[+] Tracking ticket {ticket_id} for {assignee_email} (<@{assignee_slack_id}>)")
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
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"<@{info['assignee_slack_id']}> Reminder: please follow up on ticket {ticket_id}"
                    )
                    tickets[ticket_id]['last_reminder'] = now

        except SlackApiError as e:
            print(f"[!] Slack API error on ticket {ticket_id}: {e.response['error']}")

# Start the reminder scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_reminders, trigger="interval", minutes=10)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
