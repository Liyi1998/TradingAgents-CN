# Audited source-only public export

This is a filesystem snapshot, not a Git repository or Git history. Preserve LICENSE, app/LICENSE and frontend/LICENSE and existing upstream attribution. No push, commit, remote change, login or deployment was performed.

Use source build configuration, not historical runtime image-tag overlays, to build a new image. runtime/services compose files are historical/operational reference only, not instructions to enable or deploy services. Optional local-embedding and readonly-daily-bridge server source is included; their private configuration, database, model weights, third-party dependency folders and external services are not.

Machine-specific paths in app/config/settings.json were deliberately generalized only in this copy. Dirty config/pricing.json is included unchanged; dirty docs/PROJECT_STATE.md is excluded. All real environment/config secrets, history, account/watchlist/report data, generated graphs, private operator notes, binary documents/images and dependency/build caches are omitted. Environment examples are deliberately omitted too; recreate environment values privately.

This is not an exact production deployment backup. Synthetic test email/password/IP examples and public upstream attribution remain. Three token-lookalike test modules and the actual personal research fixture are excluded; some tests that refer to omitted fixtures/assets or local services may not run. No tests, build, model request or deployment were invoked during export. Missing optional media may affect presentation.

MANIFEST.json enumerates all payload files, their SHA256, exact reviewed exclusions and grouped counts for excluded filesystem data. SHA256SUMS covers payload and the generated README/manifest (not itself). No audit can guarantee detection of every secret; this audit combines full UTF-8 payload scanning for common token/private-key/JWT/password/URI/PII patterns and high-entropy literals with source-context classification. No history, binary documents, excluded private configuration, database contents or dynamically stored prompts were inspected.
