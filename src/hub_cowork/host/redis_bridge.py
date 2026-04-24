"""
Redis Bridge — connects the local agent to Azure Managed Redis for remote
task delivery (e.g. from a Teams relay service).

Key layout (all prefixed by REDIS_NAMESPACE, default `hub-cowork`):
  Inbox stream:  {ns}:inbox:{email}   — remote senders push messages here
  Outbox stream: {ns}:outbox:{email}  — agent pushes results here
  Agent key:     {ns}:agents:{email}  — presence registration with TTL

This fork uses a dedicated namespace so it does not collide with the
original `workiq:*` keys used by the project it was forked from.

Multi-thread behaviour:
  Incoming messages are classified by `agent_core.classify_inbox` into one
  of three kinds:
    - `new`      → create a fresh ConversationThread, route its first
                   message, dispatch to its executor.
    - `existing` → append the message to the matching thread and wake its
                   executor.
    - `system`   → run a cross-cutting system query ("what's running?")
                   against the shared system pseudo-thread; reply inline.

  Agent-to-user messages include the thread's `hitl_correlation_tag`
  (e.g. `#thread-ab12cd`) so users can see which task is speaking in
  Teams. Outbound stream payloads also carry a `thread_id` field for
  external relays that want to track correlations client-side.

Optional — only active when AZ_REDIS_CACHE_ENDPOINT is configured.
"""

import json
import logging
import os
import threading
import time
import uuid

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry
from redis.exceptions import (
    BusyLoadingError,
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)
from redis_entraid.cred_provider import EntraIdCredentialsProvider
from redis_entraid.identity_provider import DefaultAzureCredentialProvider
from redis.auth.token_manager import TokenManagerConfig, RetryPolicy

logger = logging.getLogger("hub_se_agent")


def _svc_mark(status: str, detail: str = "") -> None:
    """Update the ``redis_teams`` service tile. Best-effort — never raises."""
    try:
        from hub_cowork.core.service_status import get_monitor
        get_monitor().mark("redis_teams", status, detail)  # type: ignore[arg-type]
    except Exception:
        pass


DEFAULT_NAMESPACE = "hub-cowork"

# Initial connect retry budget. For an interactive desktop agent, ~30 seconds
# covers genuine transient blips (Redis rolling restart, brief network hiccup,
# DNS flake) without making the user wait minutes for a misconfigured endpoint
# to be detected. If we can't connect inside this budget, the bridge falls
# through to local-only mode and the user can restart after fixing config.
#   delays: 2s, 4s, 8s, 15s  → ~29s worst case over 4 attempts.
_INITIAL_CONNECT_MAX_ATTEMPTS = 4
_INITIAL_CONNECT_BASE_DELAY = 2.0
_INITIAL_CONNECT_MAX_DELAY = 15.0


