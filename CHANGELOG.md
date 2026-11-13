# Changelog

## [Unreleased]

### Added

- Add a synthetic McDelivery MCP service with 13 tools for delivery, pricing, and orders.
- Manage isolated episodes, inject business and transport faults, and export state snapshots and call traces.
- Run scripted Iris scenarios and save dialogue, approvals, and sandbox evidence.
- Archive closed episodes on disk and list or purge them through the management API.
- Use Qwen 3.8 Flash to recognize scripted dialogue intents in shadow or active mode.
- Exercise address and drink clarification and replay recorded monitor decisions from evidence.
- Run a local regression suite with a fresh Iris process per scenario and export batch and dialogue summaries.

### Changed

- Run one scenario per Iris MCP credential and support episode-scoped MCP URLs.

### Fixed

- Require visible quotes, matching Iris ledger entries, and read-only recovery evidence in scenario verdicts.
- Prevent in-flight requests from crossing a round or reset and record observed disconnects separately from planned faults.
- Preserve order chronology and reject invalid reservations and unsupported customizations.
- Pin Runner evidence to the verified episode generation and retry ambiguous opens by ID.
- Reject contradictory payment and delivery claims and classify recovery lookups by timing.
- Share clarification policies across monitor modes and validate replayed replies against scenario actions.
- Preserve matching quotes across comparisons and include reservation details in order receipts.
- Keep business verdicts available when only the diagnostic Iris trace is missing.
