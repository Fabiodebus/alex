"""VoiceProfileStore — REP-tier-memory CRUD for per-rep VoiceProfiles.

The blueprint places voice state in :class:`RepMemory` so the
existing per-rep isolation (RLS + the ``org_share_rep_memories``
flag) automatically enforces "VoiceProfile is scoped strictly per rep
and never shared across reps." Every update writes a fresh memory
row; the most recent row is the active profile and the older ones
are the revert history.

Each row carries:

* ``kind='voice.profile'``
* ``content`` = JSON-encoded :class:`VoiceProfile`
* ``attributes.version`` (int, monotonic)
* ``attributes.sample_count`` (running total across both languages)
* ``attributes.notes`` (optional — populated on revert)

Lookups read recent rows in descending order; "current" is whichever
row has the highest version.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import structlog

from ..schemas import (
    MemoryRecord,
    MemoryTier,
    MemoryWrite,
    VoiceLanguage,
    VoiceLanguageProfile,
    VoiceProfile,
)
from .memory_store import MemoryStore

log = structlog.get_logger(__name__)


VOICE_PROFILE_KIND = "voice.profile"


class VoiceProfileStoreError(RuntimeError):
    pass


class VoiceProfileStore:
    def __init__(self, *, memory_store: MemoryStore) -> None:
        self._memory_store = memory_store

    async def get_current(self, *, tenant_id: UUID, rep_id: UUID) -> VoiceProfile:
        """Return the highest-version profile, or a fresh default."""
        rows = await self._list_rows(tenant_id=tenant_id, rep_id=rep_id, limit=1)
        if not rows:
            return _default_profile(rep_id)
        return _profile_from_row(rows[0]) or _default_profile(rep_id)

    async def put(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        profile: VoiceProfile,
        notes: str | None = None,
    ) -> VoiceProfile:
        """Persist ``profile`` as the new active version.

        The caller is responsible for incrementing ``profile.version`` —
        the updater bumps it after applying a signal; the revert path
        bumps it after copying from an old row. The store enforces
        monotonicity (rejects ``version <= current``)."""
        current = await self.get_current(tenant_id=tenant_id, rep_id=rep_id)
        if profile.version <= current.version and (current.updated_at is not None):
            raise VoiceProfileStoreError(
                f"voice profile version must increase ({profile.version} <= {current.version})"
            )
        profile = profile.model_copy(
            update={"updated_at": datetime.now(timezone.utc), "notes": notes}
        )
        content = json.dumps(profile.model_dump(mode="json"), separators=(",", ":"), default=str)
        await self._memory_store.write_with_status(
            tenant_id=tenant_id,
            write=MemoryWrite(
                tier=MemoryTier.REP,
                owner_id=rep_id,
                kind=VOICE_PROFILE_KIND,
                content=content,
                attributes={
                    "version": profile.version,
                    "sample_count": _total_sample_count(profile),
                    "notes": notes,
                },
                source_uri=f"voice://rep/{rep_id}/v{profile.version}",
            ),
            index_embeddings=False,
        )
        log.info(
            "voice_profile_store.put",
            rep_id=str(rep_id),
            version=profile.version,
            sample_count=_total_sample_count(profile),
        )
        return profile

    async def list_versions(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        limit: int = 20,
    ) -> list[VoiceProfile]:
        rows = await self._list_rows(tenant_id=tenant_id, rep_id=rep_id, limit=limit)
        out: list[VoiceProfile] = []
        for row in rows:
            parsed = _profile_from_row(row)
            if parsed is not None:
                out.append(parsed)
        return out

    async def revert_to(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        version: int,
        notes: str | None = None,
    ) -> VoiceProfile:
        """Write a new row that copies the payload of the named version.

        We never mutate historical rows — the new row carries a bumped
        ``version`` and a ``notes`` attribute explaining the revert so
        the audit trail stays linear."""
        history = await self.list_versions(tenant_id=tenant_id, rep_id=rep_id, limit=50)
        target = next((p for p in history if p.version == version), None)
        if target is None:
            raise VoiceProfileStoreError(
                f"no voice profile version {version} for rep {rep_id}"
            )
        latest = history[0] if history else None
        new_version = (latest.version if latest else version) + 1
        replayed = target.model_copy(update={"version": new_version, "notes": notes})
        return await self.put(
            tenant_id=tenant_id, rep_id=rep_id, profile=replayed, notes=notes
        )

    async def _list_rows(
        self, *, tenant_id: UUID, rep_id: UUID, limit: int
    ) -> list[MemoryRecord]:
        rows = await self._memory_store.list_recent(
            tenant_id=tenant_id,
            tier=MemoryTier.REP,
            owner_id=rep_id,
            kinds_filter=[VOICE_PROFILE_KIND],
            limit=limit,
        )
        # MemoryStore returns most-recent first by created_at, but the
        # version attribute is the canonical ordering — sort by it
        # explicitly so a clock skew can't swap versions around.
        rows.sort(key=lambda r: int((r.attributes or {}).get("version") or 0), reverse=True)
        return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _profile_from_row(row: MemoryRecord) -> VoiceProfile | None:
    try:
        payload = json.loads(row.content)
    except (TypeError, ValueError, json.JSONDecodeError):
        log.warning("voice_profile_store.unreadable_row", memory_id=str(row.id))
        return None
    try:
        return VoiceProfile.model_validate(payload)
    except Exception:
        log.warning("voice_profile_store.invalid_payload", memory_id=str(row.id))
        return None


def _default_profile(rep_id: UUID) -> VoiceProfile:
    return VoiceProfile(
        rep_id=rep_id,
        version=0,
        languages={
            VoiceLanguage.EN: VoiceLanguageProfile(),
            VoiceLanguage.DE: VoiceLanguageProfile(),
        },
        updated_at=None,
    )


def _total_sample_count(profile: VoiceProfile) -> int:
    return sum(sub.sample_count for sub in profile.languages.values())
