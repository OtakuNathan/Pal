# cache_reminder

Owns prompt-cache reminder policy, transient reminder state, cards, button
handlers and configuration. The first-party `cache_reminder` plugin is enabled
by default and can be detached, disabled or reloaded through PluginHost. PalCore
has no dependency on this package and works without the plugin.

LLM publishes cache facts through `LLMService` and persisted settings through
`LLM_SETTINGS`. The plugin observes those ports once and uses `CORE_CONTROL` for
lifecycle notifications, timers, queries, delivery and context-change leases.

A settled user turn registers two independent timer events: one to show the
reminder, another to withdraw it at expiry. The Core main loop executes the
callbacks; this component creates no timer tasks. User activity invalidates the
pair. A busy notice may register a bounded retry before the fixed expiry.
Disabling cancels pending reminders; unloading cancels even already queued timer
events, withdraws callbacks/configuration operations, and closes active cards.

The plugin registers its own compact, dismiss and disable actions. Every action
performs its effect and delivers its reply through Core. Core knows no reminder
button names, cache epochs or reminder settings. Claiming a notice and acquiring
context-change admission happen together under Core's coordination lock.

Confirmed warm-cache compaction requests at most three attempts with an
LLM-owned cache eligibility guard. Expired or replaced anchors never silently
become cold requests. An estimated reminder requests ordinary manual compaction
and makes no promise about cache reuse. LLM executes compaction; Memory owns the
captured history and installation.

`ModuleHandle.configuration_handlers` publishes `cache_reminder.inspect` and
`cache_reminder.configure` through the generic resident configuration router.
The user-facing CLI remains `pal core cache-reminder show/configure`. Online
configuration requires the plugin; offline configuration preserves existing keys
and reports activation on the next start. Timer state is process-local.
