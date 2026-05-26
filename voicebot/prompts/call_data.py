"""
Build the system prompt by templating pd_si.py with call metadata.

Call metadata comes from the Asterisk dialplan / job spawn — passed in as a
dict at runtime instead of being hardcoded. The placeholder DEFAULT_CALL_DATA
is provided only for local WebRTC dev runs.
"""
from __future__ import annotations

import json as _json
import os
from datetime import date, timedelta
from typing import Optional

_TODAY = date.today()

DEFAULT_CALL_DATA = {
    "agent_name": "Priya",
    "agent_gender": "female",
    "applicant_name": "Ramesh Kumar",
    "balance_claim_amount" : "10000",
    "current_date": _TODAY.strftime("%-d %B %Y"),
    "allowed_future_date_one": (_TODAY + timedelta(days=2)).strftime("%-d %B %Y"),
    "emi_ai_overdue_date": (_TODAY - timedelta(days=10)).strftime("%-d %B %Y"),
    "billed_emi_ai_overdue_amt": "1000",
    "emi_overdue_amt": "10000",
    "emi_amount" : "15000",
    "last_four_digit" : "2 3 4 5",
    "loan_nbfc_name" : "H D F C Bank",
    "allocation_dpd_value" : "12",
    "date_of_default" : "2026-06-06",
    "due_date" : "2026-06-06",
    "billed_ai_overdue_amt": "10",
    "remaining_si_emi": "62000",
    "last_4_digits_loan": "7823",
    "product_type": "two-wheeler loan",
    "linked_bank_name": "AU Small Finance Bank",
    "casa_account_no_4digit": "4521",
    "casa_balance": "103000",
    "casa_account_type": "savings",
    "previous_status": "Call Back",
    "language_supported": "Hindi, English, Telugu, Malayalam, Bengali, Marathi, Tamil",
    "default_language": "hindi",
    "bot_name" : "Priya",
    "vb_product" : "two wheeler"
}


def _load_pd_si_globals() -> dict:
    """Exec pd_si.py with stub globals and return its module namespace."""
    pd_si_path = os.path.join(os.path.dirname(__file__), "pd_si.py")
    with open(pd_si_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    stub_globals = {
        "__builtins__": __builtins__,
        "json": _json,
        "flow_name": "",
        "company_id": "",
    }
    exec(source, stub_globals)  # noqa: S102
    return stub_globals


def build_system_prompt(call_data: Optional[dict] = None) -> str:
    """
    Legacy monolithic prompt (system + full call flow). Retained for
    downstream consumers (evaluation exports, debugging) but no longer
    used by the live pipeline — see `build_base_prompt` / `build_stage_prompt`.
    """
    data = {**DEFAULT_CALL_DATA, **(call_data or {})}
    g = _load_pd_si_globals()
    combined = g["system_prompt"] + "\n\n" + g["prompt"]
    return combined.format(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Node/stage prompt builders (live path). The base prompt is seeded once at
# session start; the stage overlay is swapped in `LLMContext.messages[1]` each
# turn by StageOverlayProcessor.
# ─────────────────────────────────────────────────────────────────────────────

INITIAL_STAGE = "intro_verify"


def build_base_prompt(call_data: Optional[dict] = None) -> str:
    """Static global rules — Personality, Guardrails, Language, Dates, etc."""
    data = {**DEFAULT_CALL_DATA, **(call_data or {})}
    g = _load_pd_si_globals()
    return g["base_prompt"].format(**data)


def build_stage_prompt(stage: str, call_data: Optional[dict] = None) -> str:
    """Per-stage overlay. Falls back to INITIAL_STAGE if `stage` is unknown."""
    data = {**DEFAULT_CALL_DATA, **(call_data or {})}
    g = _load_pd_si_globals()
    overlays = g["stage_overlays"]
    overlay = overlays.get(stage) or overlays[INITIAL_STAGE]
    return overlay.format(**data)


def known_stages() -> tuple[str, ...]:
    g = _load_pd_si_globals()
    return tuple(g["STAGES"])


def build_first_message(call_data: Optional[dict] = None) -> str:
    """
    Pull `first_message` out of pd_si.py's `payload` (a json.dumps string),
    pick the language + gender variant, and template with call_data.

    Falls back to hindi/female if the requested combo isn't defined.
    """
    data = {**DEFAULT_CALL_DATA, **(call_data or {})}
    g = _load_pd_si_globals()
    payload = _json.loads(g["payload"])
    messages = payload["first_message"]["message"]

    lang = (data.get("default_language") or "hindi").lower()
    gender = (data.get("agent_gender") or "female").lower()

    by_lang = messages.get(lang) or messages.get("hindi") or next(iter(messages.values()))
    template = by_lang.get(gender) or by_lang.get("female") or next(iter(by_lang.values()))
    return template.format(**data)
