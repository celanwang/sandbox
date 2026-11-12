# Changelog

## [Unreleased]

### Added

- Add a synthetic McDelivery MCP service with 13 tools for delivery, pricing, and orders.
- Manage isolated episodes, inject business and transport faults, and export state snapshots and call traces.
- Run scripted Iris scenarios and save dialogue, approvals, and sandbox evidence.
- Archive closed episodes on disk and list or purge them through the management API.
- Use Qwen 3.8 Flash to recognize scripted dialogue intents in shadow or active mode.
- Exercise address and drink clarification and replay recorded monitor decisions from evidence.

### Fixed

- Require visible quotes, matching Iris ledger entries, and read-only recovery evidence in scenario verdicts.
- Prevent in-flight requests from crossing a round or reset and record observed disconnects separately from planned faults.
- Preserve order chronology and reject invalid reservations and unsupported customizations.
