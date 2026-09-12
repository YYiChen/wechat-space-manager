from datetime import UTC, datetime

import pytest

from wechat_cleaner.domain.contracts import (
    AccountRef,
    ContactKind,
    ContactRef,
    ContractErrorCode,
    FileIdentity,
    MappingConfidence,
    MediaType,
)
from wechat_cleaner.mapper import MediaCandidate, MediaMapper, MessageEvidence

ACCOUNT = AccountRef(account_id="wxid_mapper", account_root=r"D:\synthetic\wxid_mapper")
CONTACT = ContactRef(
    account_id=ACCOUNT.account_id,
    contact_id="wxid_contact",
    kind=ContactKind.DIRECT,
    session_hash="a" * 32,
    display_name="Synthetic Contact",
)
NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def candidate(
    path: str,
    *,
    size: int = 100,
    digest: str | None = None,
    observed_at: datetime = NOW,
) -> MediaCandidate:
    return MediaCandidate(
        account_id=ACCOUNT.account_id,
        file=FileIdentity(
            relative_path=path,
            byte_size=size,
            modified_time_ns=123,
            sha256=digest,
        ),
        media_type=MediaType.IMAGE,
        observed_at=observed_at,
    )


def evidence(
    *,
    message_id: int = 7,
    paths: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
    name: str | None = None,
    size: int | None = None,
    digest: str | None = None,
    observed_at: datetime = NOW,
) -> MessageEvidence:
    return MessageEvidence(
        account_id=ACCOUNT.account_id,
        message_local_id=message_id,
        contact=CONTACT,
        observed_at=observed_at,
        media_paths=paths,
        path_prefixes=prefixes,
        media_name=name,
        byte_size=size,
        sha256=digest,
    )


def test_exact_path_mapping_is_deterministic() -> None:
    item = candidate(r"msg\attach\session\image.dat")
    result = MediaMapper(ACCOUNT).map_candidates(
        [item], [evidence(paths=(item.file.relative_path,))]
    )

    assert not result.errors
    assert result.records[0].mapping_confidence is MappingConfidence.EXACT
    assert result.records[0].message_local_id == 7
    assert result.records[0].contact == CONTACT


def test_hash_and_prefix_mappings_are_high_signal() -> None:
    digest = "b" * 64
    hashed = candidate(r"msg\attach\other\image.dat", digest=digest)
    prefix = candidate(r"msg\attach\known\2026\image.dat")
    result = MediaMapper(ACCOUNT).map_candidates(
        [hashed, prefix],
        [evidence(digest=digest), evidence(message_id=8, prefixes=(r"msg\attach\known",))],
    )

    assert [record.mapping_confidence for record in result.records] == [
        MappingConfidence.EXACT,
        MappingConfidence.HIGH,
    ]


def test_name_date_size_and_name_only_have_different_confidence() -> None:
    medium = candidate(r"msg\file\report.pdf", size=500)
    low = candidate(r"msg\file\report.pdf", size=999)
    evidence_row = evidence(name="report.pdf", size=500)
    result = MediaMapper(ACCOUNT).map_candidates([medium, low], [evidence_row])

    assert result.records[0].mapping_confidence is MappingConfidence.MEDIUM
    assert result.records[1].mapping_confidence is MappingConfidence.LOW


def test_unmapped_candidate_has_no_contact() -> None:
    result = MediaMapper(ACCOUNT).map_candidates(
        [candidate(r"msg\attach\unknown\image.dat")],
        [],
    )

    record = result.records[0]
    assert record.mapping_confidence is MappingConfidence.UNMAPPED
    assert record.contact is None
    assert record.message_local_id is None


def test_ambiguous_evidence_is_rejected_and_not_upgraded() -> None:
    item = candidate(r"msg\attach\same\image.dat")
    result = MediaMapper(ACCOUNT).map_candidates(
        [item],
        [
            evidence(message_id=1, paths=(item.file.relative_path,)),
            evidence(message_id=2, paths=(item.file.relative_path,)),
        ],
    )

    assert result.records[0].mapping_confidence is MappingConfidence.UNMAPPED
    assert result.errors[0].code is ContractErrorCode.MAPPING_UNAVAILABLE


def test_account_mismatch_is_reported_without_emitting_a_record() -> None:
    foreign = MediaCandidate(
        account_id="wxid_foreign",
        file=FileIdentity(relative_path=r"msg\attach\foreign.dat", byte_size=1, modified_time_ns=1),
        media_type=MediaType.IMAGE,
        observed_at=NOW,
    )
    result = MediaMapper(ACCOUNT).map_candidates([foreign], [])

    assert result.records == ()
    assert result.errors[0].code is ContractErrorCode.ACCOUNT_MISMATCH


def test_evidence_rejects_traversal_and_requires_a_key() -> None:
    with pytest.raises(ValueError, match="relative paths"):
        evidence(paths=(r"..\outside\file",))
    with pytest.raises(ValueError, match="at least one correlation key"):
        MessageEvidence(
            account_id=ACCOUNT.account_id,
            message_local_id=1,
            contact=CONTACT,
            observed_at=NOW,
        )


def test_results_are_stable_even_when_evidence_arrives_out_of_order() -> None:
    first = candidate(r"msg\attach\a.dat")
    second = candidate(r"msg\attach\b.dat")
    rows = [
        evidence(message_id=2, paths=(second.file.relative_path,)),
        evidence(message_id=1, paths=(first.file.relative_path,)),
    ]
    result = MediaMapper(ACCOUNT).map_candidates([first, second], rows)

    assert [record.message_local_id for record in result.records] == [1, 2]
