from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    bot_sent_bodies: list[str] = field(default_factory=list)
    auto_reply_streak: int = 0
    soft_nudge_used: bool = False
    last_action: Optional[str] = None

    def add_turn(self, role: str, body: str) -> None:
        self.turns.append({"from": role, "body": body})

    def record_bot_send(self, body: str) -> None:
        if body and body not in self.bot_sent_bodies:
            self.bot_sent_bodies.append(body)


contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, ConversationState] = {}


def get_context(scope: str, context_id: str) -> Optional[dict[str, Any]]:
    return contexts.get((scope, context_id))


def save_context(scope: str, context_id: str, version: int, payload: dict[str, Any]) -> dict[str, Any]:
    key = (scope, context_id)
    current = contexts.get(key)
    if current and current["version"] >= version:
        return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    contexts[key] = {"version": version, "payload": payload}
    return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": ""}


def get_or_create_conversation(conversation_id: str, merchant_id: Optional[str] = None, customer_id: Optional[str] = None) -> ConversationState:
    state = conversations.get(conversation_id)
    if state is None:
        state = ConversationState(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
        )
        conversations[conversation_id] = state
    if merchant_id:
        state.merchant_id = merchant_id
    if customer_id:
        state.customer_id = customer_id
    return state
