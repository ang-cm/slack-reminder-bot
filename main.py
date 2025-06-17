from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import os
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from functools import wraps

app = Flask(__name__)

# Configure logging
def setup_logging():
    """Configure logging for the application"""
    logger = logging.getLogger()
    
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    
    # Log format
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        'reminder_bot.log',
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logging()

# Load environment variables
slack_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
webhook_secret_token = os.environ.get("WEBHOOK_SECRET_TOKEN")

# Updated channel configuration to support both SOS and escalations channels
SLACK_CHANNELS = {
    "sos": os.environ.get("CHANNEL_ID"),  # Your existing SOS channel
    "escalations": os.environ.get("SLACK_CHANNEL_ID_ESCALATIONS")  # New escalations channel
}

# Validate channel configuration
if not SLACK_CHANNELS.get("sos"):
    logger.error("CHANNEL_ID environment variable not set!")
if not SLACK_CHANNELS.get("escalations"):
    logger.warning("SLACK_CHANNEL_ID_ESCALATIONS environment variable not set, escalation tickets will go to the SOS channel")

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

# Token authentication decorator
def require_token(f):
    """Decorator to require token authentication for webhook endpoints"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not webhook_secret_token:
            logger.warning("WEBHOOK_SECRET_TOKEN not configured! Webhook endpoints are unsecured.")
            return f(*args, **kwargs)
        
        # Check for token in header
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning(f"Unauthorized webhook access attempt from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401
        
        token = auth_header.split(" ")[1]
        if token != webhook_secret_token:
            logger.warning(f"Invalid token provided from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401
        
        return f(*args, **kwargs)
    return decorated_function

# Persistence functions
def save_tickets():
    """Save tickets to a JSON file"""
    serializable_tickets = {}
    
    for ticket_id, info in tickets.items():
        # Convert datetime objects to strings
        serialized_info = info.copy()
        serialized_info['last_reminder'] = info['last_reminder'].isoformat()
        serialized_info['created_at'] = info.get('created_at', datetime.now()).isoformat()
        serializable_tickets[ticket_id] = serialized_info
    
    with open('tickets.json', 'w') as f:
        json.dump(serializable_tickets, f)
    
    logger.info(f"💾 Saved {len(tickets)} tickets to disk")

def load_tickets():
    """Load tickets from a JSON file"""
    try:
        with open('tickets.json', 'r') as f:
            serialized_tickets = json.load(f)
        
        loaded_tickets = {}
        for ticket_id, info in serialized_tickets.items():
            # Convert string dates back to datetime objects
            deserialized_info = info.copy()
            deserialized_info['last_reminder'] = datetime.fromisoformat(info['last_reminder'])
            deserialized_info['created_at'] = datetime.fromisoformat(info['created_at'])
            loaded_tickets[ticket_id] = deserialized_info
        
        logger.info(f"📂 Loaded {len(loaded_tickets)} tickets from disk")
        return loaded_tickets
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.info(f"No previous tickets file found or file corrupted: {e}. Starting fresh.")
        return {}

# Load tickets on startup
try:
    tickets = load_tickets()
except Exception as e:
    logger.error(f"Error loading tickets: {e}")
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
            channel = event.get("item", {}).get("channel")
            reaction = event.get("reaction")
            logger.info(f"Reaction '{reaction}' added to message {ts} in channel {channel}")

            if reaction == "white_check_mark":
                for ticket_id, info in list(tickets.items()):
                    if info["ts"] == ts and info.get("channel_id", SLACK_CHANNELS.get("sos")) == channel:
                        logger.info(f"✅ Ticket {ticket_id} resolved via emoji in channel {channel}. Stopping reminders.")
                        del tickets[ticket_id]
                        save_tickets()

    return "", 200

@app.route("/new_ticket", methods=["POST"])
@require_token
def new_ticket():
    try:
        data = request.get_json()
        logger.info(f"📥 Incoming new ticket payload: {data}")

        ticket_id = data.get("ticket_id")
        assignee_email = data.get("assignee_email")
        message_ts = data.get("message_ts")
        ticket_url = data.get("ticket_url")
        
        # Get channel information from payload
        channel_id = data.get("channel_id")
        channel_type = data.get("channel_type", "sos")
        is_escalation = data.get("is_escalation", False)
        
        # If no channel ID was provided, determine which one to use
        if not channel_id:
            # Choose based on ticket type
            channel_id = SLACK_CHANNELS.get(
                "escalations" if is_escalation or channel_type == "escalations" else "sos", 
                SLACK_CHANNELS.get("sos")
            )
            
            if not channel_id:
                logger.error("No channel ID provided or configured")
                return jsonify({"error": "No channel ID available"}), 400

        logger.info(f"🧾 ticket_id: {ticket_id}")
        logger.info(f"👤 assignee_email: {assignee_email}")
        logger.info(f"⏱ message_ts: {message_ts}")
        logger.info(f"📢 channel_id: {channel_id} (type: {channel_type})")

        if not all([ticket_id, assignee_email, message_ts]):
            missing = []
            if not ticket_id:
                missing.append("ticket_id")
            if not assignee_email:
                missing.append("assignee_email")
            if not message_ts:
                missing.append("message_ts")
            logger.error(f"Missing required fields: {', '.join(missing)}")
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        # Check channel ID
        if not channel_id:
            logger.error("No channel ID provided and default channel not configured!")
            return jsonify({"error": "No channel ID available"}), 500

        slack_id = ASSIGNEE_MAP.get(assignee_email.lower())
        if not slack_id:
            logger.error(f"No Slack ID found for: {assignee_email}")
            return jsonify({"error": f"Unknown assignee: {assignee_email}"}), 400

        # FIXED: More robust message verification with better error handling
        try:
            # Convert timestamp to float if it's a string
            ts_float = float(message_ts)
            
            # Verify the message exists in Slack with a slightly wider window
            # to account for potential timestamp precision issues
            result = client.conversations_history(
                channel=channel_id,
                latest=str(ts_float + 1),  # Add 1 second buffer
                oldest=str(ts_float - 1),  # Subtract 1 second buffer
                limit=5  # Get a few messages around this timestamp
            )
            
            # Log the messages we found for debugging
            logger.info(f"Found {len(result.get('messages', []))} messages near timestamp {message_ts}")
            
            # Check if we found at least one message
            if not result.get("messages"):
                logger.warning(f"No messages found near ts={message_ts} in channel {channel_id}")
                
                # Instead of rejecting, we'll create a placeholder/fallback message
                logger.info(f"Creating fallback placeholder message for ticket {ticket_id}")
                
                # Add escalation indicator if needed
                escalation_tag = "🔴 **ESCALATION** " if channel_type == "escalations" or is_escalation else ""
                
                # FIXED: Explicitly pass the channel_id
                placeholder_result = client.chat_postMessage(
                    channel=channel_id,  # Make sure channel is explicitly set
                    text=f"⚠️ {escalation_tag}*Tracking Zendesk Ticket #{ticket_id}*\n\n"
                         f"This is a fallback message created to track ticket #{ticket_id} assigned to {assignee_email}.\n\n"
                         f"To stop reminders, add a :white_check_mark: reaction to this message or solve the ticket in Zendesk."
                )
                
                # Use the new message timestamp instead
                message_ts = placeholder_result["ts"]
                logger.info(f"Created fallback message with ts: {message_ts}")
        except SlackApiError as e:
            logger.error(f"Slack API error: {e.response.get('error', str(e))}")
            
            # Rather than failing, we'll create a fallback message
            logger.info(f"Creating fallback message after API error for ticket {ticket_id}")
            try:
                # FIXED: Explicitly check and pass channel_id
                if not channel_id:
                    logger.error("Cannot create fallback message: channel_id not set")
                    return jsonify({"error": "Channel ID not configured"}), 500
                    
                # Add escalation indicator if needed
                escalation_tag = "🔴 **ESCALATION** " if channel_type == "escalations" or is_escalation else ""
                
                # FIXED: Explicitly pass the channel_id
                placeholder_result = client.chat_postMessage(
                    channel=channel_id,  # Ensure this is correct and not empty
                    text=f"⚠️ {escalation_tag}*Tracking Zendesk Ticket #{ticket_id}*\n\n"
                         f"This is a fallback message created to track ticket #{ticket_id} assigned to {assignee_email}.\n\n"
                         f"To stop reminders, add a :white_check_mark: reaction to this message or solve the ticket in Zendesk."
                )
                message_ts = placeholder_result["ts"]
                logger.info(f"Created fallback message with ts: {message_ts}")
            except SlackApiError as inner_e:
                logger.error(f"Failed to create fallback message: {inner_e}")
                # Provide better error details
                error_details = inner_e.response.get('error', str(inner_e))
                metadata = inner_e.response.get('response_metadata', {})
                if metadata and 'messages' in metadata:
                    error_details += f" - {', '.join(metadata['messages'])}"
                logger.error(f"Error details: {error_details}")
                return jsonify({"error": f"Failed to create or verify message: {error_details}"}), 500
        except ValueError as e:
            # Handle case where message_ts is not a valid float
            logger.error(f"Invalid timestamp format: {message_ts}, error: {e}")
            
            # Create a fallback message
            try:
                # Add escalation indicator if needed
                escalation_tag = "🔴 **ESCALATION** " if channel_type == "escalations" or is_escalation else ""
                
                # FIXED: Explicitly pass the channel_id
                placeholder_result = client.chat_postMessage(
                    channel=channel_id,  # Ensure this is correct
                    text=f"⚠️ {escalation_tag}*Tracking Zendesk Ticket #{ticket_id}*\n\n"
                         f"This is a fallback message created to track ticket #{ticket_id} assigned to {assignee_email}.\n\n"
                         f"To stop reminders, add a :white_check_mark: reaction to this message or solve the ticket in Zendesk."
                )
                message_ts = placeholder_result["ts"]
                logger.info(f"Created fallback message with ts: {message_ts}")
            except SlackApiError as inner_e:
                logger.error(f"Failed to create fallback message: {inner_e}")
                error_details = inner_e.response.get('error', str(inner_e))
                return jsonify({"error": f"Failed to create fallback message: {error_details}"}), 500

        # Add to tracked tickets
        tickets[ticket_id] = {
            "ts": message_ts,
            "assignee_slack_id": slack_id,
            "assignee_email": assignee_email,
            "last_reminder": datetime.now(),
            "created_at": datetime.now(),
            "reminder_count": 0,
            "ticket_url": ticket_url or f"https://finally.zendesk.com/agent/tickets/{ticket_id}",
            "status": "open",
            "channel_id": channel_id,               # Store channel_id
            "channel_type": channel_type,           # Store channel_type
            "is_escalation": is_escalation          # Store escalation status
        }
        
        save_tickets()

        logger.info(f"[+] Reminder scheduled for ticket {ticket_id} to <@{slack_id}> in channel {channel_id}")
        return jsonify({
            "status": "ok",
            "channel_id": channel_id,
            "channel_type": channel_type
        }), 200

    except Exception as e:
        logger.error(f"Exception in /new_ticket: {e}", exc_info=True)
        return jsonify({"error": f"server error: {str(e)}"}), 500

@app.route("/complete_ticket", methods=["POST"])
@require_token
def complete_ticket():
    """API endpoint to mark a ticket as complete"""
    try:
        data = request.get_json()
        logger.info(f"Received completion request: {data}")
        
        ticket_id = data.get("ticket_id")
        if not ticket_id:
            logger.error("Missing ticket_id in completion request")
            return jsonify({"error": "Missing ticket_id"}), 400
            
        # Check if this ticket is being tracked
        if ticket_id not in tickets:
            logger.warning(f"Completion request for unknown ticket: {ticket_id}")
            return jsonify({"status": "not_found"}), 404
            
        # Remove the ticket from tracking
        logger.info(f"✅ Ticket {ticket_id} marked complete via API. Stopping reminders.")
        del tickets[ticket_id]
        save_tickets()
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Exception in /complete_ticket: {e}")
        return jsonify({"error": "server error"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for monitoring"""
    status = {
        "status": "healthy",
        "time": datetime.now().isoformat(),
        "active_tickets": len(tickets),
        "scheduler_running": scheduler.running,
        "environment": {
            "sos_channel_configured": bool(SLACK_CHANNELS.get("sos")),
            "escalations_channel_configured": bool(SLACK_CHANNELS.get("escalations")),
            "slack_token_configured": bool(slack_token),
            "webhook_token_configured": bool(webhook_secret_token)
        }
    }
    
    # Test Slack API connection
    try:
        client.auth_test()
        status["slack_connection"] = "ok"
    except SlackApiError as e:
        status["slack_connection"] = "error"
        status["slack_error"] = str(e)
        status["status"] = "degraded"
    
    # Return appropriate status code
    status_code = 200 if status["status"] == "healthy" else 503
    return jsonify(status), status_code

