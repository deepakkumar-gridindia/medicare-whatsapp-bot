import os
import re
import json
import requests
from flask import Flask, request, jsonify
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "medicarebot123")
ACCESS_TOKEN    = os.getenv("WHATSAPP_TOKEN",  "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY",    "")

client = Groq(api_key=GROQ_API_KEY)

# ── In-memory stores ──────────────────────────────────────
conversations  = {}   # phone → full conversation history (with system prompt)
wa_transcripts = {}   # phone → list of transcript lines
ended_calls    = set()  # phones where conversation has ended

# ── Transcript helpers ────────────────────────────────────
def save_wa_transcript(phone, role, message):
    clean_phone = phone.replace("+","").replace(" ","")
    if clean_phone not in wa_transcripts:
        wa_transcripts[clean_phone] = []
    line = role + " : " + message
    wa_transcripts[clean_phone].append(line)
    try:
        with open("wa_transcript_" + clean_phone + ".txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def clean_message(text):
    """Remove all [TAGS] and internal markers before sending to patient"""
    if not text:
        return ""
    # Remove all bracket content
    clean = re.sub(r'\[[^\]]*\]', '', text).strip()
    # Remove bare tag words
    clean = re.sub(r'\b(GREEN|RED|YELLOW|IND_READ|DIR_READ|LISTEN)[:\s]*\S*', '', clean).strip()
    # Remove END CALL text
    clean = clean.replace("END CALL", "").strip()
    # Clean up extra spaces/newlines
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def has_end_call(text):
    """Check if AI wants to end the call"""
    return "[END CALL]" in text or "END CALL" in text.upper()

# ── WhatsApp send ─────────────────────────────────────────
def send_whatsapp_message(to_number, message):
    url = "https://graph.facebook.com/v18.0/" + PHONE_NUMBER_ID + "/messages"
    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type":  "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to_number,
        "type": "text",
        "text": {"body": message}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print("Send to " + to_number + " status: " + str(response.status_code))
        if response.status_code != 200:
            print("Error: " + response.text[:200])
        return response.status_code == 200
    except Exception as e:
        print("Send error: " + str(e))
        return False

# ── AI response ───────────────────────────────────────────
def get_ai_response(phone, patient_message):
    """Get AI response using stored conversation history"""
    if phone not in conversations:
        # Fallback simple prompt if no context set
        conversations[phone] = [{
            "role": "system",
            "content": (
                "You are a warm pharmacy assistant from MediCare Pharmacy. "
                "Follow up with the patient about their medications. "
                "Keep responses to 2-3 sentences. Be warm and friendly."
            )
        }]

    conversations[phone].append({"role": "user", "content": patient_message})

    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=conversations[phone],
        temperature=0.7,
        max_tokens=200
    )
    ai_reply = response.choices[0].message.content
    conversations[phone].append({"role": "assistant", "content": ai_reply})

    return ai_reply

# ── Serious symptoms ──────────────────────────────────────
SERIOUS_SYMPTOMS = [
    "chest pain", "breathless", "unconscious", "faint",
    "bleeding", "severe", "emergency", "hospital",
    "heart attack", "stroke"
]

def check_serious(text):
    return any(s in text.lower() for s in SERIOUS_SYMPTOMS)

# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return "MediCare WhatsApp Bot is running! 💊"

# ── Webhook verification ──────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    print("Verification — mode: " + str(mode) + " token: " + str(token))
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified!")
        return challenge, 200
    return "Forbidden", 403

# ── Receive WhatsApp message ──────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    print("Incoming: " + json.dumps(data)[:200])

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        if "messages" not in value:
            return jsonify({"status": "no message"}), 200

        message      = value["messages"][0]
        from_number  = message["from"]
        message_type = message["type"]

        # Only handle text messages
        if message_type != "text":
            send_whatsapp_message(from_number,
                "Hi! Please send a text message and I will be happy to help you.")
            return jsonify({"status": "non-text"}), 200

        patient_text = message["text"]["body"]
        print("From " + from_number + ": " + patient_text)

        # Save patient message to transcript
        save_wa_transcript(from_number, "Patient", patient_text)

        # If call already ended — don't respond
        if from_number in ended_calls:
            print("Call already ended for: " + from_number)
            return jsonify({"status": "call_ended"}), 200

        # Check for serious symptoms
        if check_serious(patient_text):
            alert = (
                "I am very concerned to hear that! "
                "I am alerting our pharmacist RIGHT NOW — "
                "they will call you back within 15 minutes. "
                "Please stay safe and calm. Goodbye!"
            )
            send_whatsapp_message(from_number, alert)
            save_wa_transcript(from_number, "Agent  ", alert + " [ESCALATED]")
            conversations.pop(from_number, None)
            ended_calls.add(from_number)
            return jsonify({"status": "escalated"}), 200

        # Get AI response
        ai_reply_raw = get_ai_response(from_number, patient_text)

        # Check for end call BEFORE cleaning
        is_end = has_end_call(ai_reply_raw)

        # Clean tags from message before sending to patient
        ai_reply_clean = clean_message(ai_reply_raw)

        # Send to patient
        send_whatsapp_message(from_number, ai_reply_clean)
        save_wa_transcript(from_number, "Agent  ", ai_reply_clean)

        # End conversation if needed
        if is_end:
            print("Conversation ended for: " + from_number)
            conversations.pop(from_number, None)
            ended_calls.add(from_number)

    except Exception as e:
        print("Error: " + str(e))
        import traceback
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200

