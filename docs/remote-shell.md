# Native remote shell

This change adds execution targets to the native backend. Python shell behavior is
unchanged. Target `0` is always local; missing remote infrastructure never selects
another target or replays a command. This document describes source changes, not
an activated Pal installation.

## Ownership and implementation

| Owner | Implementation | Responsibilities |
|---|---|---|
| Resident execution | `execution/native_shell/router.py` | Public session IDs, target/Runtime tickets, unresolved operations, original delivery binding, bounded materialization |
| Existing shell owner/events | `execution/native_shell/runtime.py`, `events.py` | Output paging, L1 acknowledgment, recovery, original-channel completion |
| Optional plugin | `pal-shell-native/pal_plugin/pal_shell_remote/plugin.py`, packaged `remote` manifest | One local Hub process, attach/detach through `execution:native_shell_targets` |
| Hub and Slot | `pal-shell-native/pal_plugin/pal_shell_remote/hub.py`, `slot.py` | Target configuration, existing startup commands, strict SSH forwarding, fd leases, connection retirement |
| Separate worker package | `pal-shell-native/python/pal_shell_worker` | Authentication, native Runtime, operation journal, retained output, completion snapshots, machine metadata, privileged operation grants |
| Native extensions | `pal-shell-native/rpc_module.cpp`, `runtime.cpp` | Dynabridge RPC codec/dispatch, FF external async composition with libuv, native processes/PTY, optional output hard limit |

No changes to Dynabridge or FF are required. Their pinned source dependencies are
reused. The RPC `.def` projection is shell-specific: its typed binary payload is a
bounded MessagePack shell request. Native callbacks enqueue worker-loop events;
they never await network I/O. The local Hub uses Pal's existing sidecar IPC.

SSH opens a local Unix socket forwarding to the worker's private Unix socket.
It provides server authentication, public-key client authentication and encrypted
transport; the worker is not an SSH server. A separate enrolled Ed25519 identity
signs a nonce bound to client ID, worker ID and Runtime epoch. One endpoint belongs
to one client identity. Give Pal and Petra separate endpoints/identities if they
must not share ownership. Private keys never appear in tool parameters or results.

## Tool contract

- `run_shell(cmd, target=0, ...)`: existing shell arguments, plus execution location.
  `sudo=True` requests one approved privileged operation on a remote target; privileged
  PTYs are rejected. Ordinary remote PTYs remain supported.
- `shell_session(session_id, ...)`: positive public ID already binds target and
  Runtime epoch; no target override. Controls use separate operation IDs.
- `shell_reconcile(operation_id)`: query the original request after uncertain
  submission or PTY input. Does not submit a replacement command.
- `shell_recover_output(call_id)`: existing retained delivery recovery.
- `list_remote(refresh=False)`: always includes local target 0 and configured
  remote entries even when offline. Configured facts and timestamped worker probes
  are separate. Refresh probes without waking or starting machines.
- `remote_start(target, action)`: explicitly invokes a configured existing startup
  command. The action's exit code does not prove the machine or worker is ready.
- `remote_power(target, action="shutdown")`: a separate management action; not
  plugin detach, transport close or session termination. Requires one trusted approval for each shutdown request.
- `run_shell_desktop(...)`: fixed projection of the unique configured desktop
  target. It cannot accept an overriding target. Canonical action metadata, rather
  than exact public aliases, controls native handoff and write admission.

All command results identify their target. Paths belong to that target. Local file
tools cannot access a remote checkout; use the target shell when no corresponding
remote file capability exists. Workspace synchronization is outside this change.

Dynamic metadata includes OS/version, hardware and worker architecture, CPU
model/cores/quota, memory and cgroup limit, load, optionally NVIDIA GPU memory,
the actual verified Bash executable/version/invocation, active tasks, retained
output, power/privilege configuration and observation time. Unsupported metrics
are unknown, not invented. Mac desktop-session presence is separate from shell
readiness; GUI Automation/unlock permissions are not inferred from SSH access.

## Failure and delivery semantics

