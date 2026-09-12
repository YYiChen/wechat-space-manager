"""Deterministic mapping from de-identified evidence to public media records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from pathlib import PureWindowsPath

from wechat_cleaner.domain.contracts import (
    AccountRef,
    ContactRef,
    ContractError,
    ContractErrorCode,
    MappingConfidence,
    MediaRecord,
)

from .models import MediaCandidate, MessageEvidence


@dataclass(frozen=True, slots=True)
class MappingResult:
    """Records and non-secret errors emitted by one mapping pass."""

    records: tuple[MediaRecord, ...]
    errors: tuple[ContractError, ...]


@dataclass(frozen=True, slots=True)
class _Match:
    evidence: MessageEvidence | None
    confidence: MappingConfidence
    reason: str
    error: ContractError | None = None


def _evidence_key(evidence: MessageEvidence) -> tuple[int, str, str]:
    return (
        evidence.message_local_id,
        evidence.contact.contact_id.casefold(),
        evidence.source.casefold(),
    )


def _deduplicate(values: Iterable[MessageEvidence]) -> tuple[MessageEvidence, ...]:
    unique: dict[tuple[int, str, str], MessageEvidence] = {}
    for value in values:
        unique[_evidence_key(value)] = value
    return tuple(sorted(unique.values(), key=_evidence_key))


class _EvidenceIndex:
    def __init__(self, evidence: Iterable[MessageEvidence]) -> None:
        by_path: dict[str, list[MessageEvidence]] = defaultdict(list)
        by_hash: dict[str, list[MessageEvidence]] = defaultdict(list)
        by_name: dict[str, list[MessageEvidence]] = defaultdict(list)
        prefixes: list[tuple[str, MessageEvidence]] = []
        for item in evidence:
            for path in item.media_paths:
                by_path[path.casefold()].append(item)
            for prefix in item.path_prefixes:
                prefixes.append((prefix.casefold(), item))
            if item.sha256:
                by_hash[item.sha256].append(item)
            if item.media_name:
                by_name[item.media_name.casefold()].append(item)
        self.by_path = {key: _deduplicate(value) for key, value in by_path.items()}
        self.by_hash = {key: _deduplicate(value) for key, value in by_hash.items()}
        self.by_name = {key: _deduplicate(value) for key, value in by_name.items()}
        self.prefixes = tuple(
            sorted(prefixes, key=lambda pair: (len(pair[0]), _evidence_key(pair[1])))
        )

    def path(self, value: str) -> tuple[MessageEvidence, ...]:
        return self.by_path.get(value.casefold(), ())

    def digest(self, value: str) -> tuple[MessageEvidence, ...]:
        return self.by_hash.get(value, ())

    def name(self, value: str) -> tuple[MessageEvidence, ...]:
        return self.by_name.get(value.casefold(), ())

    def prefix(self, value: str) -> tuple[MessageEvidence, ...]:
        key = value.casefold()
        matched: list[MessageEvidence] = []
        for prefix, item in self.prefixes:
            if key == prefix or key.startswith(prefix + "\\"):
                matched.append(item)
        return _deduplicate(matched)


class MediaMapper:
    """Map scanner candidates without guessing past explicit confidence rules."""

    def __init__(self, account: AccountRef) -> None:
        self.account = account

    def map_candidates(
        self,
        candidates: Iterable[MediaCandidate],
        evidence: Iterable[MessageEvidence],
    ) -> MappingResult:
        errors: list[ContractError] = []
        accepted_evidence: list[MessageEvidence] = []
        for item in evidence:
            if item.account_id != self.account.account_id:
                errors.append(
                    ContractError(
                        code=ContractErrorCode.ACCOUNT_MISMATCH,
                        message="database evidence belongs to a different account",
                    )
                )
                continue
            accepted_evidence.append(item)
        index = _EvidenceIndex(accepted_evidence)

        records: list[MediaRecord] = []
        for candidate in candidates:
            if candidate.account_id != self.account.account_id:
                errors.append(
                    ContractError(
                        code=ContractErrorCode.ACCOUNT_MISMATCH,
                        message="media candidate belongs to a different account",
                    )
                )
                continue
            match = self._resolve(candidate, index)
            if match.error is not None:
                errors.append(match.error)
            records.append(self._record(candidate, match))
        return MappingResult(records=tuple(records), errors=tuple(errors))

    def _resolve(self, candidate: MediaCandidate, index: _EvidenceIndex) -> _Match:
        relative_path = candidate.file.relative_path
        exact = index.path(relative_path)
        if exact:
            return self._unique(exact, MappingConfidence.EXACT, "database media path matched")

        if candidate.file.sha256:
            hashed = index.digest(candidate.file.sha256)
            if hashed:
                return self._unique(hashed, MappingConfidence.EXACT, "database media hash matched")

        prefixed = index.prefix(relative_path)
        if prefixed:
            return self._unique(prefixed, MappingConfidence.HIGH, "database media root matched")

        basename = PureWindowsPath(relative_path).name
        named = index.name(basename)
        if named:
            same_date_and_size = tuple(
                item
                for item in named
                if item.observed_at.astimezone(UTC).date()
                == candidate.observed_at.astimezone(UTC).date()
                and item.byte_size is not None
                and item.byte_size == candidate.file.byte_size
            )
            if same_date_and_size:
                return self._unique(
                    same_date_and_size,
                    MappingConfidence.MEDIUM,
                    "media name, date and size correlated",
                )
            return self._unique(named, MappingConfidence.LOW, "media name correlated heuristically")

        return _Match(None, MappingConfidence.UNMAPPED, "no database evidence matched")

    @staticmethod
    def _unique(
        values: Iterable[MessageEvidence], confidence: MappingConfidence, reason: str
    ) -> _Match:
        unique = _deduplicate(values)
        if len(unique) != 1:
            return _Match(
                None,
                MappingConfidence.UNMAPPED,
                "ambiguous database evidence",
                ContractError(
                    code=ContractErrorCode.MAPPING_UNAVAILABLE,
                    message="multiple database messages matched one media candidate",
                ),
            )
        return _Match(unique[0], confidence, reason)

    def _record(self, candidate: MediaCandidate, match: _Match) -> MediaRecord:
        evidence = match.evidence
        contact: ContactRef | None = evidence.contact if evidence is not None else None
        message_local_id = evidence.message_local_id if evidence is not None else None
        return MediaRecord(
            account_id=candidate.account_id,
            file=candidate.file,
            media_type=candidate.media_type,
            observed_at=candidate.observed_at,
            contact=contact,
            message_local_id=message_local_id,
            mapping_confidence=match.confidence,
            mapping_reason=match.reason,
            original_media_id=candidate.original_media_id,
            is_regenerable_cache=candidate.is_regenerable_cache,
        )


def map_candidates(
    account: AccountRef,
    candidates: Iterable[MediaCandidate],
    evidence: Iterable[MessageEvidence],
) -> MappingResult:
    """Convenience wrapper for one deterministic mapping pass."""

    return MediaMapper(account).map_candidates(candidates, evidence)
