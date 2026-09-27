"""
Handles POST /v1/reply — the merchant's (or customer's) reply to a
previous bot message, and decides the bot's next move: send / wait / end.

Covers the "open challenges" and Phase-4 replay scenarios called out in
challenge-brief.md §12 and challenge-testing-brief.md §4 Phase 4:
  1. Auto-reply detection (WhatsApp Business canned replies burn turns).
  2. Intent transitions (merchant says "let's do it" -> switch to action,
     don't ask another qualifying question).
  3. Hostile / off-topic handling (stay polite, stay on-mission, know when
     to stop).
  4. Graceful exit after repeated non-answers or explicit not-interested.
  5. Anti-repetition -- never resend the same body verbatim in the same
     conversation.
"""

from __future__ import annotations

import re
from typing import Optional

from .store import ConversationState

# --- phrase banks -----------------------------------------------------------

AUTO_REPLY_PATTERNS = [
    r"thank you for (contacting|reaching out|your message)",
    r"we (will|shall) (get back|respond|reply) (to you )?shortly",
    r"team (tak pahuncha|will (get back|revert))",
    r"aapki (jaankari|baaton?)? ?ke liye (bahut[- ]?)?shukriya",
    r"this is an automated (reply|response|message)",
    r"i am an automated assistant",
    r"main ek automated assistant",
    r"currently (unavailable|away|closed)",
    r"business hours (are|:)",
    r"out of office",
]

NOT_INTERESTED_PATTERNS = [
    r"\bnot interested\b",
    r"\bno thanks?\b",
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"do(n'?t| not) (message|contact|text) me",
    r"\bleave me alone\b",
    r"\bremove me\b",
]

HOSTILE_PATTERNS = [
    r"\bspam\b",
    r"\buseless\b",
    r"\bshut up\b",
    r"\bidiot\b",
    r"\bstupid\b",
    r"\bharass",
    r"f[\*u]ck",
    r"\bnonsense\b",
    r"\bwaste of time\b",
]

INTENT_COMMIT_PATTERNS = [
    r"\blet'?s do it\b",
    r"\bok(ay)? let'?s\b",
    r"\bgo ahead\b",
    r"\bsure,? do it\b",
    r"\byes,? (send|do|proceed|confirm)\b",
    r"\bproceed\b",
    r"\bconfirm(ed)?\b",
    r"\bi want to join\b",
    r"\bmujhe .*(judrna|join) hai\b",
    r"\bwhat'?s next\b",
    r"\bstart(ed)? kar\b",
]

WAIT_PATTERNS = [
    (r"\bcall me later\b|\bbusy (right now|now)\b|\bnot now\b|\bgive me (some )?time\b|\blater\b", 1800),
    (r"\bcall you back\b|\bwill check\b|\bin a meeting\b", 3600),
]

QUALIFYING_STARTERS = (
    "would you", "do you want", "can you tell", "what if", "how about",
    "are you interested", "would that",
)


def _matches_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_auto_reply(cs: ConversationState, message: str) -> bool:
    if _matches_any(AUTO_REPLY_PATTERNS, message):
        return True
    # Same message verbatim 2+ times already in this conversation from the
    # counterpart => treat as canned auto-reply per the brief's own hint.
    # NOTE: `cs.turns` already includes the *current* incoming message (it's
    # appended by the caller before this check runs), so we must exclude
    # that last entry when counting prior occurrences -- otherwise every
    # first message would incorrectly count as matching "itself".
    norm = normalize(message)
    history = cs.turns[:-1] if cs.turns and cs.turns[-1].get("body") == message else cs.turns
    prior_same = sum(
        1 for t in history if t["from"] != "bot" and normalize(t["body"]) == norm
    )
    return prior_same >= 1  # this message + >=1 prior identical = 2+ total


def is_not_interested(message: str) -> bool:
    return _matches_any(NOT_INTERESTED_PATTERNS, message)


def is_hostile(message: str) -> bool:
    return _matches_any(HOSTILE_PATTERNS, message)


def is_intent_commit(message: str) -> bool:
    return _matches_any(INTENT_COMMIT_PATTERNS, message)