Connection failures before submission are `NOT_STARTED`. Lost confirmation after
submission is `UNKNOWN`, with an operation ID and reconciliation affordance.
Nonzero process exit is a process result, not a transport failure. Heartbeat or
SSH failure does not prove worker death or machine shutdown.

The worker records the operation ID and argument fingerprint before native
submission. Reusing the same ID with different arguments is rejected. Recorded
requests never execute twice. Journal records are retained for the whole Runtime
epoch (default capacity 4096); reaching capacity rejects new operations without
evicting deduplication records. They are not a durable crash-recovery journal.

Disconnecting the Hub or SSH does not signal worker sessions. Detach marks the
old port unavailable, cancels/drains local RPC operations, and retires its fd lease;
resident tickets and unacknowledged output stay owned by execution. Reattach can
access those tickets only on the same worker epoch. Old-generation replies cannot
establish ownership after detach. Reset refuses unresolved remote work.

Worker restart creates a new epoch. Old session state cannot be recovered and
previous command effects cannot be inferred. Unknown operations retain the write
barrier; management queries remain available. This version deliberately has no
force-forget operation for lost effects and no recovery across a Pal process
restart. An operator must reconcile effects before replacing that resident state.

Output is read only through authenticated worker-owned output snapshots, never an
arbitrary remote file path. Byte offsets are bounded to the observed snapshot.
Transfer uses at most 256 KiB per request, and materialization writes a local
private temporary file before decoding for the existing pager. The worker releases
retained bytes only after the ordinary Pal delivery acknowledgment. Release retry
is idempotent and does not repeat the completion model turn.

Default limits are 8 MiB combined output per execution, 128 MiB worker reservation,
64 MiB local retained materialization budget and one materialization at a time.
The worker reserves the per-execution allowance before admission. Native limited
runs pipe and count both streams while writing; an overrun terminates the command
and explicitly marks output incomplete. Local calls retain their prior unlimited
file-output behavior. The model's display budget is separate from these limits.

## Configuration and offline installation

Install matching versions of Pal and the independently built native package on
Pal's machine, then install the companion `plugin-remote-0.3.0.palpkg` into its
runtime. Hub/Slot and the plugin manifest are maintained in the native repository,
not shipped inside Pal or the remote worker binary. Build and install with the
existing package manager:

```sh
pal package build ../pal-shell-native/pal_plugin --output ../pal-shell-native/dist
pal package install ../pal-shell-native/dist/plugin-remote-0.3.0.palpkg --runtime-root <runtime-root>
```

If upgrading a runtime that previously provisioned the built-in `remote`, perform
the first package installation while Pal is stopped. The installer recognizes the
retired managed manifest and archives its entire directory under
`packages/previous/builtin/remote/` before publishing the companion plugin. It
restores the directory if publication fails. Other built-in identities and custom
manifests remain protected against replacement. This migration does not change
`config/remote.toml`, credentials, or remote workers; start Pal after installation.

The palpkg uses the host interpreter because its adapter shares Pal's ports and
native dependency. Its verify hook checks native Runtime/RPC and resident contract
availability; it never installs dependencies or restarts Pal. Install the matching
native wheel first. CLI installation prepares files; startup or authorized plugin
rescan/attach activates them. Existing resident execution changes still require
host activation. The package uses the ordinary community-plugin lifecycle,
including detach/reload, saved disable state and package rollback.

The worker machine only needs the worker package/binary and Bash,
not a second Pal core. Linux and macOS have the POSIX backend. Windows has an
experimental native PowerShell backend described below; it does not provide ConPTY.

The separately installed remote palpkg is enabled by default when the native execution port is
available. With no `config/remote.toml` or an empty target list, it attaches an empty
port without starting a Hub process or importing worker/RPC dependencies; local
shell remains available. Python execution does not expose the required native port.
Explicit persisted plugin enable/disable preferences remain authoritative.
Create `<runtime-root>/config/remote.toml` and reload remote to add targets.
Example (paths are target-specific installation choices):

