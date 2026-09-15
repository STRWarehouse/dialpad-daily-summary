#!/usr/bin/env python3
"""Daily Dialpad owner/prospect summary for a Render Cron Job."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def request_json(method, url, token=None, body=None, timeout=60):
    headers = {"Accept": "application/json", "User-Agent": "CoHostBob-Dialpad-Summary/2.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def list_items(payload):
    if isinstance(payload, list):
        return payload
    for key in ("items", "calls", "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def recap_text(payload):
    for key in ("summary", "recap", "text", "content"):
        if isinstance(payload.get(key), str) and payload[key].strip():
            return payload[key]
    return json.dumps(payload, ensure_ascii=False)


def output_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks = []
    for item in payload.get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(part["text"])
    return "\n".join(chunks)


def main():
    dialpad_key = required("DIALPAD_API_KEY")
    openai_key = required("OPENAI_API_KEY")
    bridge_secret = required("BRIDGE_SECRET")
    feed_url = required("FEED_URL")
    mail_url = required("MAIL_URL")
    recipient = os.environ.get("RECIPIENT_EMAIL", "Bob@cohostbob.com")
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    local_tz = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(hours=12)).timestamp() * 1000)
    query = urllib.parse.urlencode({"started_after": start_ms, "started_before": end_ms})
    print("1/4 Fetching recent Dialpad calls...", flush=True)
    calls = list_items(request_json("GET", f"https://dialpad.com/api/v2/call?{query}", dialpad_key))

    call_notes = []
    selected_calls = calls[:10]
    print(f"2/4 Fetching AI recaps for {len(selected_calls)} calls...", flush=True)
    for index, call in enumerate(selected_calls, 1):
        call_id = call.get("id") or call.get("call_id")
        if not call_id:
            continue
        try:
            recap_url = f"https://dialpad.com/api/v2/call/{urllib.parse.quote(str(call_id))}/ai_recap?summary_format=bullet"
            recap = request_json("GET", recap_url, dialpad_key)
            call_notes.append({"call": call, "ai_recap": recap_text(recap)})
        except Exception as exc:
            print(f"Warning: recap {index} unavailable: {exc}", flush=True)
            call_notes.append({"call": call, "ai_recap": "No AI recap available"})
        if index < len(selected_calls):
            time.sleep(5.2)

    print("3/4 Fetching the last 12 hours of captured SMS messages...", flush=True)
    sms_payload = request_json("GET", feed_url, bridge_secret)
    sms_events = list_items(sms_payload)
    source = json.dumps({"calls": call_notes, "sms_messages": sms_events[-250:]}, ensure_ascii=False)
    source = source[:120000]

    instructions = (
        "Create a concise daily report for Bob Liddle, a short-term-rental property manager. "
        "Group information by EXTERNAL OWNER or PROSPECTIVE CLIENT, not by employee. "
        "For each person, label them Owner, Prospect, Guest, Vendor, or Unknown only when supported. "
        "Include concrete updates, requested dates, complaints, promises, and follow-up actions. "
        "Never invent names, properties, dates, or facts. If a contact name is unavailable, use the phone number. "
        "Omit routine small talk and duplicate messages. End with a short 'Needs Bob's attention' section. "
        "Output clean HTML suitable for email."
    )
    print("4/4 Creating and emailing the summary...", flush=True)
    ai = request_json("POST", "https://api.openai.com/v1/responses", openai_key, {
        "model": model,
        "input": [{"role": "system", "content": instructions}, {"role": "user", "content": source}],
    }, timeout=120)
    html = output_text(ai).strip()
    if not html:
        raise RuntimeError("OpenAI returned no summary text")

    subject = "Dialpad Daily Owner & Prospect Summary — " + datetime.now(local_tz).strftime("%B %-d, %Y")
    result = request_json("POST", mail_url, bridge_secret, {
        "to": recipient, "subject": subject, "html": html,
        "counts": {"calls": len(call_notes), "sms": len(sms_events)},
    })
    if not result.get("sent"):
        raise RuntimeError(f"Mail bridge did not confirm delivery: {result}")
    print(f"SUCCESS: emailed {recipient}; calls={len(call_notes)}, sms={len(sms_events)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
