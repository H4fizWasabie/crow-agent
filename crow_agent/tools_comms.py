"""Communication tools: Telegram, Threads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from crow_agent.telegram_rich import contains_pipe_table, _safe_html_chunks, format_telegram_html


def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register communication tools."""

    @registry.register(
        description="Send a message or file to your Telegram chat. Requires TELEGRAM_TOKEN and TELEGRAM_ALLOWED_IDS in .env."
    )
    def send_telegram(message: str = "", file_path: str = "", parse_mode: str = "HTML") -> str:
        token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        allowed = os.environ.get("TELEGRAM_ALLOWED_IDS", "").strip()
        if not token:
            return "TELEGRAM_TOKEN not set. Add it to .env"
        if not allowed:
            return "TELEGRAM_ALLOWED_IDS not set. Add your chat ID(s) to .env"

        chat_ids = [int(x.strip()) for x in allowed.split(",") if x.strip()]
        if not chat_ids:
            return "No valid chat IDs in TELEGRAM_ALLOWED_IDS"

        if not message and not file_path:
            return "Nothing to send. Provide message, file_path, or both."

        import httpx

        chat_id = chat_ids[0]
        api = f"https://api.telegram.org/bot{token}"

        try:
            safe_message = message or ""

            if file_path:
                fp = Path(file_path)
                if not fp.exists():
                    return f"File not found: {file_path}"
                if not fp.is_file():
                    return f"Not a file: {file_path}"
                size = fp.stat().st_size
                if size > 50 * 1024 * 1024:
                    return f"File too large: {size / 1024 / 1024:.1f}MB (Telegram max: 50MB)"

                data: dict[str, object] = {"chat_id": chat_id}
                if safe_message:
                    data["caption"] = safe_message
                if parse_mode:
                    data["parse_mode"] = parse_mode

                is_voice = fp.suffix.lower() == ".ogg"
                endpoint = f"{api}/sendVoice" if is_voice else f"{api}/sendDocument"
                field = "voice" if is_voice else "document"
                mime = "audio/ogg" if is_voice else "application/octet-stream"

                resp = httpx.post(
                    endpoint,
                    data=data,
                    files={field: (fp.name, fp.read_bytes(), mime)},
                    timeout=120,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("ok"):
                    return f"Sent file to Telegram chat {chat_id}: {fp.name}"
                return f"Telegram API error: {result}"

            # ponytail: split on --- for multi-DM delivery
            sections = [s.strip() for s in safe_message.split("\n---\n") if s.strip()]
            if not sections:
                sections = [safe_message.strip()] if safe_message.strip() else [safe_message]

            sent_count = 0
            for section in sections:
                payload: dict[str, object] = {"chat_id": chat_id, "text": section}
                if parse_mode:
                    payload["parse_mode"] = parse_mode

                if parse_mode == "HTML" and contains_pipe_table(section):
                    try:
                        formatted = format_telegram_html(section, rich_tables=True)
                        for chunk in _safe_html_chunks(formatted, 4000):
                            httpx.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}, timeout=30)
                        sent_count += 1
                        continue
                    except Exception:
                        pass

                resp = httpx.post(f"{api}/sendMessage", json=payload, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                if not result.get("ok"):
                    return f"Telegram API error: {result}"
                sent_count += 1

            if sent_count > 1:
                return f"Sent {sent_count} messages to Telegram chat {chat_id}"
            return f"Sent message to Telegram chat {chat_id}"

        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            return f"Telegram API error ({exc.response.status_code}): {body}"
        except Exception as exc:
            return f"Error sending to Telegram: {exc}"

    @registry.register(
        description="Post text to Threads. If reply_to is set, creates a reply (chained thread) to the given post ID."
    )
    def post_to_threads(text: str, reply_to: str = "") -> str:
        token = os.environ.get("THREADS_ACCESS_TOKEN")
        user_id = os.environ.get("THREADS_USER_ID")
        if not token or not user_id:
            return "THREADS_ACCESS_TOKEN or THREADS_USER_ID not set. Add them to .env"
        import httpx
        api = "https://graph.threads.net/v1.0"
        try:
            payload: dict[str, str] = {
                "media_type": "TEXT",
                "text": text,
                "access_token": token,
            }
            if reply_to:
                payload["reply_to_id"] = reply_to

            create = httpx.post(
                f"{api}/{user_id}/threads",
                data=payload,
                timeout=30,
            )
            create.raise_for_status()
            container_id = create.json().get("id")
            publish = httpx.post(
                f"{api}/{user_id}/threads_publish",
                data={"creation_id": container_id, "access_token": token},
                timeout=30,
            )
            publish.raise_for_status()
            post_id = publish.json().get("id")
            kind = "reply" if reply_to else "post"
            return f"Posted {kind} to Threads! Post ID: {post_id}"
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            return f"Threads API error ({exc.response.status_code}): {body}"
        except Exception as exc:
            return f"Error posting to Threads: {exc}"
