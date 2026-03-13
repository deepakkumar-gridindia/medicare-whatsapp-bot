import os
import json
import requests
from flask import Flask, request, jsonify
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)

# ── Credentials (from .env) ───────────────────────────────
VERIFY_TOKEN   = os.getenv("VERIFY_TOKEN",   "medicarebot123")
ACCESS_TOKEN   = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY",   "")

client = Groq(api_key=GROQ_API_KEY)

# ── In-memory conversation store ─────────────────────────
# Stores conversation history per phone number
conversations = {}

# ── Patient data (simple lookup) ─────────────────────────
PATIENTS = {
    "default": {
        "name": "Patient",
        "age": "unknown",
        "language": "English",
        "drugs": []
    }
}

SYSTEM_PROMPT = """You are a warm, caring pharmacy assistant from MediCare Pharmacy.
You are following up with a patient via WhatsApp.

Your goals:
1. Greet the patient warmly
2. Ask if they are taking medications as prescribed
3. Check for any side effects or discomfort
4. Remind about upcoming refills if needed
5. Ask briefly about general health
6. Once all goals covered, close warmly

Rules:
- Keep responses SHORT — max 2-3 sentences (this is WhatsApp, not email!)
- Be warm and friendly, not clinical
- NEVER suggest dose changes or diagnose
- If patient says bye/done/goodbye — say goodbye warmly
- If serious symptoms like chest pain, breathless, unconscious — 
  say pharmacist will call back immediately
- Reply in English"""

SERIOUS_SYMPTOMS = [
    "chest pain", "breathless", "unconscious", "faint",
    "bleeding", "severe", "emergency", "hospital"
]

def check_serious(text):
    return any(s in text.lower() for s in SERIOUS_SYMPTOMS)

def send_whatsapp_message(to_number, message):
    """Send a WhatsApp message via Meta Cloud API"""
    url = "https://graph.facebook.com/v18.0/" + PHONE_NUMBER_ID + "/messages"
    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=payload)
    print("Send status: " + str(response.status_code))
    print("Response: " + response.text[:200])
    return response.status_code == 200

def get_ai_response(phone_number, patient_message):
    """Get AI response maintaining conversation history"""
    # Initialize conversation if new patient
    if phone_number not in conversations:
        conversations[phone_number] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        print("New conversation started for: " + phone_number)

    # Add patient message to history
    conversations[phone_number].append({
        "role": "user",
        "content": patient_message
    })

    # Get AI response
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=conversations[phone_number],
        temperature=0.7,
        max_tokens=150
    )
    ai_reply = response.choices[0].message.content

    # Add AI reply to history
    conversations[phone_number].append({
        "role": "assistant",
        "content": ai_reply
    })

    # Save transcript line
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_line = "[" + timestamp + "] Patient: " + patient_message + "\n"
    log_line += "[" + timestamp + "] Agent  : " + ai_reply + "\n"
    with open("transcript_wa_" + phone_number[-4:] + ".txt", "a", encoding="utf-8") as f:
        f.write(log_line)

    return ai_reply

# ── Webhook Routes ────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta calls this once to verify your server"""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    print("Verification attempt — mode: " + str(mode) + " token: " + str(token))

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified!")
        return challenge, 200
    else:
        print("❌ Verification failed!")
        return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    """Meta calls this every time a patient sends a WhatsApp message"""
    data = request.get_json()
    print("Incoming message: " + json.dumps(data, indent=2)[:300])

    try:
        # Extract message details from Meta payload
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Only process actual messages (ignore status updates)
        if "messages" not in value:
            return jsonify({"status": "no message"}), 200

        message      = value["messages"][0]
        from_number  = message["from"]        # patient phone number
        message_type = message["type"]

        # Only handle text messages for now
        if message_type != "text":
            send_whatsapp_message(from_number, 
                "Hi! I can only understand text messages right now. Please type your message!")
            return jsonify({"status": "non-text ignored"}), 200

        patient_text = message["text"]["body"]
        print("Message from " + from_number + ": " + patient_text)

        # Check for serious symptoms first
        if check_serious(patient_text):
            alert_msg = (
                "I am very concerned to hear that. "
                "I am alerting our pharmacist RIGHT NOW — "
                "they will call you back within 15 minutes. "
                "Please stay calm and safe!"
            )
            send_whatsapp_message(from_number, alert_msg)
            # Clear conversation
            conversations.pop(from_number, None)
            return jsonify({"status": "escalated"}), 200

        # Get AI response and send back
        ai_reply = get_ai_response(from_number, patient_text)
        send_whatsapp_message(from_number, ai_reply)

    except Exception as e:
        print("Error processing message: " + str(e))

    return jsonify({"status": "ok"}), 200

@app.route("/", methods=["GET"])
def home():
    return "MediCare WhatsApp Bot is running! 💊"

@app.route("/conversations", methods=["GET"])
def view_conversations():
    """Quick view of active conversations"""
    summary = {}
    for phone, history in conversations.items():
        summary[phone] = len(history) - 1  # minus system prompt
    return jsonify(summary)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("Starting MediCare WhatsApp Bot on port " + str(port))
    app.run(host="0.0.0.0", port=port, debug=False)
