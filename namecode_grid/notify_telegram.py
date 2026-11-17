#!/usr/bin/env python3
"""Send one Telegram status notification, without retrying uncertain delivery.

Uses TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and MESSAGE (or argv[1]).
Missing configuration is a no-op. Only an API message_id confirms acceptance,
not that a person has received/read it. Transport errors remain unknown.
GitHub outputs and the step summary record the outcome without exposing secrets.
The caller decides whether a notification failure should fail its workflow.
One POST per invocation is NOT deduplication across workflow reruns. No durable
delivery ledger or idempotency key is implemented; reconcile before rerunning.
"""
import os
import sys

import requests


def _log(line):
    try:
        print(line)
    except OSError:
        # A broken reporting sink must not turn an accepted send into a failed
        # send. Try the independent error stream, but never repeat the POST.
        try:
            print("::warning::Telegram console reporting failed; API outcome is unchanged. "
                  "Do not resend based on a reporting error.", file=sys.stderr)
        except OSError:
            pass


def _append_report(variable, text):
    path = os.environ.get(variable)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text)
    except OSError:
        # Never include the path or exception: either could contain private data.
        _log(f"::warning::Telegram {variable} reporting failed; API outcome is unchanged. "
             "Do not resend based on a reporting error.")


def _report(status, detail, *, message_id=None):
    # Never log exception strings or response bodies: they can contain the
    # token-bearing request URL, private chat details, or the message itself.
    line = f"Telegram notification: {status}. {detail}"
    if status in ("unknown", "rejected"):
        _log(f"::warning::{line} Do not retry automatically.")
    else:
        _log(line)
    output = f"telegram_status={status}\n"
    summary = f"\n### Telegram notification: {status}\n\n{detail}\n"
    if message_id is not None:
        output += f"telegram_message_id={message_id}\n"
        summary += f"API message ID: {message_id}. This is not a read receipt.\n"
    summary += ("At most one POST was made in this invocation; no retry was made. "
                "This does not prevent duplicates across reruns. Reconcile the destination "
                "before any resend; do not rerun artwork generation or publication "
                "for this notification.\n")
    # Reporting failures are independent of delivery, and of each other.
    _append_report("GITHUB_OUTPUT", output)
    _append_report("GITHUB_STEP_SUMMARY", summary)
    return 0 if status in ("accepted", "skipped") else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    msg = argv[0] if argv else os.environ.get("MESSAGE", "")
    if not tok or not chat:
        return _report("skipped", "Bot token or chat ID is not configured; no request was made.")
    payload = {"chat_id": chat, "text": msg or "(empty)", "disable_web_page_preview": True}
    row = []
    for suffix in ("", "2"):
        url = os.environ.get(f"BUTTON{suffix}_URL")
        if url:
            row.append({"text": os.environ.get(f"BUTTON{suffix}_TEXT", "Open"), "url": url})
    if row:
        payload["reply_markup"] = {"inline_keyboard": [row]}
    try:
        # A read timeout may occur AFTER Telegram accepted the message. Never
        # retry this non-idempotent POST, including through an HTTP redirect.
        response = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json=payload, timeout=(10, 30), allow_redirects=False,
        )
    except requests.RequestException:
        return _report("unknown", "No conclusive API response was received; the message may have been accepted.")
    try:
        data = response.json()
    except ValueError:
        return _report("unknown", "The API response contained no usable acceptance receipt.")
    if not isinstance(data, dict):
        return _report("unknown", "The API response contained no usable acceptance receipt.")
    result = data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if (200 <= response.status_code < 300 and data.get("ok") is True
            and type(message_id) is int and message_id > 0):
        return _report("accepted", "Telegram accepted this notification.", message_id=message_id)
    if response.status_code < 500 and data.get("ok") is False:
        return _report("rejected", "Telegram returned an explicit API rejection.")
    return _report("unknown", "No conclusive acceptance receipt was returned.")


if __name__ == "__main__":
    sys.exit(main())

