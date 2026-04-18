"""
Hub SE Agent Test Client — Console REPL that talks to the agent via Redis streams.

Simulates a remote sender (like Teams) by:
  1. Pushing messages to  workiq:inbox:{email}
  2. Reading responses from workiq:outbox:{email}

Uses the same Entra ID credentials and Redis endpoint as the main agent.

Usage:
  cd test-client
  python chat.py
"""

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

from azure.identity import (
    AuthenticationRecord,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)
from dotenv import load_dotenv

# Load .env from parent directory (the main agent's config)
_PARENT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_PARENT_DIR / ".env")

import redis
from redis_entraid.cred_provider import create_from_default_azure_credential


def _decode_jwt_claims(token: str) -> dict:
    """Decode the payload segment of a JWT without verification."""
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.b64decode(payload))


def _create_credential():
    """Create an InteractiveBrowserCredential, reusing the agent's saved auth record."""
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    cache_options = TokenCachePersistenceOptions(name="hub_se_agent")
    auth_record_path = Path.home() / ".hub-se-agent" / "auth_record.json"

    record = None
    if auth_record_path.exists():
        try:
            record = AuthenticationRecord.deserialize(auth_record_path.read_text())
            print(f"  Reusing saved auth record from {auth_record_path}")
        except Exception:
            pass

    return InteractiveBrowserCredential(
        tenant_id=tenant_id,
        cache_persistence_options=cache_options,
        authentication_record=record,
    )


def _resolve_email(credential) -> tuple[str, str]:
    """Get the user's display name and email from an Azure token."""
    token = credential.get_token("https://communication.azure.com/.default")
    claims = _decode_jwt_claims(token.token)
    name = claims.get("name", "Test Client User")
    email = (
        claims.get("upn")
        or claims.get("preferred_username")
        or claims.get("email", "unknown@unknown.com")
    )
    return name, email


def _connect_redis(host: str, port: int):
    """Connect to Azure Managed Redis with Entra ID via redis-entraid credential provider."""
    credential_provider = create_from_default_azure_credential(
        ("https://redis.azure.com/.default",)
    )

    client = redis.RedisCluster(
        host=host,
        port=port,
        ssl=True,
        ssl_cert_reqs=None,
        decode_responses=True,
        credential_provider=credential_provider,
        socket_timeout=10,
        socket_connect_timeout=10,
    )
    client.ping()
    return client


def main():
    print("=" * 50)
    print("  Hub SE Agent Test Client (Redis console)")
    print("=" * 50)
    print()

    # --- Auth ---
    print("[1/4] Authenticating...")
    credential = _create_credential()
    name, email = _resolve_email(credential)
    email_lower = email.lower()
    print(f"  Signed in as: {name} <{email}>")

    # --- Redis connection ---
    endpoint = os.environ.get("AZ_REDIS_CACHE_ENDPOINT")
    if not endpoint:
        print("\nERROR: AZ_REDIS_CACHE_ENDPOINT not set in .env")
        sys.exit(1)

    parts = endpoint.rsplit(":", 1)
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 10000

    print(f"\n[2/4] Connecting to Redis ({host}:{port})...")
    try:
        client = _connect_redis(host, port)
    except Exception as e:
        print(f"\nERROR: Could not connect to Redis: {e}")
        sys.exit(1)
    print("  Connected!")

    # --- Check agent online ---
    agent_key = f"workiq:agents:{email_lower}"
    inbox_key = f"workiq:inbox:{email_lower}"
    outbox_key = f"workiq:outbox:{email_lower}"

    print(f"\n[3/4] Checking agent status ({agent_key})...")
    agent_info = client.get(agent_key)
    if agent_info:
        info = json.loads(agent_info)
        print(f"  Agent ONLINE: {info.get('name', '?')} (since {time.ctime(info.get('started_at', 0))})")
    else:
        print("  WARNING: Agent appears to be OFFLINE (key not found).")
        print("  Messages will be queued in Redis until the agent starts.")

    # --- REPL ---
    print(f"\n[4/4] Ready! Type messages below. Ctrl+C to exit.")
    print(f"  Inbox:  {inbox_key}")
    print(f"  Outbox: {outbox_key}")
    print()

    # Start reading outbox from the latest position
    outbox_last_id = "$"

    try:
        while True:
            try:
                text = input("You > ").strip()
            except EOFError:
                break

            if not text:
                continue

            if text.lower() in ("exit", "quit"):
                break

            # Send message to inbox
            msg_id = uuid.uuid4().hex[:12]
            client.xadd(inbox_key, {
                "sender": "test-client",
                "text": text,
                "ts": str(time.time()),
                "msg_id": msg_id,
            })
            print(f"  Sent (msg_id={msg_id}). Waiting for response...")

            # Poll outbox for the reply
            got_reply = False
            deadline = time.time() + 120  # 2 minute timeout

            while time.time() < deadline:
                try:
                    result = client.xread(
                        {outbox_key: outbox_last_id}, block=5000, count=10
                    )
                except redis.TimeoutError:
                    continue

                if not result:
                    continue

                for _stream, messages in result:
                    for sid, fields in messages:
                        outbox_last_id = sid
                        # Check if this reply matches our message
                        if fields.get("in_reply_to") == msg_id:
                            status = fields.get("status", "?")
                            response = fields.get("text", "(no response)")
                            print()
                            if status == "completed":
                                print(f"Agent > {response}")
                            else:
                                print(f"Agent [ERROR] > {response}")
                            print()
                            got_reply = True
                            break
                if got_reply:
                    break

            if not got_reply:
                print("  (Timed out waiting for response — the agent may still be processing.)\n")

    except KeyboardInterrupt:
        print("\n\nExiting...")
    finally:
        try:
            client.close()
        except Exception:
            pass
        print("Disconnected.")


if __name__ == "__main__":
    main()
