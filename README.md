# ChatGPT Connect for Open WebUI

Bring your ChatGPT models to Open WebUI through OpenAI's official [**Sign in with ChatGPT**](https://developers.openai.com/siwc) flow.

A single Open WebUI Function provides streaming text chats, thinking blocks, reasoning effort controls, and an admin page for account and model management. Sessions are saved and renewed automatically.

## Requirements

- Open WebUI **0.11.4 or later**, running with one worker
- Administrator access to Open WebUI
- A ChatGPT account eligible for Sign in with ChatGPT
- SSH access from your computer to the machine running Open WebUI

Use one installed Function for one ChatGPT account. A temporary SSH tunnel handles sign-in; chats use the saved session.

## Install

### 1. Add the Function

1. Open [**Functions**](https://docs.openwebui.com/features/extensibility/plugin/functions/#create-manually) in the Open WebUI admin panel.
2. Create an **Event Function** and paste [`dist/openwebui_chatgpt_connect.py`](dist/openwebui_chatgpt_connect.py).
3. Save and enable it.

Open the Function's Valves and set:

| Valve | Value |
| --- | --- |
| `INTERNAL_API_KEY` | A random secret of at least 32 characters, shared with the Open WebUI connection. |
| `SSH_TARGET` | Your Open WebUI host's SSH destination, such as `user@example.com` or an alias from `~/.ssh/config`. |
| `CALLBACK_PORT` | A free port on your computer. Default: `1455`. |

Generate a key with:

```sh
openssl rand -hex 32
```

### 2. Connect ChatGPT

Open `/siwc/admin` on your Open WebUI instance:

```text
https://your-open-webui.example/siwc/admin
```

1. Copy the SSH command and run it in a terminal on your computer.
2. Select **Continue with ChatGPT** and finish signing in.
3. Once the page shows your account, stop the SSH command with `Ctrl+C`.

### 3. Set up the Open WebUI connection

Use the **Set Up Open WebUI connection** section on the admin page to add a native [Responses connection](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-open-responses/#step-1-add-or-edit-a-connection):

| Setting | Value |
| --- | --- |
| URL | Copy from the admin page |
| API type | `Responses` |
| API key | Your `INTERNAL_API_KEY` |

### 4. Choose your models

All available models are selected by default. Choose the models you want in Open WebUI and select **Save changes**.

Existing permissions, parameters, custom names, and avatars are preserved.

## Usage

Select a `ChatGPT/` model in Open WebUI to start chatting. Available models and usage limits follow [your ChatGPT plan](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites) and workspace.

### Reasoning

Reasoning summaries appear in Open WebUI's thinking blocks. Set **Reasoning Effort** in **Advanced Parameters** to adjust it for models that offer this option. Effort levels vary by model.

### Account

The admin page links to ChatGPT security settings and usage information.

- **Sign out** ends the ChatGPT session saved on this Open WebUI host.
- **Security settings** opens [ChatGPT settings](https://chatgpt.com/settings/security), where you can remove the app under **Login connections**.

### Updates

Replace the Function code with the latest `dist/openwebui_chatgpt_connect.py` and save. Updates take effect in place and preserve your saved session. Refresh the admin page to see the changes.

A sign-in that was in progress during an update needs to be started again.

## Troubleshooting

- **Sign-in does not finish:** check that the SSH command is running and `CALLBACK_PORT` is free. Cancel the pending sign-in on the admin page and try again.
- **Models do not appear:** enable the native Responses connection, save your model selection on `/siwc/admin`, and reload Open WebUI.
- **OAuth callback audit warning:** add `siwc/oauth/callback` to `AUDIT_EXCLUDED_PATHS` and restart Open WebUI before signing in.

<details>
<summary>Development</summary>

Install dependencies and run the checks:

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Build the standalone Function:

```sh
uv run python scripts/build.py
```

Output: `dist/openwebui_chatgpt_connect.py`.

</details>
