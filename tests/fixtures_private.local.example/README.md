# Private local acceptance baseline

This directory is a version-controlled template only. Copy it to the ignored
directory `tests/fixtures_private.local/` and replace every placeholder locally.

The local file may record paths, aggregate counts, byte totals, and validation
results for a real account. It must never contain media copies, a chat database,
decryption keys, contact names, account IDs, plaintext message content, or any
exported/decrypted image.

`private-baseline.local.example.json` models the current local acceptance target:

- a full WeChat storage inventory of **92.31 GiB**;
- the private forwarded-record fixture informally called “季冬 Rec”;
- 12 forwarded-record directories, 121 originals, 121 thumbnails, and 242/242
  successful image decodes.

The actual source path remains a local-only setting. The application and its
tests must treat this fixture as opt-in: a missing private config is normal and
must skip the corresponding local integration check.
