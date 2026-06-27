"""Telegram bot for Crow Agent. chat_id -> session_id bridge.

Optional — only activates if TELEGRAM_TOKEN env var is set.
Runs alongside the web server in the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.helpers import escape

from crow_agent.crow_state import CrowState
from crow_agent.paths import PROJECT_ROOT
from crow_agent.run_agent import AIAgent, Trigger, TriggerSource
from crow_agent.telegram_rich import (
    format_telegram_html,
    _safe_html_chunks,
)


logger = logging.getLogger("crow_agent.telegram")

# Module-level bot instance reference for external callers (heartbeat)
_bot_instance = None


def _wrap_tool_outputs(text: str) -> str:
    """Wrap ---TOOL OUTPUT--- sections in expandable blockquotes."""
    import re as _re2
    # Match TOOL OUTPUT blocks
    pattern = r'(---TOOL OUTPUT \([^)]+\)---.*?---END TOOL OUTPUT \([^)]+\)---)'
    def _wrap(m):
        content = m.group(0)
        # Only wrap if substantial (>200 chars)
        if len(content) > 200:
            return f"<blockquote expandable>\n{content}\n</blockquote>"
        return content
    return _re2.sub(pattern, _wrap, text, flags=re.DOTALL)


class TelegramBot:
    """PTB wrapper. Caches AIAgent per chat_id for conversation continuity."""

    def __init__(
        self,
        token: str,
        agent_factory: Callable[[str], AIAgent],
        allowed_ids: set[int],
        db: CrowState | None = None,
    ) -> None:
        self._token = token
        self._factory = agent_factory
        self._allowed = allowed_ids
        self._db = db
        self._agents: dict[int, AIAgent] = {}
        self._max_agents = 50  # ponytail: evict oldest session when full
        self._locks: dict[int, asyncio.Lock] = {}
        global _bot_instance
        _bot_instance = self
        self._app: Application | None = None

    async def start(self) -> None:
        """Initialize PTB app and start polling."""
        builder = Application.builder().token(self._token)
        self._app = builder.build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("build", self._cmd_build))
        self._app.add_handler(CallbackQueryHandler(self._handle_task_callback, pattern=r"^(task_done|snooze_):"))
        self._app.add_handler(
            MessageHandler(filters.PHOTO | filters.Document.ALL, self._handle_file)
        )
        self._app.add_handler(
            MessageHandler(filters.VOICE, self._handle_voice)
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self._app.initialize()
        await self._app.start()
        # PTB v20+ requires explicit updater.start_polling() — Application.start()
        # only starts internal components (context, job queue), not the polling loop.
        await self._app.updater.start_polling()

        # Notify user on startup (recovery from crash/restart)
        # ponytail: cooldown via state file — skip if already sent within 30 min
        cooldown_path = Path("/tmp/crow_startup_notified")
        now = time.time()
        if cooldown_path.exists():
            try:
                last = float(cooldown_path.read_text().strip())
                if now - last < 1800:  # 30 min cooldown
                    logger.info("Telegram startup notify skipped (cooldown: %.0fs ago)", now - last)
                    return
            except (ValueError, OSError):
                pass
        for uid in self._allowed:
            try:
                await self._app.bot.send_message(
                    chat_id=uid,
                    text="✅ Crow is online.",
                )
            except Exception:
                pass
        cooldown_path.write_text(str(now))
        logger.info(
            "Telegram bot started — allowed IDs: %s", self._allowed
        )

    async def stop(self) -> None:
        """Shut down PTB app cleanly."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot stopped")

    def _is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._allowed

    async def _send_telegram_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        message_effect_id: str | None = None,
    ) -> None:
        """Send text with rich tables when possible, else classic HTML."""
        bot = self._app.bot if self._app else None
        if not bot:
            raise RuntimeError("Telegram bot not initialized")

        html = format_telegram_html(text, rich_tables=False)
        kwargs: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        if message_effect_id:
            kwargs["message_effect_id"] = message_effect_id
        if reply_to_message_id is not None:
            kwargs["reply_parameters"] = {"message_id": reply_to_message_id}
        for chunk in _safe_html_chunks(html, 4000):
            await bot.send_message(text=chunk, **kwargs)

    async def _cmd_start(self, update: Update, _ctx: Any) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return
        await update.message.reply_text(
            "🐦‍⬛ Crow Agent connected.\n"
            "Send any message to chat.\n"
            "/new — start a fresh session"
        )

    async def _cmd_new(self, update: Update, _ctx: Any) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return
        chat_id = update.effective_chat.id
        # Always use lock to avoid TOCTOU race with _handle_message
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        async with self._locks[chat_id]:
            old = self._agents.pop(chat_id, None)
            if old:
                old.close()
        await update.message.reply_text(
            "🔄 Session reset. Send a message to start fresh."
        )

    async def _cmd_build(self, update: Update, _ctx: Any) -> None:
        """Handle /build <app-name> — start autonomous app build."""
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return

        args = update.message.text.strip().split(maxsplit=1)
        if len(args) < 2 or not args[1].strip():
            await update.message.reply_text(
                "Usage: /build <app-name>\n"
                "Requires a frozen spec at ~/crow-builds/<app-name>/spec.md"
            )
            return

        app_name = args[1].strip()
        chat_id = update.effective_chat.id

        from .auto_builder import check_env, check_spec
        spec_err = await asyncio.to_thread(check_spec, app_name)
        if spec_err:
            await update.message.reply_text(f"⛔ {spec_err}")
            return

        env_err = await asyncio.to_thread(check_env)
        if env_err:
            await update.message.reply_text(f"⛔ {env_err}")
            return

        await update.message.reply_text(
            f"🏗️ Starting build: **{app_name}**\n"
            "I'll send progress updates as each phase completes."
        )

        tg_token = self._token
        bot = self._app.bot if self._app else None

        async def _bg_build():
            from .auto_builder import run_build
            try:
                await run_build(app_name, tg_token, chat_id, tg_bot=bot)
            except Exception as exc:
                logger.exception("Auto build crashed: %s", exc)
                try:
                    if bot:
                        await bot.send_message(chat_id=chat_id, text=f"💥 Auto builder crashed: {exc}")
                except Exception:
                    pass

        asyncio.create_task(_bg_build())

    async def _run_agent(self, chat_id: int, text: str, update: Update, status_text: str = "🐦‍⬛ Crow is working...") -> None:
        """Run agent on text, manage lock, typing indicator, status messages, and stream results."""
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()

        # Don't queue — reject immediately if already processing a turn
        if self._locks[chat_id].locked():
            await update.message.reply_text("⏳ Still working — try again in a moment.")
            return

        async with self._locks[chat_id]:
            if chat_id not in self._agents:
                # Evict oldest session if at capacity
                if len(self._agents) >= self._max_agents:
                    oldest = next(iter(self._agents))
                    old_agent = self._agents.pop(oldest)
                    old_agent.close()
                    logger.info("Evicted old session %d (max %d agents)", oldest, self._max_agents)
                self._agents[chat_id] = self._factory(str(chat_id))

            agent = self._agents[chat_id]

            async def _keep_typing():
                while True:
                    try:
                        await update.message.chat.send_action(action="typing")
                        await asyncio.sleep(4)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        break

            typing_task = asyncio.create_task(_keep_typing())
            status_msg = await update.message.reply_text(status_text)

            # Tool icons for status messages
            _tool_icons = {
                "web_search": "🔍 Searching",
                "web_fetch": "📄 Fetching",
                "read_file": "📖 Reading",
                "grep_files": "🔎 Grepping",
                "run_cmd": "⚙️ Running",
                "run_script": "🐍 Scripting",
                "pip_install": "📦 Installing",
                "spawn_agent": "🤖 Spawning",
                "delegate_task": "📋 Delegating",
                "learn": "🧠 Learning",
                "remember": "💭 Recalling",
                "retrieve": "📎 Retrieving",
                "summarize": "📝 Summarizing",
            }

            try:
                from crow_agent.task_registry import set_chat_id
                set_chat_id(chat_id)

                last_tool_msg = None
                final_text = ""
                _tools_used: list[str] = []

                async with asyncio.timeout(600):
                    cont_turns = 0
                    while cont_turns <= 8:
                        trigger = Trigger(source=TriggerSource.USER, prompt=text, chat_id=chat_id)
                        async for event in agent.run_stream(trigger):
                            if isinstance(event, dict):
                                ev_type = event.get("type", "")
                                if ev_type == "tool" and event.get("status") == "start":
                                    name = event["name"]
                                    _tools_used.append(name)
                                    icon = _tool_icons.get(name, f"🛠️ {name}")
                                    try:
                                        if last_tool_msg:
                                            await last_tool_msg.delete()
                                    except Exception:
                                        pass
                                    last_tool_msg = await update.message.reply_text(f"{icon}...")

                                elif ev_type == "final":
                                    final_text = event["text"]

                                elif event.get("done"):
                                    pass

                        if "[CONTINUE]" in final_text:
                            final_text = final_text.replace("[CONTINUE]", "").strip()
                            if not final_text:
                                final_text = "..."  # force retry
                            cont_turns += 1
                            text = "[CONTINUE task] Continue. Signal [DONE] when complete."
                            await self._send_telegram_text(chat_id, final_text, reply_to_message_id=update.message.message_id)
                            final_text = ""
                            continue
                        elif "[DONE]" in final_text:
                            final_text = final_text.replace("[DONE]", "").strip()
                            # ponytail: [DONE]-only means task done, don't loop
                            break
                        else:
                            break

                # Delete status messages
                try:
                    await status_msg.delete()
                    if last_tool_msg:
                        await last_tool_msg.delete()
                except Exception:
                    pass

                # Skip text delivery if send_telegram tool already sent content
                used_send_telegram = "send_telegram" in _tools_used
                if final_text and not used_send_telegram:
                    # ponytail: split on --- for multi-DM delivery
                    sections = [s.strip() for s in final_text.split("\n---\n") if s.strip()]
                    if not sections:
                        sections = [final_text.strip()] if final_text.strip() else []
                    for section in sections:
                        try:
                            await self._send_telegram_text(
                                chat_id,
                                section,
                                reply_to_message_id=update.message.message_id,
                            )
                        except Exception:
                            for chunk in _safe_html_chunks(section, 4000):
                                await update.message.reply_text(chunk)

                # After turn: send activity summary to Crow Log
                from collections import Counter
                log_lines = [f"📥 {text[:80]}"]
                if _tools_used:
                    tc = Counter(_tools_used)
                    tl = ", ".join(f"{n}" + (f" x{c}" if c > 1 else "") for n, c in tc.most_common(10))
                    log_lines.append(f"🛠️ {tl}")
                if final_text:
                    log_lines.append(f"💬 {final_text[:120]}")
                await send_to_crow_log(
                    self._app.bot,
                    "\n".join(log_lines),
                    ""
                )

                # After turn: spawn background executors for any delegated tasks
                await self._drain_delegated(chat_id)

            except TimeoutError:
                logger.warning("Agent timed out after 300s for chat %s", chat_id)
                try:
                    await status_msg.edit_text("⚠️ Timed out — agent took longer than 10 minutes.")
                except Exception:
                    pass
                self._agents.pop(chat_id, None)
            except Exception as exc:
                logger.exception("Agent error for chat %s", chat_id)
                try:
                    await status_msg.edit_text(f"⚠️ Error: {exc}")
                except Exception:
                    pass
            finally:
                typing_task.cancel()

    async def _drain_delegated(self, chat_id: int) -> None:
        """After turn: execute any delegated tasks via shared execution path."""
        from crow_agent.task_registry import drain_and_execute

        bot = self._app.bot if self._app else None
        if not bot:
            return

        async def tg_deliver(task_id: str, result: str, error: str | None) -> None:
            if error:
                text = f"❌ Task _{task_id}_ failed:\n\n{error}"
            else:
                text = f"✅ Task _{task_id}_ done:\n\n{result[:3000]}"
                try:
                    await bot.send_message(chat_id=chat_id, text=text, message_effect_id="5104841245755180586")
                    return
                except Exception:
                    pass  # effect may not be supported, fall through to normal send
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

        await drain_and_execute(deliver=tg_deliver, background=True)

    async def _handle_message(self, update: Update, _ctx: Any) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return

        text = update.message.text.strip()
        if not text:
            return

        # Include replied-to message as context so "fix this" refers to the right thing
        # ponytail: capture text, captions, and Crow's own responses for full context
        # Structure the replied-to message as a separate context section so the
        # LLM treats it as the primary context (the task to act on), not inline noise.
        if update.message.reply_to_message:
            replied_msg = update.message.reply_to_message
            replied_text = (
                replied_msg.text
                or replied_msg.caption
                or ""
            ).strip()
            if replied_text:
                # Strip HTML tags from Crow's own messages for clean context
                replied_text = re.sub(r"<[^>]+>", "", replied_text)
                # Prepend as clearly labeled section, then user's new instruction
                text = (
                    f"--- Referenced message (user is replying to this) ---\n"
                    f"{replied_text[:500]}\n"
                    f"---\n\n"
                    f"{text}"
                )

        await self._run_agent(update.effective_chat.id, text, update)

    async def _handle_voice(self, update: Update, _ctx: Any) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return

        chat_id = update.effective_chat.id
        voice = update.message.voice
        if not voice:
            return

        caption = update.message.caption or ""
        await update.message.chat.send_action(action="typing")

        # Download voice file
        try:
            tg_file = await voice.get_file()
            ogg_path = Path(f"/tmp/crow_voice_{chat_id}_{voice.file_id}.ogg")
            await tg_file.download_to_drive(ogg_path)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to download voice: {e}")
            return

        # Transcribe
        text = self._transcribe_voice(ogg_path)
        ogg_path.unlink(missing_ok=True)

        if not text:
            await update.message.reply_text("❌ Couldn't transcribe voice. Install whisper: pip install openai-whisper")
            return

        # Include caption as context
        if caption:
            text = f"{caption}\n\n[Voice transcript: \"{text}\"]"
        else:
            text = f"[Voice transcript: \"{text}\"]"

        await self._run_agent(chat_id, text, update)

    def _transcribe_voice(self, ogg_path: Path) -> str:
        """Transcribe .ogg voice file using whisper. Returns empty string on failure."""
        try:
            import whisper
        except ImportError:
            return ""

        # Convert ogg to wav (whisper needs wav)
        import subprocess
        wav_path = ogg_path.with_suffix('.wav')
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-i', str(ogg_path), '-ar', '16000', '-ac', '1', str(wav_path)],
                capture_output=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

        if not wav_path.exists():
            return ""

        try:
            # ponytail: tiny model, fast enough for real-time
            model = whisper.load_model("tiny")
            result = model.transcribe(str(wav_path), language="en")
            wav_path.unlink(missing_ok=True)
            return result.get("text", "").strip()
        except Exception:
            wav_path.unlink(missing_ok=True)
            return ""

    async def _handle_file(self, update: Update, _ctx: Any) -> None:
        uid = update.effective_user.id if update.effective_user else None
        if not self._is_allowed(uid):
            await update.message.reply_text("⛔ Unauthorized")
            return

        chat_id = update.effective_chat.id

        # Determine file type and get Telegram File object
        if update.message.document:
            doc = update.message.document
            tg_file = await doc.get_file()
            fname = doc.file_name or f"doc_{tg_file.file_id}"
            mime = doc.mime_type or "unknown"
        elif update.message.photo:
            photo = update.message.photo[-1]  # largest size
            tg_file = await photo.get_file()
            fname = f"photo_{tg_file.file_id}.jpg"
            mime = "image/jpeg"
        else:
            return

        # Download to memory vault raw sources for future wiki ingest
        # Override via CROW_TELEGRAM_UPLOAD_DIR env var (for read-only deployments)
        upload_root = os.environ.get("CROW_TELEGRAM_UPLOAD_DIR") or (PROJECT_ROOT / "memory vault" / "raw" / "sources" / "telegram")
        upload_dir = Path(upload_root)
        upload_dir.mkdir(parents=True, exist_ok=True)
        local_path = upload_dir / fname
        await tg_file.download_to_drive(str(local_path))

        # Build message for agent: file path + optional caption
        caption = (update.message.caption or "").strip()
        text = f"📎 User sent a file: {local_path} (type: {mime})"
        if caption:
            text += f"\nCaption: {caption}"

        await self._run_agent(chat_id, text, update, status_text="🐦‍⬛ Crow is reading the file...")

    # ── task reminders ──

    async def send_message(self, chat_id: int, text: str) -> None:
        """Send a proactive message to a Telegram chat (cron results, notifications)."""
        if not self._app:
            logger.debug("Telegram not available — dropping message")
            return
        try:
            await self._send_telegram_text(chat_id, text)
        except Exception as exc:
            logger.warning("Failed to send message to %d: %s", chat_id, exc)

    async def send_reminder(self, chat_id: int, task: dict[str, object]) -> None:
        """Send a task reminder with inline action buttons."""
        text = f"📋 <b>{escape(str(task['title']))}</b>"
        if task.get("deadline"):
            text += f"\n⏰ Due: {escape(str(task['deadline']))}"
        if task.get("description"):
            text += f"\n└ {escape(str(task['description']))}"
        keyboard = [
            [InlineKeyboardButton("✅ Done", callback_data=f"task_done:{task['id']}")],
            [
                InlineKeyboardButton("⏰ 30m", callback_data=f"snooze_:30:{task['id']}"),
                InlineKeyboardButton("⏰ 1h", callback_data=f"snooze_:60:{task['id']}"),
            ],
        ]
        try:
            await self._app.bot.send_message(
                chat_id, text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as exc:
            logger.warning("Failed to send reminder to %d: %s", chat_id, exc)

    async def _handle_task_callback(self, update: Update, _ctx: Any) -> None:
        """Handle inline button presses on task reminders."""
        query = update.callback_query
        await query.answer()
        data = query.data
        parts = data.split(":", 2)
        action = parts[0]
        task_id = parts[-1]

        if self._db is None:
            await query.edit_message_text("⚠️ DB not available — task unchanged")
            return

        if action == "task_done":
            task = self._db.get_task(task_id)
            title = task["title"] if task else task_id

            if task and task.get("repeat") and task.get("deadline"):
                new_id = self._db.advance_recurring_task(task_id)
                msg = f"✅ <i>{escape(str(title))}</i> marked done!"
                if new_id:
                    msg += "\n🔄 Next occurrence created"
            else:
                self._db.update_task(task_id, status="done", snoozed_until=None)
                msg = f"✅ <i>{escape(str(title))}</i> marked done!"

            try:
                await query.edit_message_text(msg, parse_mode="HTML")
            except Exception:
                await query.edit_message_text(msg)

        elif action == "snooze_":
            minutes = int(parts[1])
            from datetime import datetime, timedelta, timezone
            snooze_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            self._db.update_task(task_id, snoozed_until=snooze_until)
            await query.edit_message_text(
                f"⏰ Snoozed for {minutes} min. I'll remind you then.",
            )

# ── Crow Log channel for autonomous Initiative output ──
CROW_LOG_CHAT_ID = -1003985785844

async def send_to_crow_log(bot, text: str, initiative_id: str = "") -> bool:
    """Send a message to the Crow Log Telegram channel.
    Returns True on success, False on failure.
    Called by heartbeat._spawn_initiative after autonomous turns.
    """
    try:
        prefix = f"[#{initiative_id}] " if initiative_id else ""
        await bot.send_message(
            chat_id=CROW_LOG_CHAT_ID,
            text=prefix + text,
            parse_mode=None,  # plain text, no markdown
        )
        return True
    except Exception as e:
        logger.warning("Crow Log send failed: %s", e)
        return False