def wait_seconds_for(message: str) -> Optional[int]:
    t = message.lower()
    for pattern, secs in WAIT_PATTERNS:
        if re.search(pattern, t):
            return secs
    return None


def has_offtopic_question(message: str) -> bool:
    return "?" in message and not is_hostile(message)


def dedupe_body(cs: ConversationState, body: str) -> str:
    """If we've already sent this exact body in this conversation, nudge it
    so we never repeat verbatim (anti-repetition penalty in the rubric)."""
    if body not in cs.bot_sent_bodies:
        return body
    return body.rstrip(".") + " — just checking this landed okay."


def _merchant_name(merchant: Optional[dict]) -> str:
    if not merchant:
        return "there"
    identity = merchant.get("identity", {}) or {}
    return identity.get("owner_first_name") or identity.get("name") or "there"


def _active_offer(merchant: Optional[dict], category: Optional[dict]) -> str:
    if merchant:
        for offer in merchant.get("offers", []) or []:
            if offer.get("status") == "active":
                return offer.get("title") or "your offer"
    if category:
        catalog = category.get("offer_catalog", []) or []
        if catalog:
            first = catalog[0]
            if isinstance(first, dict):
                return first.get("title") or "your offer"
    return "a relevant offer"


def _digest_fact(category: Optional[dict]) -> Optional[dict]:
    if not category:
        return None
    digest = category.get("digest", []) or []
    for item in digest:
        if item.get("kind") == "research":
            return item
    return digest[0] if digest else None


def _specific_next_step(merchant: Optional[dict], category: Optional[dict]) -> str:
    offer = _active_offer(merchant, category)
    location = ""
    if merchant:
        identity = merchant.get("identity", {}) or {}
        if identity.get("locality"):
            location = f" for {identity.get('locality')}"
    if category and category.get("slug") == "dentists":
        return f" I can draft a quick recall message around {offer}{location}."
    return f" I can prep the next step around {offer}{location}."


def build_proactive_message(category: Optional[dict], merchant: Optional[dict], trigger: Optional[dict]) -> dict:
    """Construct a stronger, data-aware proactive message for /v1/tick."""
    if not merchant:
        return {
            "body": "Hi there — I’ve got a relevant idea for your business based on current activity. Want me to take a look?",
            "cta": "binary",
            "rationale": "Generic fallback when merchant context is missing.",
        }

    name = _merchant_name(merchant)
    offer = _active_offer(merchant, category)
    identity = merchant.get("identity", {}) or {}
    locality = identity.get("locality") or identity.get("city") or "your area"
    performance = merchant.get("performance", {}) or {}
    ctr = performance.get("ctr")
    lapsed = (merchant.get("customer_aggregate", {}) or {}).get("lapsed_180d_plus")
    kind = (trigger or {}).get("kind", "scheduled")
    category_slug = (category or {}).get("slug", "business")

    if kind == "research_digest":
        digest = _digest_fact(category)
        fact = digest.get("title", "the latest research") if digest else "the latest research"
        source = digest.get("source", "a fresh update") if digest else "a fresh update"
        body = (
            f"{name}, this is relevant for {locality}: “{fact}” ({source}). "
            f"It fits your clinic mix and {offer} is a practical next step. Want me to draft a quick patient recall message?"
        )
        cta = "binary"
        rationale = "Research digest with verifiable clinical fact + merchant-specific offer + clear yes/no CTA."
    elif kind == "recall_due":
        body = (
            f"{name}, your patient follow-up window is opening up — {lapsed or 0} patients are now beyond 180 days. "
            f"That makes {offer} a smart re-engagement angle. Want me to send the recall message?"
        )
        cta = "binary"
        rationale = "Recall trigger uses actual patient lapse data to create a direct action prompt."
    elif kind == "perf_spike":
        body = (
            f"{name}, your activity is trending up this week ({performance.get('views', 'stronger visibility')} views in 30d). "
            f"That makes {offer} the right offer to push now. Want me to draft the next version?"
        )
        cta = "binary"
        rationale = "Performance spike makes timing relevant and supports a focused offer."
    elif kind == "perf_dip":
        body = (
            f"{name}, your recent lead flow has softened. The simplest fix is to restart with a sharper offer around {offer}. "
            f"Should I draft a fresh version for {locality}?"
        )
        cta = "binary"
        rationale = "Recovery prompt tied to merchant actual performance dip and location-specific offer."
    else:
        body = (
            f"{name}, I’ve got a relevant {category_slug} idea for {locality}. "
            f"A specific offer like {offer} is much stronger than a generic discount. Want me to prep it?"
        )
        cta = "open_ended"
        rationale = "Generic but contextual message grounded in merchant category and locality."

    return {"body": body, "cta": cta, "rationale": rationale}


