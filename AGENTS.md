# Agent guide

Connect Open WebUI to ChatGPT plan usage through the official [SIWC API](https://developers.openai.com/siwc/token-sharing-open-source).

## Responsibilities

- Open WebUI owns chat history, model access controls, and tool permissions/execution.
- The Event Function owns SIWC authentication, the admin page, and Responses adaptation.
- The companion Filter preserves opaque reasoning and hosted web actions, supplies original uploaded documents, and routes the chat web-search switch through native hooks. Keep it stateless and scoped to this connector; no second history store or global monkey-patches.
- Use the native Responses connection and existing Open WebUI HTTP port, not a separate gateway/listener, ChatGPT web endpoints, or reused Codex credentials.
- One installation connects one ChatGPT account/workspace; deployment uses one worker.

## Source map

Python sources live in `src/openwebui_chatgpt_connect/`:

| Path | Responsibility |
| --- | --- |
| `function.py` | Event lifecycle, route ownership/hot updates, internal API authentication |
| `settings.py` | Protocol constants and administrator Valves |
| `auth.py` | Identity validation, credential persistence, token refresh/revocation |
| `admin.py` | Admin operations, OAuth transactions/callbacks, model metadata updates |
| `responses.py` | Catalog, request adaptation, tools/images/files, checked streaming |
| `history_filter.py` | Stateless reasoning history and original-upload compatibility Filter |
| `file_inputs.py` | Shared inline-document validation and request size limit |
| `assets/` | Standalone `admin.html` page/preview and bundled `chatgpt.png` avatar |

`scripts/build.py` generates both deliveries; `tests/` tests the complete deliveries. `README.md` is the end-user guide; keep developer commands, tests, and internal details out.

## Contracts to preserve

- Keep administrator authentication, CSRF protection, and expiring one-use OAuth state/PKCE/nonce validation. Never log tokens, OAuth codes, chat content, or opaque reasoning.
- Keep file locks, restrictive permissions, atomic saves, and coordinated session changes. Credentials live under Open WebUI's `DATA_DIR/siwc-connect`.
- Sign out attempts revocation and clears local tokens, but retains the issued client/account mapping and stable host ID. Removing the remote app is a separate ChatGPT settings action.
- SIWC requests use `store=false`, `stream=true`, and complete input history. Preserve text/image order, tool call/result pairing, and reasoning order relative to tool calls. Open WebUI executes function tools; the adapter supplies the required SIWC namespace.
- Uploaded documents are current-request attachment context, not per-turn replay. Resolve file IDs through host permissions and storage; never trust request paths. Preserve original bytes, enforce the shared size limit, consume only converted upload references before RAG, and leave knowledge sources unchanged.
- Native web access is opt-in through the chat Web Search switch. Preserve global/user/model web-search controls; consume only the connector's host search flag before RAG and builtin search injection. OpenAI executes hosted search and page opening in the main request; Open WebUI still owns ordinary function tools. Carry hosted actions alongside encrypted reasoning in native history without synthetic reasoning or a second store. Emit progress only from actual events; keep retrieved results distinct from answer citations. UI delivery failures must not interrupt inference or history adaptation.
- A stream succeeds only at `response.completed`. Close upstream resources on cancellation, disconnect, or failure; do not retry a stream after consumption has begun.
- Thinking displays public summaries only. Preserve accumulated summaries when final snapshots are empty, retain paragraph boundaries, and prefer nonempty upstream snapshots. Never decrypt, display, rewrite, or fabricate encrypted reasoning.
- Scope the Filter using the catalog's `__model__.openai.source` marker, not display names. Preserve other providers' fields and compatibility with already-saved history details.
- Keep upstream model IDs; prefix display names with `ChatGPT/`. Model metadata writes belong to explicit admin saves, not inference/catalog reads. Preserve access controls, selection, custom names, parameters, and custom avatars.
- Discover reasoning capabilities lazily from the catalog before capability validation; inference must not require a prior model-list request.

## Build and validation

- Edit `src/`, never generated `dist/` files. Metadata comes from `pyproject.toml`.
- Keep the build script fixed-purpose: known module order, embedded assets, two outputs. No CLI options, recursive dependency resolver, or builder framework.
- Regenerate and include both `dist/openwebui_chatgpt_connect.py` and `dist/chatgpt_responses_history.py` with changes that affect delivery.
- `.github/workflows/ci.yml` runs the checks below and verifies generated files; the tag-triggered release workflow reuses CI before publishing both Functions and `LICENSE`.
- Release tags must equal `v<pyproject.toml version>` and have one nonempty matching section in `CHANGELOG.md`. Keep release instructions out of the user README; never push tags or publish without authorization.

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python scripts/build.py
uv run pytest
```

Node.js is needed for admin-page interaction tests. Mock only host/network boundaries. Native-history contracts live in `tests/fixtures/responses_history.json`. Live sign-in/inference requires user authorization.

Keep new tests proportional to changed observable contracts; use deployment acceptance for live model capabilities, content quality, and performance.

## Change style

Use English source, UI, and documentation. Keep changes small and self-documenting. Split modules only for a concrete boundary, not a `tools.py`/`images.py`/`streaming.py` taxonomy. Add regression tests for observable behavior, not implementation details.

Use one physical line per prose paragraph; preserve structural line breaks for lists, tables, and code blocks. Let Markdown renderers wrap text rather than inserting hard wraps to match code line widths.
