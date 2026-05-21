from typing import Optional, Dict, Any
import uuid
import json
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import text


def log_audit(
    db: Session,
    actor_id: Optional[str],
    action: str,
    entity_type: str,
    entity_id: Optional[str],
    changes: Optional[Dict[str, Any]] = None,
) -> None:
    # Ensure datetimes/UUIDs/times are JSON serializable for audit payloads.
    payload = None if changes is None else json.dumps(jsonable_encoder(changes))
    db.execute(
        text(
            """
            INSERT INTO audit_logs (id, actor_id, action, entity_type, entity_id, changes)
            VALUES (:id, :actor_id, :action, :entity_type, :entity_id, CAST(:changes AS jsonb))
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "changes": payload,
        },
    )