def next_action(cs: ConversationState, merchant: Optional[dict], category: Optional[dict],
                 message: str, turn_number: int) -> dict:
    """Core decision function for /v1/reply. Returns the response dict."""

    # 1) Hard stop: explicit not-interested / opt-out.
    if is_not_interested(message):
        return {
            "action": "end",
            "rationale": "Merchant/customer signaled not interested or asked to stop; exiting gracefully without further nudges.",
        }

    # 2) Hostile handling — apologize once, decline unrelated asks politely,
    #    and stop future nudges rather than pushing the original pitch.
    if is_hostile(message):
        if has_offtopic_question(message):
            body = (
                "Sorry about that — I'll stop the nudges here. "
                "That's outside what I can help with from this account, but happy to leave you be otherwise."
            )
            return {
                "action": "send",
                "body": dedupe_body(cs, body),
                "cta": "none",
                "rationale": "Hostile + off-topic ask; apologized, stayed on-mission, declined the unrelated request politely, signaling this is the last message.",
            }
        return {
            "action": "end",
            "rationale": "Hostile message with no further engagement path; ending the conversation politely rather than pushing back.",
        }

    # 3) Auto-reply detection — try exactly one soft human-check nudge, then exit.
    if is_auto_reply(cs, message):
        cs.auto_reply_streak += 1
        if cs.soft_nudge_used or cs.auto_reply_streak >= 2:
            return {
                "action": "end",
                "rationale": "Detected repeated canned auto-reply text; already tried one direct nudge, exiting to avoid burning further turns.",
            }
        cs.soft_nudge_used = True
        body = "Understood — before this goes to the team, want to take a quick look yourself? Takes 2 minutes."
        return {
            "action": "send",
            "body": dedupe_body(cs, body),
            "cta": "binary",
            "rationale": "Detected likely WhatsApp Business auto-reply; using the one allowed direct nudge before exiting if it repeats.",
        }
    else:
        cs.auto_reply_streak = 0

    # 4) Intent transition — merchant has committed, switch straight to action.
    if is_intent_commit(message):
        specific = _specific_next_step(merchant, category)
        name = _merchant_name(merchant)
        body = f"{name}, done — I’ll move ahead with the offer and follow-up.{specific}"
        return {
            "action": "send",
            "body": dedupe_body(cs, body.replace("  ", " ")),
            "cta": "none",
            "rationale": "Merchant expressed explicit commitment; switching directly to action mode instead of re-qualifying, and adding one concrete next-best-step from real context.",
        }

    # 5) Explicit "give me time" -> back off instead of pushing again.
    ws = wait_seconds_for(message)
    if ws:
        return {
            "action": "wait",
            "wait_seconds": ws,
            "rationale": "Merchant asked for time; backing off rather than re-prompting immediately.",
        }

    # 6) Too many turns without resolution -> exit gracefully.
    if turn_number >= 6:
        return {
            "action": "end",
            "rationale": "Reached the turn budget for this conversation without a clear resolution; exiting gracefully.",
        }

    # 7) Default: engaged reply. Acknowledge what they said and advance one step.
    name = _merchant_name(merchant)
    offer = _active_offer(merchant, category)
    location = ""
    if merchant:
        identity = merchant.get("identity", {}) or {}
        if identity.get("locality"):
            location = f" around {identity.get('locality')}"
    body = (
        f"{name}, got it — I’ll use {offer} as the concrete follow-up{location}. "
        f"Want me to keep it focused on the next best patient or offer angle?"
    )
    return {
        "action": "send",
        "body": dedupe_body(cs, body),
        "cta": "open_ended",
        "rationale": "Engaged, substantive reply; acknowledged and advanced with a specific offer and a low-friction follow-up.",
    }