# ── Dashboard API: Start conversation ─────────────────────
@app.route("/wa_send", methods=["POST"])
def send_opening_message():
    """
    Dashboard calls this to:
    1. Set the structured system prompt for this patient
    2. Send opening message to patient via WhatsApp
    """
    data        = request.get_json()
    phone       = data.get("phone","").replace("+","").replace(" ","").strip()
    message     = data.get("message","")
    context     = data.get("context","")   # Full structured prompt from dashboard

    print("wa_send called for: " + phone)

    # Clear any previous conversation for fresh start
    conversations.pop(phone, None)
    wa_transcripts.pop(phone, None)
    ended_calls.discard(phone)

    # Clear transcript file
    try:
        open("wa_transcript_" + phone + ".txt", "w").close()
    except:
        pass

    # Set the FULL structured system prompt from dashboard
    if context:
        conversations[phone] = [{"role": "system", "content": context}]
        print("✅ Structured prompt set for: " + phone)
    else:
        conversations[phone] = [{
            "role": "system",
            "content": "You are a warm pharmacy assistant from MediCare Pharmacy."
        }]

    # Also store the opening message as first assistant turn
    # so AI knows it already said the greeting
    clean_msg = clean_message(message)
    if clean_msg:
        conversations[phone].append({"role": "assistant", "content": clean_msg})

    # Send opening message to patient
    success = send_whatsapp_message(phone, clean_msg)

    if success:
        save_wa_transcript(phone, "Agent  ", clean_msg)
        return jsonify({"status": "sent", "phone": phone})
    else:
        return jsonify({"status": "failed", "error": "WhatsApp send failed"}), 500

# ── Dashboard API: Get live transcript ───────────────────
@app.route("/wa_transcript/<phone>", methods=["GET"])
def get_wa_transcript(phone):
    phone = phone.replace("+","").replace(" ","").strip()
    lines = wa_transcripts.get(phone, [])

    # Also try reading from file if not in memory (after server restart)
    if not lines:
        try:
            with open("wa_transcript_" + phone + ".txt", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            if lines:
                wa_transcripts[phone] = lines
        except:
            lines = []

    return jsonify({
        "phone":    phone,
        "lines":    lines,
        "count":    len(lines),
        "is_ended": phone in ended_calls
    })

# ── Dashboard API: Clear transcript ──────────────────────
@app.route("/wa_clear/<phone>", methods=["POST"])
def clear_transcript(phone):
    phone = phone.replace("+","").replace(" ","").strip()
    wa_transcripts.pop(phone, None)
    conversations.pop(phone, None)
    ended_calls.discard(phone)
    try:
        open("wa_transcript_" + phone + ".txt", "w").close()
    except:
        pass
    return jsonify({"status": "cleared", "phone": phone})

# ── Dashboard API: View active conversations ─────────────
@app.route("/conversations", methods=["GET"])
def view_conversations():
    summary = {}
    for phone, history in conversations.items():
        summary[phone] = {
            "messages": len(history) - 1,
            "ended":    phone in ended_calls
        }
    return jsonify(summary)

# ── Dashboard API: Check status ───────────────────────────
@app.route("/status/<phone>", methods=["GET"])
def check_status(phone):
    phone = phone.replace("+","").replace(" ","").strip()
    return jsonify({
        "phone":        phone,
        "active":       phone in conversations,
        "ended":        phone in ended_calls,
        "msg_count":    len(wa_transcripts.get(phone, [])),
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print("Starting MediCare WhatsApp Bot on port " + str(port))
    app.run(host="0.0.0.0", port=port, debug=False)
