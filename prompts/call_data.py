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
    "current_date": _TODAY.strftime("%-d %B %Y"),
    "allowed_future_date_one": (_TODAY + timedelta(days=2)).strftime("%-d %B %Y"),
    "emi_ai_overdue_date": (_TODAY - timedelta(days=10)).strftime("%-d %B %Y"),
    "billed_emi_ai_overdue_amt": "1000",
    "emi_overdue_amt": "10000",
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
}


def build_system_prompt(call_data: Optional[dict] = None) -> str:
    """
    Load pd_si.py's `system_prompt` and `prompt` strings and template them
    with `call_data` (defaults to DEFAULT_CALL_DATA for dev runs).

    pd_si.py contains a `payload = json.dumps(...)` at the bottom referencing
    undefined names — exec it with stub globals to extract only the strings
    we need.
    """
    data = {**DEFAULT_CALL_DATA, **(call_data or {})}
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
    combined = stub_globals["system_prompt"] + "\n\n" + stub_globals["prompt"]
    return combined.format(**data)
