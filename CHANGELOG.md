# Changelog

Notable changes are recorded here using [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Native OpenAI web search in the main ChatGPT conversation, enabled with Open WebUI's Web Search switch and existing permissions. Search, page opening, answers, and citations share one Responses request instead of separate search and reading Tool requests.

## [0.4.0] - 2026-10-12

Initial public release.

### Added

- Sign in with ChatGPT to use your subscription in Open WebUI, with automatic session renewal.
- An administrator settings page for account management and model selection, preserving Open WebUI model permissions and custom settings.
- Streaming conversations with text, images, original documents and spreadsheets, plus model-supported reasoning effort and public thinking summaries.
- Native Open WebUI tool calling, including multiple calls and follow-up conversations.
- A companion Filter that keeps tool and document conversations working across follow-ups and Open WebUI restarts.
- Two standalone Function files for installation through Open WebUI.

[Unreleased]: https://github.com/huangboming/openwebui-chatgpt-connect/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/huangboming/openwebui-chatgpt-connect/releases/tag/v0.4.0
