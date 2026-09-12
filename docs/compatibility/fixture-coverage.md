# Fixture compatibility coverage

This document states what the Phase 1 filesystem fixtures prove and what they do
not prove. It prevents a synthetic test pass from being presented as evidence of
compatibility with an installed WeChat version.

| Capability | Synthetic coverage | Private local acceptance | Status |
| --- | --- | --- | --- |
| Locate an `xwechat_files` root | Yes, two synthetic accounts | Required before release | Planned |
| Account-directory isolation | Yes | Required | Planned |
| Image/video/file categorisation | Yes, filename/layout only | Required | Planned |
| `Rec` original/thumbnail pairing | Yes, two pairs | 12 records, 121 pairs | Registered locally |
| Protected-directory exclusion | Yes: DB, favourites, send temp | Required | Planned |
| Cache classification | Yes: synthetic cache | Required | Planned |
| Symlink/junction rejection | Opt-in symlink probe; junction specified | Required on Windows | Planned |
| Database schema mapping | No | Required | Pending contracts/fixtures |
| Legacy image decryption | No | Required | Pending decoder |
| V2 image decryption | No | 242/242 existing local result | Pending decoder regression |
| Cleanup execution | No | Separate disposable-copy validation | Prohibited in Phase 1 |

## Compatibility rule

The application must expose capability detection, not a blanket claim that every
WeChat release is supported. A new database/decryption format is unsupported
until it passes both a non-private structural test and an explicit private local
read-only acceptance check.
