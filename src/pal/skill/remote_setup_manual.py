"""On-demand remote onboarding manual; no remote dependency is imported here."""

PAL_REMOTE_SETUP_SKILL_ID = "pal.remote.setup"

PAL_REMOTE_SETUP_MANUAL = """# Pal Remote Host Setup

Help users install the independent remote worker and connect it to Pal's native
shell. Explain the concrete steps directly; do not make reading repository docs
a prerequisite. For implementation and package details consult docs/remote-shell.md.
This manual is discoverable even before the optional remote plugin is installed.
Its availability does not prove that this Pal process supports remote execution.

## Establish the host and authorized scope

Reuse known configuration and authorization. Ask only for missing connection
details: SSH destination/account/port, available key reference, target purpose,
and whether the user wants an isolated test or a persistent user service. Inspect
existing SSH configuration before asking the user to repeat it. Never ask them to
paste a private key, login password or sudo password into the conversation.

Verify host identity with trusted known_hosts enrollment and use noninteractive
public-key authentication. Do not bypass host-key checking to make setup work.
If an unknown host needs enrollment, provide its fingerprint for verification
through a trusted channel; a key obtained by scanning alone is not verification.
An authentication failure calls for checking the account/key enrollment, not
password collection through chat or a switch to another target.

Inspect actual OS/version, CPU architecture, Bash, available resources, worker
installations and user service ownership. Match both architecture and platform
runtime requirements of the artifact; a Debian-built binary is not automatically
portable to every Linux distribution. Linux/macOS workers use Bash. An experimental Windows worker supports native
noninteractive PowerShell sessions, but no ConPTY, power or privilege management.
Windows SSH success alone does not establish that this matching worker is installed.
Inspect actual support and use the Windows prototype instructions in docs/remote-shell.md.

On Pal's side verify the native execution backend, matching pal-shell-native/RPC
package, execution:native_shell_targets port, and remote plugin discovery.
The remote plugin is a companion palpkg maintained in pal-shell-native/pal_plugin;
it is not built into Pal. Install the matching native wheel in the host interpreter,
then use package_install (or pal package install for offline preparation) with
plugin-remote-0.3.0.palpkg. If the runtime still contains the retired managed
plugins/_builtin/remote manifest, the first migration requires an offline CLI
installation while Pal is stopped. The installer archives the old directory under
packages/previous/builtin/remote and restores it on publication failure. Do not
delete manifests or bypass the live-host lock; hand off this one-time stop/start
to the user. Remote configuration, credentials, and workers remain in place.
Its verification hook checks prerequisites; it does not
install dependencies or activate resident code. Python
shell does not support remote and must not silently fall back or change backend.
Missing resident support requires a separately planned Pal activation; never
restart the active Pal host from its own turn to finish onboarding.

## Prepare the remote worker and identities

Prefer a verified matching standalone worker bundle. Preserve its directory
contents, including bundled libraries. The remote machine needs the worker and
Bash, not another Pal core. Use a private directory belonging to the execution
user; an administrative SSH account is not a reason to run the worker as root.
Keep existing services and configuration intact. If building is necessary, use an
isolated environment and resource limits appropriate to the host's capacity.

SSH private keys stay on the client. RPC enrollment uses a separate Ed25519 client
identity, also private to the client. Reuse the enrolled identity when appropriate;
do not overwrite or rotate it during routine reconnects. Generate a new identity
only when needed, in an existing owner-private directory:

```sh
pal-shell-worker --generate-client-key /private/client/remote-client.key
```

That command prints only the public key. Copy the public key into remote worker
TOML, with distinct worker_id/client_id, an absolute private socket_path and the
verified Bash path. A minimal remote configuration is:

```toml
worker_id = "build-worker"
client_id = "pal-main"
client_public_key = "PUBLIC_ED25519_HEX"
socket_path = "/home/worker/.local/run/pal-shell/worker.sock"
shell = "/bin/bash"
shutdown_policy = "disabled"
```

The socket directory must belong to the worker user and have mode 0700. Never
unlink an existing socket until its owner and liveness are established. Start the
worker independently of the SSH connection: native Runtime owns the processes,
PTYs and output. No tmux layer is required for connection-loss survival.

For persistent installation, generate the platform's user-service definition:

```sh
pal-shell-worker --config /absolute/worker.toml --write-service /staging/service --executable /absolute/pal-shell-worker
```

This only writes a systemd user unit or Mac LaunchAgent plist. Inspect its paths
and existing service state, then install/load it within the user's authorized
scope using the actual platform service manager. Do not confuse writing the file
with starting a service. Honor existing authorization without repeated approval.
If user interaction is required, prepare the exact remaining command and expected
check. Explain the relevant login/logout and reboot lifetime: a user agent is not
a system daemon. An isolated test worker need not become an auto-start service.

## Connect Pal and expose only supported operations

Preserve existing entries in <runtime-root>/config/remote.toml. Allocate an unused
positive target ID; 0 is permanently local. Add a [[targets]] entry with target,
name, worker_id, client_id, client_key (local private-key path), socket_path (remote
path), ssh_host, ssh_port, ssh_identity and known_hosts (both local paths). Use
explicit paths. Do not copy actual credential values into configuration examples,
tool parameters, results, logs or checkpoints. One worker endpoint has one
authenticated client identity; use separate endpoints for separate ownership.

Leave start_actions absent unless the machine has an explicitly configured startup
method. Reuse existing tested wake commands rather than implementing WOL again.
For a cloud shell-only target, leave shutdown_argv absent and shutdown_policy
disabled. For a Windows target without a configured wake method, report startup
as unsupported. Unsupported management calls return a clear no-effect result;
never substitute a shell shutdown command, sudo, reboot or another target.

list_remote reports configured capabilities and timestamped probes separately.
Unprobed is not unsupported, and offline does not make a configured target vanish.
Shell readiness does not prove GUI/Automation permission or a logged-in desktop.
Sudo setup is optional and separate. Matching 0.3.0 worker/native/palpkg use
protocol 2. Linux uses signed sudoers management without a stored password or
unlocked Secret Service dependency. Only apt/apt-get update and apt/apt-get install
PACKAGE... are accepted through run_shell(sudo=True), without extra flags, package
files or root shell scripts. Each operation requires trusted approval of normalized
action/packages. Shutdown remains remote_power, enabled only for a configured
Ubuntu desktop; cloud, Windows and Mac power remain disabled.

The machine owner runs
`pal-shell-worker --config /absolute/worker.toml --setup-sudo`
in their own remote terminal as the ordinary worker account. Linux selects target,
optional shutdown (default disabled), protected bundle and output paths. It generates
sudoers, a fixed no-argument root helper, policy and worker fragments. Never allow
NOPASSWD for a shell, apt itself or the old arbitrary-command helper. An administrator
reviews files, installs the complete protected bundle and runs visudo -c. The wizard
does not restart services. Retain the root journal on upgrades/rollback; never remove
it to retry an unknown operation.

macOS retains Keychain and protected askpass: the OS tool prompts without echo.
Never enroll passwords via Pal shell/PTY or conversation. Windows has neither sudo
nor power management. Ordinary shell remains available without sudo setup.
Always hand off the actual remote absolute path to NEXT_STEPS.txt and a shell-quoted
cat command; do not merely say "follow NEXT_STEPS.txt". The default remote directory
is ~/.local/share/pal-shell-sudo-setup/setup-*/. If lost, inspect filenames; do not
repeat credential enrollment just to locate instructions. Distinguish generated
templates, installed protected helpers, installation probe and an approved operation
verified. On Mac also distinguish stored credentials from readable Keychain.
Metadata supported/configured alone is not E2E. On Linux verify approved apt update;
never enable power to test access. PTY prompts never authorize credential injection.

When authorized to connect the live instance, discover the actual plugin controls.
Rescan discovers a new manifest; enable attaches a disabled remote plugin; attach
reloads an existing enabled generation. Do not repeat activation already completed
by an installer. Remote defaults to enabled when native execution is available;
missing/empty remote.toml attaches with no remote targets or Hub process. Existing
explicit disable preferences are preserved. After adding target configuration,
reload the enabled plugin rather than requiring an extra enable step.
Configuration edits alone do not reload the Hub. Coordinate any
existing sessions and pending output before reload; detach does not kill remote
tasks or erase their resident tickets. Do not reset away unresolved execution.

## Verify and hand off

Refresh list_remote and verify the intended target, OS/architecture, actual shell,
capacity and supported management operations. Run a harmless command through
run_shell(target=...), checking execution identity and remote cwd. Explain that
local file tools do not edit remote files; record the exact remote checkout and
how local uncommitted changes were transferred before claiming remote test results.

For an isolated acceptance task, disconnect the transport and reconnect to the
same Runtime; verify the original session/output is accessible. A lost submission
acknowledgment is UNKNOWN and calls for shell_reconcile, never a fresh command.
Check a PTY when supported, output paging/recovery, completion delivery and output
release. Worker restart changes Runtime identity and is not connection recovery.
Avoid fault injection against unrelated live tasks.

Report separately: files installed, worker running identity, user-service/autostart
state, Pal configuration and plugin activation, verified behavior, and unsupported
or untested capabilities. Preserve the worker when the user requests continued
use; otherwise clean up only the isolated resources created for the test. Give
the actual retained paths and next action. Never claim a saved config or successful
SSH login means Pal already has a working remote execution target.
"""
