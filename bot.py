from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.conversation import build_proactive_message, next_action
from app.dataset_loader import seed_context_store
from app.store import contexts, conversations, get_context, get_or_create_conversation, save_context

app = FastAPI(title="magicpin AI Assistant")
START_TIME = time.time()

# Seed the in-memory context store with the provided challenge dataset on startup.
DATASET_COUNTS = seed_context_store()


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any] = Field(default_factory=dict)
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.get("/v1/healthz")
def healthz() -> dict[str, Any]:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _), _ in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "team_name": "Team Alpha",
        "team_members": ["Alice", "Bob"],
        "model": "gpt-4o-mini",
        "approach": "rules-based merchant composer with stateful conversation handling",
        "contact_email": "team@example.com",
        "version": "0.1.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
def push_context(body: ContextBody):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return JSONResponse(
            {"accepted": False, "reason": "invalid_scope", "details": f"scope={body.scope!r} not allowed"},
            status_code=400,
        )

    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] >= body.version:
        return JSONResponse(
            {"accepted": False, "reason": "stale_version", "current_version": current["version"]},
            status_code=409,
        )

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
def tick(body: TickBody):
    actions: list[dict[str, Any]] = []
    for trigger_id in body.available_triggers:
        trigger_ctx = get_context("trigger", trigger_id)
        if not trigger_ctx:
            continue
        trigger = trigger_ctx["payload"]
        merchant_id = trigger.get("merchant_id")
        merchant = get_context("merchant", merchant_id)["payload"] if merchant_id else None
        category_slug = merchant.get("category_slug") if merchant else None
        category = get_context("category", category_slug)["payload"] if category_slug else None
        if not merchant or not category:
            continue

        msg = build_proactive_message(category, merchant, trigger)
        merchant_name = merchant.get("identity", {}).get("name") or merchant_id
        offer_titles = [item.get("title") for item in merchant.get("offers", []) if item.get("status") == "active"]
        active_offer = offer_titles[0] if offer_titles else "your offer"

        actions.append(
            {
                "conversation_id": f"conv_{merchant_id}_{trigger_id}",
                "merchant_id": merchant_id,
                "customer_id": None,
                "send_as": "vera",
                "trigger_id": trigger_id,
                "template_name": "vera_generic_v1",
                "template_params": [merchant_name, active_offer, category.get("slug", "business")],
                "body": msg["body"],
                "cta": msg["cta"],
                "suppression_key": trigger.get("suppression_key", f"{merchant_id}:{trigger_id}"),
                "rationale": msg["rationale"],
            }
        )

    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: ReplyBody):
    state = get_or_create_conversation(body.conversation_id, body.merchant_id, body.customer_id)
    state.add_turn(body.from_role, body.message)

    merchant = None
    category = None
    if body.merchant_id:
        merchant_ctx = get_context("merchant", body.merchant_id)
        if merchant_ctx:
            merchant = merchant_ctx["payload"]
            category_slug = merchant.get("category_slug")
            if category_slug:
                category_ctx = get_context("category", category_slug)
                if category_ctx:
                    category = category_ctx["payload"]

    result = next_action(state, merchant, category, body.message, body.turn_number)
    state.last_action = result.get("action")
    if result.get("action") == "send" and result.get("body"):
        state.record_bot_send(result["body"])
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:app", host="0.0.0.0", port=8080, reload=False)
