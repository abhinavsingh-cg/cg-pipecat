import json

from typing import Optional
from http import HTTPStatus

from constants import (
    Tables
)


async def get_flow_details(
    db,
    flow_id: int,
    columns=[
        "prompt",
        "system_prompt",
        "loan_entities_v2",
        "company_entities",
        "agent_name",
        "agent_gender",
        "first_message",
        "language_supported",
        "llm_parameters",
        "vendor_name",
        "tts_vendor_name",
        "voice_name",
        "additional_loan_entities",
    ],
):
    """
    Fetch flow details
    """
    status, response = await db.fetch_db_data(
        table=Tables.flows,
        columns=columns,
        where={"id = '%s'": flow_id, "is_deleted = %s": False},
        limit=1,
    )
    if not status:
        return False, "failed to fetch flow", HTTPStatus.INTERNAL_SERVER_ERROR, {}
    if not response:
        return False, "flow not found", HTTPStatus.NOT_FOUND, {}
    response = response[0]
    if response.get("loan_entities_v2"):
        response["loan_entities_v2"] = json.loads(response["loan_entities_v2"])
        response["loan_entities"] = response["loan_entities_v2"]
    if response.get("llm_parameters"):
        response["llm_parameters"] = json.loads(response["llm_parameters"])
    if response.get("additional_loan_entities"):
        response["additional_loan_entities"] = json.loads(
            response["additional_loan_entities"]
        )
    return True, "success", HTTPStatus.OK, response


async def get_voices(
    db,
    persona_id: Optional[int] = None,
    gender: Optional[str] = None,
    columns=["id", "name"],
    vendor_id: Optional[str] = None,
):
    where = {}
    if persona_id is not None:
        where["id = %s"] = persona_id
    if gender:
        where["gender = '%s'"] = gender
    if not where:
        return False, "No filter provided", HTTPStatus.BAD_REQUEST, {}
    if vendor_id is not None:
        where["vendor_id = %s"] = vendor_id
    where["is_active = %s"] = True

    status, response = await db.fetch_db_data(
        table=Tables.personas, columns=columns, where=where
    )
    if not status:
        return False, "Failed to fetch voices", HTTPStatus.INTERNAL_SERVER_ERROR, {}

    if persona_id is not None:
        if not response or not response[0].get("voice_id"):
            return False, "No voice found for the given id", HTTPStatus.NOT_FOUND, {}
        return True, "success", HTTPStatus.OK, response[0]

    return True, "success", HTTPStatus.OK, response