```toml
[[targets]]
target = 1
name = "cloud-build"
worker_id = "cloud-build-worker"
client_id = "pal-main"
client_key = "/home/pal/.config/pal/remote-client.key"
socket_path = "/home/worker/.local/run/pal-shell/worker.sock"
ssh_host = "worker@cloud.example"
ssh_port = 22
ssh_identity = "/home/pal/.ssh/cloud-build"
known_hosts = "/home/pal/.ssh/known_hosts"

[targets.static]
usage = "Linux build host; checkout must be selected explicitly"

[[targets]]
target = 2
name = "desktop"
shortcut = "desktop"
worker_id = "desktop-worker"
client_id = "pal-main"
client_key = "/home/pal/.config/pal/desktop-client.key"
socket_path = "/Users/owner/.local/run/pal-shell/worker.sock"
ssh_host = "owner@desktop.example"
ssh_identity = "/home/pal/.ssh/desktop"
known_hosts = "/home/pal/.ssh/known_hosts"

[targets.start_actions]
wake = ["/absolute/path/to/existing-tested-wake-command", "desktop"]
```

`start_actions` references existing programs with literal argv. It is the reuse
point for the already tested network wake implementation; this change does not
implement WOL again. No configured startup action runs implicitly. Omitting
`ssh_host` connects to a local private Unix socket for loopback acceptance.

Generate a separate application signing identity in an owner-private directory:

```sh
pal-shell-worker --generate-client-key /private/directory/client.key
```

Only the public key is printed. Enroll that public key in worker TOML:

```toml
worker_id = "cloud-build-worker"
client_id = "pal-main"
client_public_key = "PUBLIC_ED25519_HEX_FROM_ENROLLMENT"
socket_path = "/home/worker/.local/run/pal-shell/worker.sock"
shell = "/bin/bash"
output_limit = 8388608
retained_limit = 134217728
operation_limit = 4096
shutdown_policy = "disabled"
```

The Unix socket directory must be owned by the worker user and mode 0700. Enroll
SSH host keys through a trusted channel; automatic host-key acceptance is disabled.
The worker refuses an existing socket path rather than unlinking another service.
After a crash, inspect ownership before explicitly removing a stale endpoint.

`pal-shell-worker --config /absolute/worker.toml --write-service /output/directory
--executable /absolute/pal-shell-worker` generates a systemd **user** unit on Linux
or LaunchAgent plist on Mac. It does not load, enable, start or restart anything.
The user decides activation. Linux logout survival depends on the user's service
manager configuration; Mac LaunchAgents depend on the login session. Disconnect
survival does not promise survival across logout, reboot or worker crash.