@app.route("/", methods=["GET"])
def home():
    return "Slack Reminder Bot is running ✅", 200

@app.route("/tickets", methods=["GET"])
def list_tickets():
    """Admin endpoint to list all tracked tickets"""
    try:
        # Check for an API key for minimal security
        api_key = request.args.get("api_key")
        expected_key = os.environ.get("ADMIN_API_KEY")
        
        if not expected_key or api_key != expected_key:
            return jsonify({"error": "Unauthorized"}), 401
            
        # Get ticket summary
        ticket_summary = []
        for ticket_id, info in tickets.items():
            # Create a simplified view
            ticket_summary.append({
                "ticket_id": ticket_id,
                "assignee": info.get("assignee_email"),
                "channel_type": info.get("channel_type", "sos"),
                "is_escalation": info.get("is_escalation", False),
                "reminder_count": info.get("reminder_count", 0),
                "last_reminder": info.get("last_reminder").isoformat(),
                "created_at": info.get("created_at").isoformat()
            })
            
        return jsonify({
            "total_tickets": len(tickets),
            "tickets": ticket_summary
        })
        
    except Exception as e:
        logger.error(f"Error listing tickets: {e}")
        return jsonify({"error": "Internal server error"}), 500

def check_reminders():
    """Check for tickets that need reminders"""
    logger.info(f"Checking reminders for {len(tickets)} active tickets")
    for ticket_id, info in list(tickets.items()):
        try:
            # Get channel ID from ticket info
            channel_id = info.get('channel_id')
            if not channel_id:
                logger.error(f"Missing channel_id for ticket {ticket_id}, using default")
                channel_id = SLACK_CHANNELS.get("sos")
                if not channel_id:
                    logger.error(f"No default channel configured, skipping reminder for ticket {ticket_id}")
                    continue
            
            # Check if the ticket has been marked complete with a reaction
            res = client.reactions_get(channel=channel_id, timestamp=info['ts'])
            reactions = res['message'].get('reactions', [])
            if any(r['name'] == 'white_check_mark' for r in reactions):
                logger.info(f"✅ Ticket {ticket_id} marked complete via reaction. Removing from reminders.")
                del tickets[ticket_id]
                save_tickets()
            else:
                now = datetime.now()
                # Determine reminder frequency based on ticket type
                reminder_hours = 2 if info.get("is_escalation", False) else 4
                
                if now - info['last_reminder'] >= timedelta(hours=reminder_hours):
                    # Add escalation indicator if needed
                    escalation_tag = "🔴 **ESCALATION** " if info.get("is_escalation", False) else ""
                    
                    # Send reminder
                    client.chat_postMessage(
                        channel=channel_id,
                        text=f"{escalation_tag}<@{info['assignee_slack_id']}> Reminder: please follow up on ticket {ticket_id}"
                    )
                    
                    tickets[ticket_id]['last_reminder'] = now
                    tickets[ticket_id]['reminder_count'] = info.get('reminder_count', 0) + 1
                    save_tickets()
                    
                    logger.info(f"🔁 Reminder sent for ticket {ticket_id} (#{tickets[ticket_id]['reminder_count']}) in channel {channel_id}")
                    
                    # Escalate after 3 reminders
                    if tickets[ticket_id]['reminder_count'] >= 3:
                        client.chat_postMessage(
                            channel=channel_id,
                            text=f"<!here> {escalation_tag}Ticket {ticket_id} has been waiting for response for over {reminder_hours * 3} hours"
                        )
                        logger.info(f"⚠️ Escalated ticket {ticket_id} after {tickets[ticket_id]['reminder_count']} reminders")

        except SlackApiError as e:
            logger.error(f"Slack API error for ticket {ticket_id}: {e.response['error']}")