class RedisBridge:
    """Polls a Redis inbox stream for remote tasks and writes results to an outbox stream."""

    def __init__(self, user_email: str, user_name: str,
                 endpoint: str, credential, ttl: int = 86400,
                 namespace: str | None = None):
        self._credential = credential  # shared InteractiveBrowserCredential
        self._user_email = user_email.lower()
        self._user_name = user_name
        self._ttl = ttl
        self._stopping = threading.Event()

        # Parse host:port from endpoint
        parts = endpoint.rsplit(":", 1)
        self._host = parts[0]
        self._port = int(parts[1]) if len(parts) > 1 else 10000

        # Namespace — overridable per instance, otherwise from env, otherwise default.
        self._namespace = (
            namespace
            or os.environ.get("REDIS_NAMESPACE")
            or DEFAULT_NAMESPACE
        ).strip()
        if not self._namespace:
            self._namespace = DEFAULT_NAMESPACE

        # Stream / key names
        self._inbox_key = f"{self._namespace}:inbox:{self._user_email}"
        self._outbox_key = f"{self._namespace}:outbox:{self._user_email}"
        self._agent_key = f"{self._namespace}:agents:{self._user_email}"

        # Track request_id → (inbox msg_id, thread_id) for reply correlation
        self._pending_replies: dict[str, tuple[str, str]] = {}
        self._pending_lock = threading.Lock()

        self._client: redis.RedisCluster | None = None
        self._on_broadcast = None  # set by start()
        self._poller_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

        logger.info("Redis bridge configured (namespace=%s, inbox=%s)",
                    self._namespace, self._inbox_key)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Create the Redis cluster connection using the shared credential.

        Built-in retry: the client retries on ConnectionError / TimeoutError /
        BusyLoadingError using exponential backoff so transient blips during
        normal operation are absorbed without surfacing to callers.
        """
        # Wrap the shared InteractiveBrowserCredential (silent refresh via cached auth record)
        # instead of DefaultAzureCredential (which spawns az CLI processes → cmd windows)
        idp = DefaultAzureCredentialProvider(
            app=self._credential,
            scopes=("https://redis.azure.com/.default",),
        )
        token_mgr_config = TokenManagerConfig(
            0.7, 0, 100, RetryPolicy(3, 3)
        )
        credential_provider = EntraIdCredentialsProvider(idp, token_mgr_config)

        # SDK-level retry: 3 attempts, exponential backoff (1s, 2s, 4s, capped 10s)
        # on connection / timeout / busy-loading errors. Applied to every command.
        retry = Retry(
            backoff=ExponentialBackoff(cap=10, base=1),
            retries=3,
            supported_errors=(
                RedisConnectionError,
                RedisTimeoutError,
                BusyLoadingError,
            ),
        )

        self._client = redis.RedisCluster(
            host=self._host,
            port=self._port,
            ssl=True,
            ssl_cert_reqs=None,
            decode_responses=True,
            credential_provider=credential_provider,
            socket_timeout=10,
            socket_connect_timeout=10,
            retry=retry,
        )
        self._client.ping()
        self._connected_at = time.time()
        logger.info("Redis bridge connected to %s:%d (credential_provider, retry=3)",
                     self._host, self._port)

    _MAX_CONNECTION_AGE = 1800  # force reconnect every 30 minutes

    def _ensure_connected(self):
        """Reconnect if the client is missing or the connection is stale."""
        age = time.time() - getattr(self, '_connected_at', 0)
        if self._client is None or age > self._MAX_CONNECTION_AGE:
            if self._client is not None:
                logger.info("Redis connection stale (%.0fs) — forcing reconnect", age)
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self._connect()

    def _ping_or_reconnect(self):
        """Verify the connection is alive; reconnect if not."""
        try:
            self._ensure_connected()
            self._client.ping()
        except Exception as e:
            logger.warning("Redis PING failed (%s) — reconnecting", e)
            self._client = None
            self._connect()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _connect_with_retry(self) -> bool:
        """Try the initial connect with exponential backoff.

        Returns True on success, False if all attempts were exhausted (in
        which case the bridge stays disabled and the agent runs in
        local-only mode).
        """
        delay = _INITIAL_CONNECT_BASE_DELAY
        for attempt in range(1, _INITIAL_CONNECT_MAX_ATTEMPTS + 1):
            if self._stopping.is_set():
                return False
            try:
                self._connect()
                if attempt > 1:
                    logger.info(
                        "Redis bridge connected on attempt %d/%d",
                        attempt, _INITIAL_CONNECT_MAX_ATTEMPTS,
                    )
                return True
            except Exception as e:
                if attempt >= _INITIAL_CONNECT_MAX_ATTEMPTS:
                    logger.error(
                        "Redis bridge initial connect failed after %d attempts (%s) — "
                        "running in local-only mode",
                        attempt, e,
                    )
                    _svc_mark("down", f"connect failed after {attempt} attempts: {e}")
                    return False
                logger.warning(
                    "Redis bridge connect attempt %d/%d failed (%s) — retrying in %.1fs",
                    attempt, _INITIAL_CONNECT_MAX_ATTEMPTS, e, delay,
                )
                # Wait but stay responsive to shutdown signals.
                if self._stopping.wait(timeout=delay):
                    return False
                delay = min(delay * 2, _INITIAL_CONNECT_MAX_DELAY)
        return False

    def start(self, on_broadcast=None):
        """Start the bridge: connect (with retry on a worker thread), register, begin polling.

        The initial connect can take a while (token acquisition + DNS +
        TLS + cluster discovery) and may transiently fail, so we run it in
        a background worker. The poller and heartbeat threads are started
        only once the connection succeeds.
        """
        self._on_broadcast = on_broadcast

        def _bootstrap():
            if not self._connect_with_retry():
                return  # gave up — stay in local-only mode

            self._register_agent()

            self._poller_thread = threading.Thread(
                target=self._poll_inbox, daemon=True, name="redis-inbox-poller",
            )
            self._poller_thread.start()

            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="redis-heartbeat",
            )
            self._heartbeat_thread.start()

            logger.info("Redis bridge started (namespace=%s, inbox=%s)",
                        self._namespace, self._inbox_key)

        threading.Thread(
            target=_bootstrap, daemon=True, name="redis-bootstrap",
        ).start()

    def stop(self):
        """Signal the bridge to stop."""
        self._stopping.set()
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        _svc_mark("down", "bridge stopped")
        logger.info("Redis bridge stopped")

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def _register_agent(self):
        """Register / refresh this agent's presence in Redis.

        Successful registration is the moment the Teams relay can reach us,
        so this is also when the ``redis_teams`` status tile flips green.
        Failure here means we're connected to Redis but couldn't publish
        presence — still a broken channel from the Teams side.
        """
        try:
            self._ensure_connected()
            info = json.dumps({
                "name": self._user_name,
                "email": self._user_email,
                "started_at": time.time(),
                "version": "1.0",
            })
            self._client.set(self._agent_key, info, ex=self._ttl)
            logger.info("Agent registered: %s (TTL=%ds)", self._agent_key, self._ttl)
            _svc_mark("ok", "")
        except Exception as e:
            logger.error("Agent registration failed: %s", e)
            _svc_mark("down", f"presence registration failed: {e}")

    def _heartbeat_loop(self):
        """Refresh the agent registration key every 30 minutes."""
        while not self._stopping.wait(timeout=1800):
            try:
                self._register_agent()
            except Exception as e:
                logger.warning("Heartbeat refresh failed: %s", e)

    # ------------------------------------------------------------------
    # Inbox poller
    # ------------------------------------------------------------------

    def _poll_inbox(self):
        """Block-read from the inbox stream, dispatching messages to the task queue.

        On connection loss we drop into an exponential-backoff reconnect loop
        rather than retrying every 5s indefinitely (which floods logs and can
        keep the server under load).
        """
        last_id = "$"  # only new messages from this point forward
        reconnect_delay = _INITIAL_CONNECT_BASE_DELAY
        was_disconnected = False
        last_keepalive_mark = 0.0
        while not self._stopping.is_set():
            try:
                self._ensure_connected()
                # XREAD with 5-second block timeout
                result = self._client.xread(
                    {self._inbox_key: last_id}, block=5000, count=10
                )
                # Successful round-trip — reset reconnect backoff.
                reconnect_delay = _INITIAL_CONNECT_BASE_DELAY
                # If we previously flipped the tile to "down" because of a
                # transient blip, restore it to "ok" now that we're talking
                # to Redis again. Without this the dot stays red until the
                # 30-minute heartbeat re-registers the presence key.
                if was_disconnected:
                    _svc_mark("ok", "")
                    was_disconnected = False
                # Keep-alive: refresh the "checked at" timestamp on the
                # service tile every ~60s. The active probe loop skips
                # redis_teams, so without this the tooltip would freeze at
                # the time of the last status transition.
                now = time.time()
                if now - last_keepalive_mark >= 60.0:
                    _svc_mark("ok", "")
                    last_keepalive_mark = now
                if not result:
                    continue

                for _stream, messages in result:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        self._handle_inbox_message(msg_id, fields)

            except (RedisConnectionError, RedisTimeoutError, BusyLoadingError) as e:
                logger.warning(
                    "Redis connection lost (%s) — reconnecting in %.1fs",
                    e, reconnect_delay,
                )
                _svc_mark("down", f"connection lost: {e}")
                was_disconnected = True
                self._client = None
                self._stopping.wait(timeout=reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _INITIAL_CONNECT_MAX_DELAY)
            except Exception as e:
                logger.error("Inbox poll error: %s", e, exc_info=True)
                self._stopping.wait(timeout=5)

    def _handle_inbox_message(self, stream_id: str, fields: dict):
        """Process a single inbox message using the multi-thread model.

        Uses `classify_inbox` to decide whether this message:
          - starts a new ConversationThread,
          - continues an existing one (reply / confirmation), or
          - is a cross-cutting system query.
        """
        text = fields.get("text", "").strip()
        sender = fields.get("sender", "remote")
        msg_id = fields.get("msg_id", stream_id)
        hinted_thread_id = fields.get("thread_id", "").strip() or None

        if not text:
            logger.warning("Empty inbox message %s — skipping", stream_id)
            return

        logger.info("Remote message from %s (msg_id=%s, thread_hint=%s): %.80s",
                    sender, msg_id, hinted_thread_id, text)

        # Show the remote user's message in the local chat UI (global notice).
        if self._on_broadcast:
            self._on_broadcast({
                "type": "remote_message",
                "sender": sender,
                "text": text,
            })

        # Lazy imports to avoid cyclic deps at module load time.
        from hub_cowork.core.agent_core import classify_inbox, route
        from hub_cowork.core.thread_manager import get_manager
        from hub_cowork.core.thread_executor import get_pool

        tm = get_manager()
        pool = get_pool()

        # Consider only this sender's active threads when classifying.
        active = tm.list(
            external_user=sender,
            statuses=("active", "running", "awaiting_user", "completed"),
        )
        summaries = [t.summary() for t in active]

        # If the relay explicitly hinted a thread_id and it's live, honor it
        # without re-classifying (fast path for HITL replies).
        if hinted_thread_id and any(s.get("id") == hinted_thread_id for s in summaries):
            decision = {"kind": "existing", "thread_id": hinted_thread_id}
            logger.info("[InboxRouter] Honouring thread_id hint=%s", hinted_thread_id)
        else:
            decision = classify_inbox(text, summaries)

        kind = decision.get("kind", "new")

        if kind == "system":
            self._handle_system_query(text, sender, msg_id)
            return

        if kind == "existing":
            thread_id = decision.get("thread_id")
            thread = tm.get(thread_id) if thread_id else None
            if thread is None:
                logger.warning("Classifier said 'existing' but thread missing — falling back to new")
                kind = "new"
            else:
                # A completed thread re-awakens as active when the user replies.
                if thread.status not in ("running", "awaiting_user"):
                    tm.set_status(thread.id, "active")
                request_id = uuid.uuid4().hex[:8]
                with self._pending_lock:
                    self._pending_replies[request_id] = (msg_id, thread.id)
                pool.submit(thread.id, text, request_id=request_id)
                return

        # kind == "new" — gate: only one in-flight remote task per Teams user.
        # If this sender already has a thread in `running` or `awaiting_user`,
        # politely refuse the new task. SYSTEM queries and replies to existing
        # threads are still allowed (handled above). Local UI-initiated
        # threads do NOT count toward the cap.
        in_flight = [
            t for t in active
            if t.source == "remote"
            and t.external_user == sender
            and t.status in ("running", "awaiting_user")
        ]
        if in_flight:
            blocker = max(in_flight, key=lambda t: t.updated_at)
            status_word = (
                "is still in progress" if blocker.status == "running"
                else "is waiting for your reply"
            )
            logger.info(
                "[InboxRouter] Rejecting new task from %s — blocker thread %s (status=%s)",
                sender, blocker.id, blocker.status,
            )
            self._write_outbox(
                text=(
                    f"I can't start a new task right now — "
                    f"\"{blocker.title[:60]}\" ({blocker.hitl_correlation_tag}) "
                    f"{status_word}. Please reply to that thread to continue "
                    f"it, or wait for it to finish before sending a new "
                    f"request. You can also start additional tasks directly "
                    f"on your laptop."
                ),
                in_reply_to=msg_id,
                thread_id=blocker.id,
                status="rejected",
            )
            return

        # Cleared the gate — create a fresh thread, set its skill, dispatch.
        title = text[:60]
        thread = tm.create(title=title, source="remote", external_user=sender)
        try:
            skill_name = route(text)
            if skill_name and skill_name != "none":
                tm.set_skill(thread.id, skill_name)
        except Exception as e:
            logger.error("Router failed for remote message: %s", e, exc_info=True)

        # Background LLM-derived title (does not block the dispatch).
        def _retitle(tid: str, msg: str) -> None:
            try:
                from hub_cowork.core.agent_core import generate_thread_title
                new_title = generate_thread_title(msg)
                if new_title:
                    tm.update_title(tid, new_title)
            except Exception as e:
                logger.warning("Background title gen failed for %s: %s", tid, e)
        threading.Thread(target=_retitle, args=(thread.id, text), daemon=True).start()

        request_id = uuid.uuid4().hex[:8]
        with self._pending_lock:
            self._pending_replies[request_id] = (msg_id, thread.id)
        pool.submit(thread.id, text, request_id=request_id)

    # ------------------------------------------------------------------
    # System queries (cross-thread)
    # ------------------------------------------------------------------

    def _handle_system_query(self, text: str, sender: str, msg_id: str):
        """Run a system query (e.g. 'what's running?') inline and reply.

        Uses the `task_status` skill if present; otherwise a short synthesized
        summary of active threads. Never creates a new thread.
        """
        from hub_cowork.core.agent_core import get_skill, _run_none_skill, get_responses_client, CHAT_MODEL
        from hub_cowork.core.thread_manager import get_manager

        tm = get_manager()
        active = tm.list(
            external_user=sender,
            statuses=("active", "running", "awaiting_user", "completed"),
        )
        # Build a short description for the LLM.
        if not active:
            summary_text = "No active tasks right now."
        else:
            lines = ["Here is the status of your active tasks:"]
            for t in active:
                age = int((time.time() - t.created_at) / 60)
                lines.append(
                    f"- {t.hitl_correlation_tag} [{t.status}] "
                    f"skill={t.skill_name or 'pending'} "
                    f"\"{t.title[:60]}\" ({age} min old)"
                )
            summary_text = "\n".join(lines)

        # Run a short LLM rephrase so the reply is conversational.
        try:
            client = get_responses_client()
            resp = client.responses.create(
                model=CHAT_MODEL,
                instructions=(
                    "You are Hub SE Agent replying to a user via Teams. The user "
                    "asked a status/system question. You are given a structured "
                    "summary of their active tasks — rephrase it into a short, "
                    "natural reply. Preserve every correlation tag "
                    "(e.g. #thread-xxxx) exactly as given so the user can quote "
                    "them back to continue specific threads."
                ),
                input=[{
                    "role": "user",
                    "content": f"User asked: {text}\n\nTasks:\n{summary_text}",
                }],
                tools=[],
            )
            reply = ""
            for item in resp.output:
                if item.type == "message":
                    for part in item.content:
                        if part.type == "output_text":
                            reply += part.text
            if not reply.strip():
                reply = summary_text
        except Exception as e:
            logger.warning("System query rephrase failed: %s — sending raw", e)
            reply = summary_text

        self._write_outbox(
            text=reply,
            in_reply_to=msg_id,
            thread_id="system",
            status="completed",
        )

    def _run_system_task_remote(self, *args, **kwargs):
        """Removed — replaced by `_handle_system_query` in the thread model."""
        raise NotImplementedError(
            "Remote system tasks now flow through _handle_system_query"
        )

    # ------------------------------------------------------------------
    # Outbox writer (called by ThreadExecutor callbacks)
    # ------------------------------------------------------------------

    def _write_outbox(self, text: str, in_reply_to: str, thread_id: str,
                      status: str, request_id: str = ""):
        """Low-level outbox writer with ping + retry."""
        payload = {
            "request_id": request_id,
            "thread_id": thread_id,
            "status": status,
            "text": text[:4000],
            "ts": str(time.time()),
            "in_reply_to": in_reply_to,
        }
        for attempt in range(2):
            try:
                self._ping_or_reconnect()
                self._client.xadd(self._outbox_key, payload)
                self._client.xtrim(self._outbox_key, maxlen=100, approximate=True)
                logger.info("Outbox reply (thread=%s status=%s in_reply_to=%s attempt=%d)",
                            thread_id, status, in_reply_to, attempt + 1)
                return
            except Exception as e:
                logger.warning("Outbox write attempt %d failed: %s", attempt + 1, e)
                self._client = None
        logger.error("Failed to write outbox for thread %s after 2 attempts", thread_id)

    def on_thread_reply(self, thread_id: str, request_id: str,
                        text: str, status: str):
        """Called by ThreadExecutor when a remote-sourced thread has a reply.

        `status` is one of: `completed`, `failed`, `awaiting_user`.
        The reply text is prefixed with the thread's correlation tag so the
        Teams user sees which task is speaking.
        """
        from hub_cowork.core.thread_manager import get_manager

        tm = get_manager()
        thread = tm.get(thread_id)
        if thread is None:
            logger.warning("on_thread_reply: thread %s not found", thread_id)
            return

        # Only relay replies for remote-sourced threads.
        if thread.source != "remote":
            return

        with self._pending_lock:
            entry = self._pending_replies.pop(request_id, None)
        in_reply_to = entry[0] if entry else ""

        # Prefix the correlation tag so the user knows which thread replied.
        tagged = f"{thread.hitl_correlation_tag} {text}".strip()

        self._write_outbox(
            text=tagged,
            in_reply_to=in_reply_to,
            thread_id=thread_id,
            status=status,
            request_id=request_id,
        )
