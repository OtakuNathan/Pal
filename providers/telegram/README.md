# Telegram channel provider

This provider is loaded exclusively from
`<runtime_root>/channel/providers/telegram/`.

Its runtime-owned implementation and manifest live in that provider directory.
Each configured endpoint keeps provider-owned mutable state in
`<runtime_root>/data/channel/<endpoint_id>/state.sqlite3`. The central Pal
database owns only endpoint registration and lifecycle projection.

Inline-keyboard callback mappings and native Telegram message targets are
durable provider projections. Human-review truth and decision validity remain
owned by Bunshin.

Remote execution approval uses the same execution-owned approval flow as TTY
and desktop_avatar. The card is delivered only to the channel that requested
the operation. Telegram callbacks must come from the paired user who owns the
card, in its original chat; the verified Telegram sender becomes the approval
actor. Approval is single-use and expires. “Approved once” confirms authorization,
not successful execution; the operation reports its result separately.

Version 0.3.4 separates interaction details from decisions. Approval requests,
Bunshin questions and architecture reviews send their details first, followed by
one short decision message replying to those details. Short details remain chat
messages; text exceeding `send_policy.max_message_chars` (capped at Telegram's
4096 UTF-16 units) is sent intact as a UTF-8 Markdown file. Other oversized
interaction cards use the same presentation.

Version 0.3.5 renders interaction message bodies through the same MarkdownV2
converter as ordinary replies, including in-place keyboard updates. Producers
own headings, emphasis and grouping in ordinary Markdown; this provider does
not define slash-command-specific layouts. Button labels remain plain text.
A rejected Markdown format is retried without formatting, preserving the
same keyboard and action mapping.

Bunshin's existing task YAML and architecture Markdown attachments are sent
before the decision card. An identical delivered text attachment is reused as
the details, rather than uploading or pasting it again. A failed upload prevents
the dependent keyboard from appearing. Short sudo descriptions preserve command
and stdin code blocks using the existing Markdown renderer.

Decision messages are limited to 1024 UTF-16 units and keep the original action
mapping. Bunshin question buttons display A/B/C while returning the unchanged
producer-owned answers. A changed document withdraws the previous keyboard and
creates a new card; callbacks must match the current card's message ID as well
as its original chat and owner. Details and card targets survive provider restart.
Resolution edits only the short card. A failed close revokes its local actions
but retains the target for a later close retry.

These presentation changes use the existing interaction and attachment contracts.
Activate the updated Telegram provider at an idle boundary; no Pal core restart,
native binary replacement, Bunshin data migration or worker upgrade is required.

Build and install the provider independently from Pal core:

```bash
scripts/build_provider_packages.sh
pal provider install ./dist/providers/pal_channel_provider_telegram-*.whl \
  --runtime-root ~/.pal
```

The wheel is a versioned provider artifact, not a request to install its code
into Pal's shared Python environment. `pal provider install` validates and
atomically publishes its payload under the runtime root, archives the previous
copy, and leaves endpoint configuration, credentials, and provider data alone.
The CLI installation requires Pal to be stopped; start it afterward to load the
new generation. For online installation use `install_package` inside Pal and
check `inspect_package_status` for the coordinated activation result. For an existing
provider updated directly on disk, use
`manage_plugin(name="telegram", operation="reload")` at an idle boundary;
rescan alone does not reload existing provider code. If online installation
already activated the provider, do not reload it again.