For a standalone bundle, use an isolated build environment with the native wheel
and PyInstaller, then run `pal-shell-native/packaging/build_worker.py`. The bundle
contains its Python runtime and extensions. Platform CI builds Linux x86_64/ARM64
and Mac ARM64/Intel packages; these are separate binaries, not one portable file.
The Intel runner label follows the [GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## Experimental Windows worker

The Windows process adapter reuses the existing C++ Runtime reactor, session state,
FF completion flow, worker journal and output protocol. It runs PowerShell scripts
through CreateProcessW, with a suspended launch assigned to a kill-on-close Job
Object before resuming. Only the child's standard handles are inherited. Job
termination also retires descendants; see Microsoft's [Job Objects documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

This prototype supports noninteractive PowerShell execution, background session
query/termination, deadlines, bounded output and transport reconnection. `tty=true`,
interactive input/resize, privilege management and machine power management are
unsupported. No Windows startup actions are configured. Do not interpret a successful
PowerShell command as ConPTY or GUI support. Windows Pal clients are outside this
prototype; the tested Pal client remains Linux.

Commands run as UTF-8 BOM `.ps1` files in the native session's output directory,
with UTF-8 console output and `$ErrorActionPreference = 'Stop'`. The invocation is
`-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File`; execution policy
is scoped to that process, not changed system-wide. Use explicit `exit N` where a
specific script exit code is required. Windows paths and PowerShell syntax belong
to this target; Bash assumptions and local file tools do not apply.

Build on Windows with the matching CMake, C++ toolchain and Python development
files. `packaging/build_windows_worker.ps1` in pal-shell-native assembles a complete
private Python distribution (with msgpack/cryptography), compiled `.pyd` modules
and worker package into a directory bundle. It does not install or start a service.
The native repository's independent `windows-prototype.yml` CI builds both
extensions on Windows x64 / Python 3.13, runs the Windows worker regressions,
repeats them from the assembled bundle, and uploads an experimental ZIP and
checksum. Windows remains outside the official release matrix; this job does not
validate Windows Pal clients or SSH deployment.

Windows uses an authenticated loopback-only TCP worker endpoint. In worker TOML,
set `tcp_port` to an available port (0 selects an ephemeral test port), `shell` to
the absolute PowerShell executable, `socket_path` to a private endpoint **JSON file**,
and `shutdown_policy = "disabled"`. This endpoint file records the actual port,
PID and Runtime epoch. Preserve the normal client/worker public-key enrollment.
In Pal target TOML set `worker_port` to that actual port and `socket_path = ""`.
The Slot forwards its local Unix socket through SSH to `127.0.0.1:worker_port`;
there is no public worker listener and no unencrypted network fallback.

The worker must run independently of OpenSSH's connection process. The tested
prototype uses a manually started, limited-privilege interactive-user Scheduled
Task with no triggers. It is not a system service, has no boot trigger and does not
promise survival across user logout or worker restart. Inspect task ownership and
existing endpoint identity before stopping or replacing an instance. Do not add
machine startup/shutdown capabilities to establish a shell connection.

Windows x86_64 / PowerShell 5.1 acceptance on 2026-09-14 passed five native worker
tests (including Unicode cwd, byte quota, deadlines and descendant termination)
and cross-machine Pal tests against the standalone bundle. The latter verified
UTF-8 stdout/stderr and exit code 7, a non-administrator token, killed SSH transport
with reconciliation and a single counted side effect, session termination after
reconnect, paging, and no-effect management/PTY rejection. The limited-user
`PalShellWindowsE2E` task has no automatic triggers. Bundle, connection configuration
and logs reside in the native repository's `build/remote-validation/windows/`.
This test instance is separate from live Pal activation.

## Privilege and machine lifecycle

A resident approval request uses the existing interaction/control delivery path.
It displays the exact target, epoch, command/script and working directory. Only
Approve once / Reject are offered; there is no Accept All or session-wide grant.
The server-held decision is tied to the original authenticated actor and route.
No `confirmed=True`, caller-provided actor or tool argument is approval authority.
After acceptance, the Hub signs the worker's ten-minute, single-use grant. The
worker verifies target, fingerprint, client identity, epoch, nonce and expiry.

The in-repository socket provider supplies `trusted_actor` from its actual session.
Other channel providers must supply their authenticated sender identity in
`InteractionResult.trusted_actor`; it must match the originating binding's
user/account/session identity. Providers without this evidence cannot approve.
External Telegram/other provider packages are not modified by this change.
Bunshin scopes do not inherit the resident remote port or its identities.

The user worker is not elevated. Linux 0.3 uses signed, fixed management
operations, without a stored sudo password or Secret Service session:

- `run_shell(target=1, sudo=True, cmd="apt update")`
- `run_shell(target=1, sudo=True, cmd="apt install cmake ninja-build")`

`apt-get` is also accepted. Extra options, paths, package versions, repository
changes, upgrades/removal, shell operators and arbitrary root commands are rejected
before approval. The worker normalizes action/package names; Pal displays those
same values for approval and signs the bound fingerprint. Installation uses fixed
`apt-get --assume-yes --no-remove install -- ...` argv, sanitized environment,
noninteractive stdin and process-group supervision. Distribution packages and their
maintainer scripts remain trusted system software.

Package names are literal: the protected helper escapes and anchors each operand
before passing it to apt-get. A name such as `bash.` cannot expand to other packages;
trailing `+`/`-` cannot become install/remove operators. Legitimate names such as
`g++` and `python3.11` retain their spelling in the approval.

The only NOPASSWD entry is a root-owned no-argument `/usr/local/libexec/pal-shell-manage`
launcher, pointing at a fully protected matching worker bundle. Never grant
NOPASSWD to apt, a shell, shutdown directly, or the old arbitrary-command helper.
The helper independently verifies the signed grant and root-owned target/client
policy, then durably reserves its operation ID before consuming the worker grant
and attempting execution. Duplicate grants never create another process. A crash
leaves an unknown record; querying is allowed, replay is not. The bounded root
journal must survive upgrades. Worker `query` includes `management_journal` evidence
for privileged operations; missing records do not prove absence of prior effects.
Grant availability is independent of the initial RPC snapshot: `wait_ms=0` can
return a running session while the helper still starts. Consumption is single-use;
process termination, execution failure or worker shutdown revokes any unused grant.

Run installation setup as the ordinary worker account in the user's own terminal:

```sh
pal-shell-worker --config /absolute/worker.toml --setup-sudo
```

Linux asks for target number, optional shutdown (disabled by default), protected
bundle path and output directory. It generates root policy, a fixed launcher,
sudoers, worker fragments and `NEXT_STEPS.txt`; no password is requested.
The final message prints the actual remote absolute path and quoted `cat` command.
An administrator reviews and installs these files and runs `visudo -c`. Preserve
identity/configuration and coordinate active sessions before activation. The wizard
does not install root files, change services or claim sudo E2E passed. `list_remote`
separates configured support from a timestamped helper/sudoers installation probe.
Verify an actual approved `apt update` separately.

macOS retains remote Keychain and protected askpass/privilege helpers. Its wizard
uses explicit `STORE` consent and the OS hidden prompt. Secrets never enter Pal
arguments, environment, PTY transcripts or checkpoints. Locked/denied Keychain fails
explicitly. Windows has no privileged operations or power. Linux management no
longer uses Secret Service; existing credentials are left untouched.

`remote_power(action="shutdown")` is separate from session/transport close. Enable
it only in the Ubuntu desktop's root policy and worker configuration; cloud,
Windows and Mac targets retain disabled power. Every shutdown requires trusted
approval. Worker draining and the busy check are atomic: active tasks, unresolved
execution or retained output reject it and reopen admission, without a queued
future shutdown. Protect the Pal host identity across aliases and configured
`protected_machine_ids`. Accepted does not prove power-off; SSH disconnect alone
cannot prove shutdown. No automatic wake follows.

## Acceptance and activation boundary

### macOS Intel maintenance worker acceptance

On 2026-09-14, an Intel Mac running macOS 14.6.1 built the matching native wheel
and standalone worker with an isolated Python 3.13 environment. Five native backend
tests and fourteen worker tests passed. The first standalone launch exceeded the
old ten-second test readiness window; retry passed, and the bounded startup window
was extended to thirty seconds. This does not change command execution timeouts.

The installed standalone worker passed cross-machine Pal acceptance: non-root
identity, killed SSH transport with unchanged Runtime and one counted side effect,
public PTY session recovery after detach/reattach, paging and final output release.
Startup and shutdown management calls were rejected without execution.

At the user's request the worker remains installed as an independent user
LaunchAgent, separate from Petra's files and process. It starts with that user's
login, has no machine startup/shutdown actions and no automatic worker crash-restart
policy. Its client signing key and target fragment are retained in the client's
private configuration directory. No live Pal target was activated, and Petra was
not restarted. Onboarding the live Pal still requires its matching native/remote
implementation and explicit target/plugin activation. Build and acceptance logs
are retained under the native repository's `build/remote-validation/macos/`.

Run matching working trees explicitly:

```sh
PYTHONPATH=../pal-shell-native/build/check:../pal-shell-native/python:src:native/shell_runtime/python \
  python -m unittest discover -s native/shell_runtime/tests -v
PYTHONPATH=../pal-shell-native/build/check:../pal-shell-native/python \
  python ../pal-shell-native/tests/test_remote.py
```

Coverage includes real RPC/PTY, lost submit confirmation and deduplication,
wrong client/Runtime identity, byte quotas, forged output snapshots, repeat release,
Hub process detach/attach, real isolated SSH forwarding and wrong host-key rejection,
transport cancellation, approval actor/route isolation and shutdown busy/expiry/host
protection. Tests use an isolated sshd when available; no production service is
restarted. The transport failure path also has a Valgrind stress check.

Existing Pal CI retains its published native pin, so remote integration explicitly
skips against that old package. Dispatch the native workflow with the exact matching
`native_ref` to require RPC integration. After the sibling change is published,
update the immutable pin; do not substitute an invented SHA for an uncommitted tree.

Cloud acceptance must record the actual remote checkout SHA and treatment of dirty
local changes. A passing remote test does not validate unsynchronized local edits.
Real sudo/vault access, root descendant cancellation, Mac GUI/login/logout and
physical wake/shutdown remain machine-level acceptance items. No live Pal deployment,
plugin activation, runtime configuration change or service restart is performed by
these source changes.

### Cloud acceptance, 2026-09-14

An isolated Debian 13 x86_64 cloud worker was built from the current native working
tree and pinned dependencies, using CPython 3.13 and a Debug build. Compilation
ran as UID 1000 with one job, 50% CPU quota, 600 MiB memory and 512 MiB swap limits.
The worker's 14 tests and standalone bundle acceptance passed on that host.

A local Pal test instance then accessed that standalone worker through strict SSH
host verification and enrolled RPC authentication. Commands executed as UID 1000.
Killing the actual SSH tunnel and dropping the submission acknowledgment preserved
the Runtime epoch; reconciliation recovered the command and its counted side effect
occurred once. An existing public PTY handle remained usable after backend detach,
tunnel loss and reattach. Large output passed through Pal's existing paging path.
The final worker probe reported zero active tasks, retained outputs and reservations.

The temporary worker was stopped after acceptance, then explicitly retained and
started again at the user's request for further integration. No boot service or live
Pal plugin was enabled; sudo and machine power actions were not exercised. The resulting
binary is a Debian 13 x86_64 validation bundle, not a manylinux portability claim.
Logs, source archive checksum and the bundle are retained locally under the native
repository's `build/remote-validation/cloud/` directory.

## Protocol-v2 activation and transport acceptance

Version 0.3.0 requires matching native wheel, remote palpkg and worker protocol 2.
Hello/authentication retain Dynabridge framing for explicit version negotiation;
authenticated messages wrap the unchanged Dynabridge payload in a 64-bit request ID.
There is no fallback to a serial worker. Dynabridge and FF are unchanged.

Each Hub and worker owns one persistent native libuv RPC loop and the existing FF
uv_executor. Socket acceptance, connected reads/writes, pending requests and timers
belong to that loop; Python callbacks enqueue events on their asyncio owner.
The native process Runtime retains its own reactor. Each connection allows 32
in-flight requests, 1 MiB frames and an 8 MiB send queue. Responses may arrive out
of order. Timeout/cancellation does not retire unrelated requests or repeat effects.
Slot admission asynchronously queues up to 128 additional requests, in FIFO order,
for at most 30 seconds. No lease or socket request is acquired while queued. Queue
cancellation/detach removes the request; overflow or waiting timeout returns
NOT_STARTED. The Hub port deadline includes this queue wait. After transmission,
the existing UNKNOWN/reconciliation contract still applies.
A partially transmitted/unconfirmed mutation remains UNKNOWN. Handshake and partial
frames have 30-second deadlines; authenticated idle connections have no 90-second
expiry. Request IDs do not replace operation IDs or Runtime epochs. Slot locking
covers connection admission/retirement, not round trips.

Failure before privileged commit clears the local ticket as NOT_STARTED; loss
after commit retains reconciliation state. Do not infer safety solely from an empty
epoch. Reconcile existing UNKNOWN operations before replacing resident code;
restarting just to erase evidence is not reconciliation.

Source/build artifacts do not activate services. Upgrade worker and client together
in a coordinated window, preserving sessions/output and root journals. Install
reviewed Linux management files manually, then verify ordinary execution, approved
apt update, denial and reconnect separately. Physical shutdown is a separate
owner-authorized E2E, never an installation test.
