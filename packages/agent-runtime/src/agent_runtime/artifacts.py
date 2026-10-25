"""Artifact metadata projected from committed tool results, without storage I/O."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from .journal import Journal, load, now_ms

FIELD = "_runtime_artifacts"


@dataclass(frozen=True)
class ArtifactRef:
    key: str
    sha256: str
    bytes: int
    name: str
    media_type: str
    source: str = ""
    expires_at: int | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not 1 <= len(self.key) <= 1024:
            raise ValueError("bounded artifact storage key required")
        if not isinstance(self.sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise ValueError("artifact SHA-256 required")
        if type(self.bytes) is not int or self.bytes < 0:
            raise ValueError("artifact byte length must be nonnegative")
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 256:
            raise ValueError("bounded artifact name required")
        if not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 256:
            raise ValueError("bounded artifact media type required")
        if not isinstance(self.source, str) or len(self.source) > 4096:
            raise ValueError("bounded artifact source required")
        if self.expires_at is not None and (type(self.expires_at) is not int or self.expires_at < 0):
            raise ValueError("artifact expiry must be an epoch millisecond timestamp")


def references(result) -> tuple[ArtifactRef, ...]:
    if not isinstance(result, dict) or FIELD not in result:
        return ()
    items = result[FIELD]
    if not isinstance(items, list) or len(items) > 64:
        raise ValueError("artifact manifest must contain at most 64 references")
    refs = tuple(ArtifactRef(**item) for item in items)
    if len({ref.key for ref in refs}) != len(refs):
        raise ValueError("artifact keys must be unique in a tool result")
    return refs


class RuntimeArtifacts:
    """The host authorizes Run access and verifies bytes/permissions on retrieval.

    A reference is an immutable tool-result fact, not a claim that its backing
    bytes still exist. Retention never deletes the journal or a user's originals.
    """

    def __init__(self, journal: Journal):
        self.journal = journal

    def list(self, run_id: str, *, tenant_id: str) -> list[dict]:
        if not tenant_id:
            raise ValueError("tenant identity is required")
        with self.journal.read_tx() as db:
            if db.execute("SELECT id FROM runs WHERE id=? AND tenant_id=?", (run_id, tenant_id)).fetchone() is None:
                raise KeyError(run_id)
            rows = db.execute(
                "SELECT id,effect_id,result FROM steps WHERE run_id=? AND kind='tool' AND state='SUCCEEDED' ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        result = []
        at = now_ms()
        for row in rows:
            for ref in references(load(row["result"])):
                identity = "\0".join((run_id, row["id"], ref.key, ref.sha256)).encode()
                result.append(
                    {
                        **asdict(ref),
                        "id": hashlib.sha256(identity).hexdigest(),
                        "run_id": run_id,
                        "step_id": row["id"],
                        "effect_id": row["effect_id"],
                        "expired": ref.expires_at is not None and ref.expires_at <= at,
                    }
                )
        return result
