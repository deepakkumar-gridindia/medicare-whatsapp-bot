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

# ── Load patient data ─────────────────────────────────────
def load_patients():
    try:
        with open("patients.json", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

# ── In-memory stores ──────────────────────────────────────
conversations  = {}
wa_transcripts = {}
ended_calls    = set()

# ── Build structured prompt (same as dashboard) ───────────
def build_prompt(patient):
    fname = patient["name"].split()[0]
    lname = " ".join(patient["name"].split()[1:])
    sal   = "Mr. " + lname if lname else fname
    drugs = patient["drugs"]

    drug_ref = ""
    for i, d in enumerate(drugs, 1):
        drug_ref += (
            f"\nDRUG {i}:"
            f"\n  Name       : {d['drug_name']} {d['dosage']}"
            f"\n  Indication : {d['indication']}"
            f"\n  Direction  : {d['direction']}"
            f"\n  Refill due : {d['refill_due']}"
            f"\n  Prescriber : Dr. {d['prescriber']}"
        )

    steps = f"""STEP 1 — GREETING:
Say: "Hello {patient['name']}, thank you for taking the time to speak with me today from MediCare Pharmacy. How have you been doing lately?"
=> STOP. Wait for patient reply. Then go to STEP 2.

"""
    step_num = 2
    for i, d in enumerate(drugs, 1):
        steps += f"""STEP {step_num} — DRUG {i} CONFIRMATION:
Say: "{sal}, I am here to go over your medications with you today. Are you still taking {d['drug_name']} {d['dosage']} as directed — {d['direction']} — as prescribed by Dr. {d['prescriber']}, which is used to help manage your {d['indication']}?"
=> Add tags: [IND_READ:{d['drug_name']}] [DIR_READ:{d['drug_name']}]
=> If YES → add [GREEN:{d['drug_name']}]
=> If NO  → add [RED:{d['drug_name']}]
=> STOP. Wait for patient reply. Then go to STEP {step_num + 1}.

STEP {step_num + 1} — DRUG {i} REFILL:
=> If patient said YES to taking drug:
   Say: "Your refill for {d['drug_name']} {d['dosage']} is due on {d['refill_due']}. Would you like us to arrange the refill for you?"
   STOP. Wait for patient reply.
=> If patient said NO to taking drug:
   Say: "Since you are not currently taking {d['drug_name']}, a refill may not be needed right now. However, its refill was due on {d['refill_due']} — if you need it in future, we can arrange it. Would you like us to keep it on hold?"
   STOP. Wait for patient reply.
=> DO NOT mention any other drug in this message.
=> Then go to STEP {step_num + 2}.

"""
        step_num += 2

    steps += f"""STEP {step_num} — GENERAL HEALTH:
Say: "Now that we have covered all your medications, how has your overall health been lately, {fname}?"
=> STOP. Wait for patient reply. Then go to STEP {step_num + 1}.

STEP {step_num + 1} — CLOSING:
Say: "Is there anything else you would like to discuss or any questions you have for me today?"
=> STOP. Wait for patient reply.
=> After patient replies → close warmly → add [END CALL]
=> Do NOT loop back. IMMEDIATELY add [END CALL] after closing.
"""

    return f"""You are a warm pharmacy assistant from MediCare Pharmacy calling {patient['name']}.

PATIENT MEDICATIONS:
{drug_ref}

FOLLOW THIS EXACT SEQUENCE — ONE STEP PER MESSAGE:
{steps}

ABSOLUTE RULES:
1. ONE step per message — never combine two steps
2. ALWAYS wait for patient reply before next step
3. NEVER ask about Drug 2 until Drug 1 refill is answered
4. NEVER jump to health question until ALL drug refills done
5. Language: {patient['language']}

TAGS — include silently (hidden from patient):
[GREEN:drug_name]    when patient confirms taking drug
[RED:drug_name]      when patient says NOT taking drug
[YELLOW:drug_name]   when patient mentions new medicine not in list
[IND_READ:drug_name] when YOU mention the indication
[DIR_READ:drug_name] when YOU mention the direction"""

# ── Clean tags from text ──────────────────────────────────
def clean_message(text):
    if not text: return ""
    clean = re.sub(r'\[[^\]]*\]', '', text).strip()
    clean = re.sub(r'\b(GREEN|RED|YELLOW|IND_READ|DIR_READ|LISTEN)[:\s]*\S*', '', clean).strip()
    clean = clean.replace("END CALL","").strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def has_end_call(text):
    return "[END CALL]" in text or "END CALL" in text.upper()

# ── Transcript helpers ────────────────────────────────────
def save_wa_transcript(phone, role, message):
    if phone not in wa_transcripts:
        wa_transcripts[phone] = []
    line = role + " : " + message
    wa_transcripts[phone].append(line)
    try:
        with open("wa_transcript_" + phone + ".txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

# ── Send WhatsApp message ─────────────────────────────────
def send_whatsapp_message(to_number, message):
    url     = "https://graph.facebook.com/v18.0/" + PHONE_NUMBER_ID + "/messages"
    headers = {"Authorization":"Bearer "+ACCESS_TOKEN,"Content-Type":"application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to":   to_number,
        "type": "text",
        "text": {"body": message}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        print("Send status:", r.status_code)
        if r.status_code != 200:
            print("Error:", r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("Send error:", e)
        return False

# ── Get AI response ───────────────────────────────────────
def get_ai_response(phone, patient_message):
    if phone not in conversations:
        # Auto-load patient from patients.json using phone number
        patients = load_patients()
        patient  = patients.get(phone)
        if patient:
            print("✅ Loaded patient from JSON:", patient["name"])
            conversations[phone] = [{"role":"system","content":build_prompt(patient)}]
        else:
            print("⚠️ Patient not found for:", phone)
            conversations[phone] = [{"role":"system","content":(
                "You are a warm pharmacy assistant from MediCare Pharmacy. "
                "Follow up with the patient about their medications warmly."
            )}]

    conversations[phone].append({"role":"user","content":patient_message})
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=conversations[phone],
        temperature=0.7,
        max_tokens=200
    )
    ai_reply = resp.choices[0].message.content
    conversations[phone].append({"role":"assistant","content":ai_reply})
    return ai_reply

# ── Serious symptoms ──────────────────────────────────────
SERIOUS = ["chest pain","breathless","unconscious","faint","bleeding",
           "severe","emergency","hospital","heart attack","stroke"]

def check_serious(text):
    return any(s in text.lower() for s in SERIOUS)

# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    return "MediCare WhatsApp Bot is running! 💊"

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified!")
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        if "messages" not in value:
            return jsonify({"status":"no message"}), 200

        message      = value["messages"][0]
        from_number  = message["from"]
        message_type = message["type"]

        if message_type != "text":
            send_whatsapp_message(from_number,
                "Hi! Please send a text message and I will be happy to assist you.")
            return jsonify({"status":"non-text"}), 200

        patient_text = message["text"]["body"]
        print("From", from_number, ":", patient_text)
        save_wa_transcript(from_number, "Patient", patient_text)

        if from_number in ended_calls:
            print("Call already ended for:", from_number)
            return jsonify({"status":"call_ended"}), 200

        if check_serious(patient_text):
            alert = (
                "I am very concerned to hear that! "
                "I am alerting our pharmacist RIGHT NOW — "
                "they will call you back within 15 minutes. "
                "Please stay safe and calm. Goodbye!"
            )
            send_whatsapp_message(from_number, alert)
            save_wa_transcript(from_number, "Agent  ", alert+" [ESCALATED]")
            conversations.pop(from_number, None)
            ended_calls.add(from_number)
            return jsonify({"status":"escalated"}), 200

        ai_reply_raw   = get_ai_response(from_number, patient_text)
        is_end         = has_end_call(ai_reply_raw)
        ai_reply_clean = clean_message(ai_reply_raw)

        send_whatsapp_message(from_number, ai_reply_clean)
        save_wa_transcript(from_number, "Agent  ", ai_reply_clean)

        if is_end:
            print("Conversation ended for:", from_number)
            conversations.pop(from_number, None)
            ended_calls.add(from_number)

    except Exception as e:
        print("Error:", e)
        import traceback; traceback.print_exc()

    return jsonify({"status":"ok"}), 200

@app.route("/wa_send", methods=["POST"])
def send_opening_message():
    data    = request.get_json()
    phone   = data.get("phone","").replace("+","").replace(" ","").strip()
    message = data.get("message","")

    print("wa_send for:", phone)

    # Clear previous conversation
    conversations.pop(phone, None)
    wa_transcripts.pop(phone, None)
    ended_calls.discard(phone)
    try: open("wa_transcript_"+phone+".txt","w").close()
    except: pass

    # Load patient from JSON and set structured prompt
    patients = load_patients()
    patient  = patients.get(phone)
    if patient:
        print("✅ Setting structured prompt for:", patient["name"])
        conversations[phone] = [{"role":"system","content":build_prompt(patient)}]
        # Store opening as first assistant turn so bot continues from STEP 2
        clean_msg = clean_message(message)
        if clean_msg:
            conversations[phone].append({"role":"assistant","content":clean_msg})
    else:
        print("⚠️ Patient not found for phone:", phone)
        conversations[phone] = [{"role":"system","content":(
            "You are a warm pharmacy assistant from MediCare Pharmacy."
        )}]
        clean_msg = clean_message(message)

    success = send_whatsapp_message(phone, clean_msg)
    if success:
        save_wa_transcript(phone, "Agent  ", clean_msg)
        return jsonify({"status":"sent","phone":phone,"patient":patient["name"] if patient else "unknown"})
    else:
        return jsonify({"status":"failed"}), 500

@app.route("/wa_transcript/<phone>", methods=["GET"])
def get_wa_transcript(phone):
    phone = phone.replace("+","").replace(" ","").strip()
    lines = wa_transcripts.get(phone, [])
    if not lines:
        try:
            with open("wa_transcript_"+phone+".txt", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            if lines: wa_transcripts[phone] = lines
        except: lines = []
    return jsonify({
        "phone":    phone,
        "lines":    lines,
        "count":    len(lines),
        "is_ended": phone in ended_calls
    })

@app.route("/wa_clear/<phone>", methods=["POST"])
def clear_transcript(phone):
    phone = phone.replace("+","").replace(" ","").strip()
    wa_transcripts.pop(phone, None)
    conversations.pop(phone, None)
    ended_calls.discard(phone)
    try: open("wa_transcript_"+phone+".txt","w").close()
    except: pass
    return jsonify({"status":"cleared","phone":phone})

@app.route("/conversations", methods=["GET"])
def view_conversations():
    summary = {}
    for phone, history in conversations.items():
        summary[phone] = {"messages":len(history)-1,"ended":phone in ended_calls}
    return jsonify(summary)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print("Starting MediCare WhatsApp Bot on port", port)
    app.run(host="0.0.0.0", port=port, debug=False)
