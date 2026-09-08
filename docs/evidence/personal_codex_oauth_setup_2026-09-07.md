# Pal Personal Codex OAuth Setup Record

Recorded on 2026-09-07. This document preserves the sources of relevant public statements and the local deployment checks performed that day, for possible future correspondence with OpenAI Support or Tibo.

This is not official authorization, a compliance guarantee, a promise about future account decisions, or an independent archive of the original posts. It contains no API keys, OAuth tokens, account identifiers, or other authentication credentials.

## 1. Tibo’s public statements and sources

Relevant post dated 2026-08-21:

- [Tibo’s original post](https://x.com/thsottiaux/status/2090675027670978569)
- [Accessible mirror containing the post and replies](https://zamantika.com/en/mwfowlie/status/2090683786925396369)

Direct access to the original X post returned HTTP 403 during this review. The content was obtained from the mirror found at the time; the original post’s full text was not directly verified. The following is a paraphrase, not a verbatim quotation:

- Using one’s own subscription allowance through Sign in with ChatGPT in official clients or supported open-source clients, such as Pi and OpenCode, was described as acceptable.
- Converting a subscription into API traffic for redistribution or sharing among multiple users was described as unsupported and potentially subject to fraud-prevention checks.

The earlier [January 2026 post concerning OpenCode](https://x.com/thsottiaux/status/2009742187484065881) is also retained as a reference from the discussion. It could not be read directly during this review and is not presented as specific authorization for CLIProxyAPI.

These statements do not automatically authorize every proxy implementation. No explicit official confirmation covering this particular single-user, local CLIProxyAPI OAuth connection to Pal was found during this review.

Reference: [OpenAI Terms of Use](https://openai.com/policies/terms-of-use/). The terms include restrictions on circumventing usage limits or protective measures. The applicable regional terms and any subsequent updates should be checked with OpenAI.

## 2. User’s stated purpose

The user stated that CLIProxyAPI runs on their own Raspberry Pi, authenticates through OAuth using their own account, and provides model access exclusively to their personal agent, Pal. Subscription access is not offered or resold to other users. This records the user’s stated purpose; network configuration alone cannot independently establish all historical usage.

## 3. Deployment checks performed on the recorded date

The review consisted of local configuration reads, runtime inspection, and model-list requests. No model inference requests were sent, and no configuration was changed.

| Check | Result |
| --- | --- |
| Proxy service | User service `cli-proxy-api.service`, running |
| Installed version directory | `~/.local/lib/cli-proxy-api/7.2.151/` |
| Actual listening address | `127.0.0.1:8317`, matching the configuration |
| Remote management | `allow-remote: false` |
| Management panel | `disable-control-panel: true` |
| Management secret | Not set |
| Local API authentication | One API key configured; `/v1/models` returned 401 without a key and 200 with the configured key |
| OAuth account files | One JSON file found, with type `codex` and `disabled: false` |
| Credential permissions | OAuth file: 600; auth directory: 700; proxy configuration file: 600 |
| Debug and file logging | `debug: false`, `logging-to-file: false`; this does not imply that no other system logs exist |
| Outbound proxy | `socks5h://127.0.0.1:1080` |
| Pal connection URL | `http://127.0.0.1:8317/v1` |
| Pal wire protocol | `openai_response` (Responses) |
| Pal local proxy credentials | Credentials referenced by all five Codex endpoints were successfully decrypted and matched the proxy configuration; no keys were printed |
| Model list | Local `/v1/models` included `gpt-6-astra`, along with Sol, Terra, Luna, Spark, and other entries; a listed model does not establish successful upstream access |

Relevant configuration locations:

- `~/.config/cli-proxy-api/config.yaml`
- `~/.config/cli-proxy-api/auth/`
- Pal endpoint definitions and the current selection: `~/.pal/pal.sqlite3`
- Pal credential storage: `~/.pal/secrets.json`; its contents are not reproduced here

At the time of inspection, Pal’s active main model endpoint was `glm-5.3-bigmodel-anthropic`. The subscription proxy endpoint `codex-gpt-6-astra` was configured but was not selected as the main model. A separate endpoint named `gpt-6-astra` used OpenRouter; these are distinct endpoints.

The checks established the local listening address, configuration, credential matching, and model-list authentication. Full inference, tool calling, streaming responses, cache behavior, and OAuth refresh were not tested. The review did not audit all host forwarding rules or historical traffic. A localhost binding does not itself establish authorization under the terms.

## 4. Suggested message for Support

The following is an unsent draft. Before using it, update the deployment details and ensure that all statements still reflect actual usage:

> I run CLIProxyAPI locally on my own Raspberry Pi and authenticate with my own ChatGPT account via OAuth. It is intended exclusively as the model backend for my personal agent, Pal. The proxy binds to 127.0.0.1:8317 and requires a local API key. I do not share or resell subscription access, and I do not intend to bypass usage limits.
>
> I understood your August 21 statement to support personal use of subscription allowances through third-party clients using Sign in with ChatGPT. Could you clarify whether this particular single-user, self-hosted setup is permitted? If an account restriction was caused by this setup, could you review whether it was classified as shared or resold access in error?
>
> Reference: https://x.com/thsottiaux/status/2090675027670978569

If an account restriction occurs, request an account review or submit an appeal. Resetting a usage allowance and lifting an account restriction are separate actions. This record does not guarantee that either request will be approved.
