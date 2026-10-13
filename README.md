# ChatGPT Connect for Open WebUI

Use your **ChatGPT subscription** in Open WebUI through OpenAI's official [**Sign in with ChatGPT**](https://developers.openai.com/siwc) flow.

Chat with text, images, and original documents, use Open WebUI Tools, and adjust reasoning effort. Manage your account and available models from a dedicated settings page.

[Download the latest release](https://github.com/huangboming/openwebui-chatgpt-connect/releases/latest) · [Changelog](CHANGELOG.md)

## Requirements

- Open WebUI **0.11.4 or later**, running with one worker
- Administrator access to Open WebUI
- A ChatGPT account eligible for [plan usage in other apps](https://developers.openai.com/siwc/token-sharing-open-source)
- SSH access from your computer to the machine running Open WebUI

Each installation connects one ChatGPT account. Sign-in uses a temporary SSH tunnel; your session is saved and renewed as you chat.

## Set up

### 1. Install the connector

1. Open [**Functions**](https://docs.openwebui.com/features/extensibility/plugin/functions/) in the Open WebUI admin panel.
2. Choose **Import From Link** and paste:

   ```text
   https://github.com/huangboming/openwebui-chatgpt-connect/releases/latest/download/openwebui_chatgpt_connect.py
   ```

3. Save and enable it.

You can also download `openwebui_chatgpt_connect.py` from the [latest release](https://github.com/huangboming/openwebui-chatgpt-connect/releases/latest) and paste its contents into a new Event Function.

Open the Function's **Valves** and set:

| Valve | Value |
| --- | --- |
| `INTERNAL_API_KEY` | A random secret of at least 32 characters. You will also use it for the connection in step 3. |
| `SSH_TARGET` | Your Open WebUI host's SSH destination, such as `user@example.com` or an alias from `~/.ssh/config`. |
| `CALLBACK_PORT` | A free port on your computer. Default: `1455`. |

You can generate an API key with:

```sh
openssl rand -hex 32
```

### 2. Sign in to ChatGPT

Open `/siwc/admin` on your Open WebUI instance:

```text
https://your-open-webui.example/siwc/admin
```

1. Copy the SSH command.

   **Docker:** If SSH connects to the Docker host, use the published HTTP port as the tunnel's remote target. For `-p 3000:8080`, replace `:127.0.0.1:8080` with `:127.0.0.1:3000` in the copied command. Keep the local `CALLBACK_PORT` unchanged.

   Run the command in a terminal on your computer.
2. Select **Continue with ChatGPT** and finish signing in.
3. Once the page shows your account, stop the SSH command with `Ctrl+C`.

### 3. Add the Open WebUI connection

In **Set Up Open WebUI connection** on the settings page, copy the URL and add a [Responses connection](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-open-responses/#step-1-add-or-edit-a-connection) in Open WebUI:

| Setting | Value |
| --- | --- |
| URL | Copy from the settings page |
| API type | `Responses` |
| API key | Your `INTERNAL_API_KEY` |

Keep **Responses API stateful mode** off, which is the default.

### 4. Choose your models

All available models are selected by default. Choose the models you want on `/siwc/admin` and select **Save changes**.

Everyone with access to these models uses the connected ChatGPT account and its plan or workspace usage allowance. Manage access in Open WebUI's model permissions.

Your existing model permissions, parameters, custom names, and avatars are preserved.

### 5. Enable document uploads, tools, and web access (optional)

1. In **Functions**, choose **Import From Link** and paste:

   ```text
   https://github.com/huangboming/openwebui-chatgpt-connect/releases/latest/download/chatgpt_responses_history.py
   ```

2. Save, enable it, and turn on **Global**. It applies only to models from this connector.
3. Open `/api/models?refresh=true` on your Open WebUI instance, then reload the page.
4. For tools and web access, use **Native** function calling with streaming enabled. Enable any ordinary Tools you want in your chat.

## Everyday use

Select a `ChatGPT/` model to start chatting. Requests draw on [your ChatGPT plan](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)'s included usage or your ChatGPT credits balance. Available models and limits depend on your plan and workspace.

Review usage and manage limits in [ChatGPT usage settings](https://chatgpt.com/settings/usage).

### Images and documents

Upload one or more images in the chat composer to read screenshots, compare photos, or ask about visible text. You can refer back to those images in later messages.

Choose a model with [vision support](https://developers.openai.com/api/docs/guides/images-vision#analyze-images). Supported formats are PNG, JPEG, WEBP, and non-animated GIF.

With the companion Filter enabled, upload documents and spreadsheets such as PDF, Word, PowerPoint, text, CSV, or Excel files. Original files go directly to the selected model and are included again when you follow up. Supported formats depend on the model; use PDF when page layout or embedded images matter.

The combined document size is limited to 50 MB per request. Open WebUI's upload limits also apply. Knowledge bases continue to use Open WebUI's retrieval features.

### Thinking and reasoning effort

When a model provides [reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries), they appear as thinking blocks in Open WebUI.

Set **Reasoning Effort** in **Advanced Parameters** for models that offer this option. Available effort levels depend on the selected model.

### Tools

Enable [Open WebUI Tools](https://docs.openwebui.com/features/extensibility/plugin/tools/) in your chat to let the model use them. Tool calls and results appear in the conversation, with permissions and confirmations managed by Open WebUI.

### Web access

Enable **Web Search** in Open WebUI's admin settings, allow it for the user and model, then turn on the chat's Web Search switch for a `ChatGPT/` model. The companion Filter must be enabled. No separate Web Access Tool, search-engine API key, or additional sign-in is needed for these models.

Ask for current information or provide a public URL to read. The model searches and opens pages through OpenAI within the main conversation, with progress and sources displayed by Open WebUI. Answer citations link to supporting pages; retrieved links are separately labeled **Search result** and may not have been opened or cited. Other providers keep their existing search behavior. Ordinary Tools and document uploads can still be used alongside native web access.

Web access is available to the model while the switch is on, not forced on every turn. It uses the chat model and its reasoning settings; it can still be slow if the model performs several searches or extended reasoning. Availability and page access depend on the model and ChatGPT account or workspace policy; page reading is model-assisted, not a raw download or guaranteed full-page extraction.

### Account settings

Open `/siwc/admin` to manage your account and model selection.

- **Usage** opens your ChatGPT usage information.
- **Sign out** ends the session on this Open WebUI host.
- **Security settings** opens [ChatGPT settings](https://chatgpt.com/settings/security), where you can remove the app under **Login connections**.

## Update

Download the files from the [latest release](https://github.com/huangboming/openwebui-chatgpt-connect/releases/latest). Replace the code in the existing connector Function with `openwebui_chatgpt_connect.py` and save. If you use document uploads, tools, or web access, update the Filter with `chatgpt_responses_history.py` from the same release.

Changes take effect when you save, and your ChatGPT session is preserved. Open `/api/models?refresh=true` on your Open WebUI instance, then reload the page. If you were signing in during the update, start sign-in again.

## Troubleshooting

- **Sign-in does not finish:** check that the SSH command is running and `CALLBACK_PORT` is free. Cancel sign-in on the settings page and try again.
- **Models do not appear:** check that the Responses connection is enabled, save your model selection on `/siwc/admin`, and reload Open WebUI.
- **Documents are not read:** check that the companion Filter is enabled with **Global** on, refresh models, and check the upload size. Try PDF if the model rejects another format.
- **Tools stop after the first call:** check that the companion Filter is enabled with **Global** on. Refresh models at `/api/models?refresh=true` and start a new chat using **Native** function calling and streaming.
- **Web access is unavailable:** enable Web Search in Open WebUI's admin settings, check the user's feature permission and model web-search controls, and update both Functions. Use the chat Web Search switch, not a separate Web Access Tool.
- **Usage limit reached:** open [ChatGPT usage settings](https://chatgpt.com/settings/usage) to review your plan or this app's limit.
- **Callback logging warning during sign-in:** add `siwc/oauth/callback` to Open WebUI's `AUDIT_EXCLUDED_PATHS` and restart Open WebUI before signing in.

## License

Licensed under the [Apache License 2.0](LICENSE).