# Scheduler setup
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_reminders, trigger="interval", minutes=10)
scheduler.start()

if __name__ == "__main__":
    # Check connections on startup
    try:
        # Validate critical configuration
        if not SLACK_CHANNELS.get("sos"):
            logger.critical("CHANNEL_ID environment variable is not set! The bot will not function correctly.")
        else:
            logger.info(f"Using SOS Slack channel: {SLACK_CHANNELS.get('sos')}")
            
        if SLACK_CHANNELS.get("escalations"):
            logger.info(f"Using escalations Slack channel: {SLACK_CHANNELS.get('escalations')}")
        else:
            logger.warning("SLACK_CHANNEL_ID_ESCALATIONS not set, will use SOS channel for escalation tickets")
            
        if not slack_token:
            logger.critical("SLACK_BOT_TOKEN environment variable is not set! The bot will not function correctly.")
        
        # Test Slack connection
        auth_test = client.auth_test()
        logger.info(f"Connected to Slack as {auth_test['user']} in team {auth_test['team']}")
        
        # Verify channel access
        for channel_type, channel_id in SLACK_CHANNELS.items():
            if channel_id:
                try:
                    channel_info = client.conversations_info(channel=channel_id)
                    logger.info(f"Connected to Slack {channel_type} channel: {channel_info['channel'].get('name', channel_id)}")
                except SlackApiError as e:
                    logger.critical(f"Cannot access the {channel_type} channel {channel_id}: {e.response['error']}")
    except SlackApiError as e:
        logger.error(f"Failed to connect to Slack: {e}")
    
    # Start the Flask app
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Slack Reminder Bot on port {port}")
    app.run(host="0.0.0.0", port=port)
