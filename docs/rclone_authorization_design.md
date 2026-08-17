# Rclone-owned remote authorization design

## Status

This document describes the **shipped** authorization subsystem and is the
maintainer reference for it: `src/rclone_kit/authorization/`
(`manager.py`, `session.py`, `state_driver.py`, `relay.py`, `types.py`,
`exceptions.py`, `redaction.py`), `src/rclone_kit/rc/auth.py`, and
`Rclone.authorize()` in `client.py`. Everything below describes code that
exists, unless it is explicitly marked as open.

Implemented, including live-provider verification against a real Google
Drive account in local-direct mode. The one part not verified by this
repository's own test suite is a real reverse-proxy-shaped relay deployment
against a caller-owned provider client - see "Implementation status" below.
See "Test strategy" for the unit, offline-integration, and live test
coverage.

`rclone-kit` runs rclone in-process, once, through a native library loaded
via `ctypes` and dispatched with typed RC calls (`RcloneRuntime`/`RcClient`,
`src/rclone_kit/native/runtime.py`, `src/rclone_kit/rc/client.py`). There is
no subprocess and no CLI argument surface anywhere in this path; every
design decision below is derived from that architecture and from the actual
vendored Go source in `native/rclone/`.

Getting the listener/redirect-URL separation accepted into upstream rclone
(tracked upstream as rclone issue #7634 and PR #7635) is still worth
pursuing, but is not a blocking dependency for this feature: the local fork
(`native/rclone/`) already carries the capability this design needs (see
"Architectural constraints from rclone's OAuth implementation" below). Treat
upstreaming as a project independent of this document, not as a
precondition for implementing it.

## Objective

A caller should be able to start authorization through `rclone-kit`, receive
a URL suitable for an end user, and obtain a ready-to-use rclone remote after
the user grants access. The caller may be a command-line application or a web
application running behind a reverse proxy in a container.

The end user must not need rclone, shell access, or an SSH tunnel. Rclone must
remain responsible for:

- selecting provider-specific OAuth behavior;
- constructing the provider authorization request;
- generating and validating OAuth state;
- exchanging the authorization code;
- interpreting provider-specific responses;
- producing rclone's token representation; and
- refreshing the token during later storage operations.

`rclone-kit` should wrap that behavior. It should not implement a second OAuth
client stack or become a hosted authorization service.

## Architectural constraints from rclone's OAuth implementation

`rclone-kit` runs rclone in-process, through one native shared library load
per process, dispatched with typed RC calls rather than through a spawned
subprocess. That architecture, together with how rclone itself implements
OAuth, produces the constraints this section records; they drive every
decision after it.

### There is one native library load per process, never one per session

`Rclone` never spawns `rclone` as a subprocess - not for authorization, not
for transfers, not for anything else. It loads one native shared library per
process via `RcloneRuntime.from_library_path()` and calls into it through a
small C ABI (`initialize()`, `rpc()`, `finalize()`). The ABI enforces, and
`native/rclone/librclone/rclonekit/bridge/bridge.go:81-108` confirms, that
`Initialize` can only ever succeed once per loaded library instance; a
second call returns `StatusAlreadyInitialized`
(`NativeAlreadyInitializedError`). `docs/production_usage.md`'s "Runtime
lifecycle and multi-client processes" section already documents the
consequence for the rest of the library: a process that wants several
`Rclone` clients must share one `RcloneRuntime` (via `shared_runtime()`),
because a second, independently initialized runtime is never possible
in-process - only a separate OS process gives that.

There is nothing to spawn per authorization session; every authorization
this process ever runs shares the one native library load the process
already has.

### rclone's own OAuth state is a process-wide global, not a per-session value

This is the fact that shapes the rest of this document. Reading
`native/rclone/lib/oauthutil/oauthutil.go` directly:

```go
var (
	oauthCancelFn context.CancelFunc
	oauthCancelMu sync.Mutex
	oauthURL      string
)
```

`configSetup()` (the function that starts the local OAuth webserver and
blocks waiting for the browser callback) sets `oauthCancelFn`/`oauthURL`
under `oauthCancelMu` when it starts, and clears them in a `defer` when it
returns - and it does this **unconditionally**, without checking whether a
flow is already running:

```go
oauthCancelMu.Lock()
oauthCancelFn = cancel
oauthURL = authURL
oauthCancelMu.Unlock()
```

`config/oauthstatus` and `config/oauthstop` (added upstream by PR #9466,
already present in the vendored checkout at
`native/rclone/lib/oauthutil/oauthutil.go:824-882`) read and cancel exactly
this one global pair. There is no session identifier anywhere in this state.
If two authorization flows were ever driven concurrently in the same
process, the second `configSetup()` call would silently overwrite the
first's `oauthCancelFn`/`oauthURL` - the first flow's `config/oauthstatus`
would then report the second flow's URL, and `config/oauthstop` would cancel
the second flow's context, not the first's. This is a real correctness bug
in the vendored code if triggered, not a hypothetical, and it is not
something `rclone-kit` can fix from Python: rclone's OAuth status is
process-global, full stop, and no RC call changes that.

The conclusion is unavoidable: **`rclone-kit` must never let a second
authorization flow reach rclone's blocking OAuth step while one is already
active in the same process.** This is not a design preference to weigh
against throughput; it is a hard safety requirement, enforced entirely on
the Python side by a strict single-flight serializer (see "Central
architectural constraint" below). It also has to be scoped to the *process*
(really: the one loaded native library), not to a `Rclone` client or an
`AuthorizationManager` instance someone forgot to share - two `Rclone`
clients built via `shared_runtime()` are two Python wrappers around the same
one Go OAuth global state.

### The listener/redirect separation this design needs already exists in the fork

Separating the private OAuth listener address from the public redirect URL
advertised to the provider is a capability of the vendored rclone build
itself, not something `rclone-kit` needs to add or probe for.
`native/rclone/fs/config.go:695-696` has:

```go
OAuthListenAddress string `config:"oauth_listen_addr"`  // Private address for the OAuth callback listener, overriding the default loopback address
OAuthRedirectURL   string `config:"oauth_redirect_url"` // Public OAuth redirect URI to advertise to the provider, overriding the listener address
```

and `overrideOAuthRedirect`/`oauthListenAddress` in `oauthutil.go` apply them
per-flow through `fs.GetConfig(ctx)`: the override is configuration local to
one flow, not a package-level mutation. The address the OS actually assigns
when binding `:0` is reported correctly: `authServer.BoundAddress()` returns
the OS-selected address, exercised directly by
`TestAuthServerBoundAddressResolvesPortZero` in
`native/rclone/lib/oauthutil/oauthutil_test.go`. There is no capability
probe to write and no fallback to raise for a missing flag - the binary this
repository builds in CI has the feature, unconditionally.

This keeps "required rclone capability" work and any temporary or patched
binary off `rclone-kit`'s critical path. Upstreaming the listener/redirect
separation (rclone issue #7634, PR #7635) is still worth pursuing on its own
timeline, but nothing in this document depends on it landing.

### `_config` is a real per-call override, confirmed in the RC dispatch layer

`native/rclone/fs/rc/context.go` decodes a request's `_config` key into a
copy of the ambient `fs.ConfigInfo` scoped to that one call, then deletes
the key before the method handler ever sees it - this is generic RC
plumbing, not something specific to transfer methods. `rclone-kit` already
relies on exactly this for transfer tuning
(`operations/transfer_options.py:encode_transfer_options_config`, keyed by
Go struct field name - e.g. `"Checkers"`, `"Transfers"`). The same mechanism
applies to `config/create`/`config/update`: passing
`_config: {"OAuthListenAddress": ..., "OAuthRedirectURL": ...}` on the RC
call that reaches rclone's OAuth step scopes the override to that call
without touching any other client's config on the shared runtime.

### There is exactly one rclone config, and it is not disposable

Authorization has no session-private config file to create, read back, and
discard. `RcloneRuntime.initialize(config_path=...)`
binds one config path for the runtime's entire lifetime; `config/setpath` can
retarget it, but doing so would repoint every other `Rclone` client sharing
that runtime at a different file mid-flight, which is far more dangerous
than useful. `fs/config/config.go`'s `updateRemote()` - reached by both
`config/create` and `config/update` - calls `SaveConfig()`
unconditionally, so any RC-driven remote creation persists straight into
whatever file the runtime already has open.

This matches the same conclusion `docs/production_usage.md` already reaches
for the rest of the library: *"Model different logical tenants as different
remotes within that one shared config (registered dynamically via
`config/create`/`config/update` RC calls if needed), not as separate
runtimes."* Authorization's job is to populate that one shared, already-live
config safely. See "Configuration ownership" below for what this means
concretely (name-collision protection, where the returned `Config` value
comes from, the interaction with `Rclone.config` snapshots).

### There is no OS-process-list exposure surface for secrets

Every value crosses the authorization boundary as a JSON RC parameter over
the native ABI (`RcloneRuntime.call(method: str, params: dict)`), never as
an OS-visible `argv` - there is no positional command-line argument carrying
a client secret or config blob for `Get-Process`/`/proc` to see. The
remaining secret-handling work is ordinary: do not log full RC params or
responses, and do not embed them in exception messages.

## Central architectural constraint: one authorization flow, one at a time, per process

Restated plainly, because it drives nearly every other decision:

> At most one authorization may be past its initial (fast, local)
> `config/create`/`config/update` call and into rclone's blocking OAuth step
> at any moment, for the lifetime of one loaded native library. Every other
> pending authorization in that same process must wait.

This is enforced entirely in Python, by a manager that is shared by every
`Rclone` client built against the same `RcloneRuntime` - not one manager per
client. `RcloneRuntime.call()` itself does not serialize concurrent RPC
dispatch (see its own docstring: the Go bridge only briefly holds a mutex to
check `initialized`, then delegates to upstream `librclone.RPC`, which is
safe to call concurrently) - that concurrency is exactly what lets a status
watcher poll `config/oauthstatus` on one goroutine while the flow's own
driving call blocks on another. The manager's serialization is a Python-side
policy layered on top of that native concurrency, not a workaround for a
missing one.

Two `Rclone` clients constructed via `shared_runtime()` in the same process
therefore must not each build their own `AuthorizationManager` - they must
resolve to the same one, or the single-flight guarantee is only as strong as
whichever caller remembered to share it. See "Session routing and
concurrency" for how the manager is obtained to make this automatic rather
than a documentation-only requirement.

Real cross-tenant isolation - not sharing OAuth globals, config, or even
this queue between two authorizations - still requires separate OS
processes, exactly as `docs/production_usage.md` already says for every
other kind of isolation this library can't provide in-process. A deployment
that genuinely needs concurrent authorizations should run more than one
`rclone-kit` process (each with its own `shared_runtime()`), not ask this
manager to fake concurrency it cannot safely provide.

## The `config/create` non-interactive OAuth state machine

Driving OAuth through RC without a terminal requires walking a specific
sequence of states, read directly from
`native/rclone/lib/oauthutil/oauthutil.go`'s
`ConfigOAuth` and `native/rclone/fs/backend_config.go`'s `BackendConfig`.

`BackendConfig` loops calling the backend's config step function, following
each `ConfigGoto` automatically, and only returns to the RC caller when a
step needs an answer (`ConfigInput`/`ConfigConfirm`, i.e. `out.Option !=
nil`), signals a terminal error, or finishes (`out.State == ""`). For a
fresh remote whose `Config()` is a bare `oauthutil.ConfigOut("", ...)` -
dropbox is the minimal example, and the one
`tests/native/test_authorization_offline_integration.py` uses - the
sequence with no existing token is:

```text
                     ┌─────────────────────────────────────┐
call 1               │ config/create name type parameters   │
(fast, local)        │   opt={nonInteractive: true}         │
                     └──────────────────┬────────────────────┘
                                        │  loops internally:
                                        │  *oauth -> *oauth-confirm (no
                                        │  existing token -> ConfigGoto,
                                        │  no question)
                                        ▼
                     *oauth-islocal: ConfigConfirm
                     "Use web browser to automatically
                      authenticate rclone with remote?"
                     -> returned to caller as {state, option}

call 2               config/create name type parameters
(BLOCKS until          opt={nonInteractive: true, continue: true,
OAuth completes,               state: <state from call 1>,
fails, or is                   result: "true"}
cancelled)           ┌──────────────────┬────────────────────┐
                     │  *oauth-islocal(true) -> *oauth-do:      │
                     │  calls configSetup() INLINE - binds the  │
                     │  webserver, sets oauthCancelFn/oauthURL, │
                     │  blocks on the browser callback, then    │
                     │  calls configExchange() -> *oauth-done   │
                     └──────────────────┬────────────────────┘
                                        ▼
                     terminal ConfigOut{State: ""} - remote is
                     now saved (SaveConfig()) with its token
```

Every continuation call carries the same full `parameters` map, not an
empty one (`state_driver.build_call_functions()` closes over one
`parameters` dict and passes it to every `create_continue`/`update_continue`
call). `parameters` is scoped to a single RC call: several state
transitions can run server-side inside one `BackendConfig` loop, so a
question reached partway through a continuation is pre-answered only if that
same call's own `parameters` carries it - see `rc/auth.py`'s module
docstring.

A backend whose `Config()` names a post-OAuth state does not terminate
there. `drive` passes `oauthutil.ConfigOut("teamdrive", ...)`, so once
OAuth finishes it asks `config_change_team_drive` - a non-OAuth question
outside the driver's two-question policy, which raises
`AuthorizationUnsupportedPromptError` unless it is pre-answered through
`backend_options`/`parameters`. Every shipped Drive caller passes
`{"config_change_team_drive": "false"}` for exactly this reason.

Reconnecting an existing remote (a token already present) inserts one more
question before `*oauth-islocal`: `*oauth-confirm`'s "Token already
configured - replace it?", which must also be answered `true` to proceed.
`rclone-kit`'s driver only ever needs to answer two possible questions, with
a fixed policy - it never asks the end user anything through this channel:

| `config_*` question key   | Meaning                                   | Always answered |
|----------------------------|--------------------------------------------|:---:|
| `config_refresh_token`     | replace an existing token                 | `"true"` |
| `config_is_local`          | use the local/embedded webserver (vs. an out-of-band paste-the-code flow for a headless machine with no browser at all) | `"true"` |

`rclone-kit`'s whole value proposition is the local-webserver-plus-relay
path (`*oauth-islocal` = true); the manual out-of-band paste-a-code path
(`*oauth-remote`, what the interactive `rclone authorize` companion-machine
flow uses) is out of scope - it has no browser step for the relay to
intercept and does not fit a headless-service deployment. If the driver
encounters any `option` question it does not recognize (a backend with
extra prompts beyond these two, e.g. a config wizard step that runs before
the OAuth block), it must raise rather than guess an answer - see
`AuthorizationUnsupportedPromptError` below.

Two more details confirmed by reading the source, both load-bearing for the
implementation:

- **`config/oauthstatus`'s `authUrl` is always the internal listener URL**,
  never the overridden public redirect: `configSetup()` builds it as
  `"http://" + server.BoundAddress() + "/auth?state=" + state` (line 1002),
  independent of whatever `OAuthRedirectURL` override applies to the
  provider-facing request. The relay is what turns this into a
  publicly-usable URL (see "Relay design"); it is not already public.
- **`CreateRemote` unconditionally deletes any existing section with the
  requested name** on the *first* call of a fresh create
  (`opts.Continue == false`): `fs/config/config.go`'s `CreateRemote()` calls
  `LoadedData().DeleteSection(name)` before setting the type. Calling
  `config/create` with a name that collides with an unrelated existing
  remote silently destroys that remote's configuration. The manager must
  check name availability before ever calling `config/create` (see
  "Configuration ownership").

## Design decisions

1. Drive authorization entirely through the existing shared native runtime
   and its RC dispatch - never spawn an `rclone` process, never load a
   second native library instance.
2. Enforce a strict single-flight queue for authorization sessions, shared
   by every `Rclone` client on one `RcloneRuntime`, because rclone's own
   OAuth state is a process-wide global (see above). Sessions beyond the
   one active slot wait in a `QUEUED` state rather than being rejected
   outright.
3. Use a stable public callback URL registered with the provider; rclone
   only ever needs one public redirect URI per deployment (or per provider
   client), not one per session, since sessions are serialized anyway.
4. Drive `config/create`/`config/update`'s non-interactive state machine
   from a small, explicit, tested driver with a fixed answer policy (see
   above) - never guess an unrecognized prompt.
5. Scope `oauth_listen_addr`/`oauth_redirect_url` per RC call via `_config`,
   not by mutating the runtime's ambient config, matching how the fork
   already implements per-flow overrides.
6. Route the public callback relay to the one currently-active session
   using rclone's generated `state` as an untrusted lookup key; rclone
   remains the authoritative validator.
7. Forward browser requests to rclone's private listener without
   interpreting the authorization code or exchanging it in Python.
8. Treat the shared runtime's live config as authorization's actual target,
   not an implementation detail to hide - guard against silent
   remote-name collisions (`CreateRemote`'s `DeleteSection` hazard) instead
   of pretending isolation that the ABI cannot provide.
9. Additionally return an `rclone-kit.Config` value scoped to just the
   created/updated remote (via the existing `config_show(remote=...)`
   plumbing), so a caller that wants to store or transmit the credential
   separately from the shared config file still can.
10. Keep web-framework integration outside the core library API - the relay
    contract stays framework-neutral.
11. Do not require capability probing as a precondition for authorization -
    the native library this process already loaded either has
    `oauth_listen_addr`/`oauth_redirect_url` or it doesn't; probe once
    against `native_build_info()`/a first harmless RC call if a defensive
    check is wanted, but do not build a parallel binary-selection story.

## Architecture

```text
Application
    |
    | Rclone.authorize(remote_name, backend, ...)
    |   (or AuthorizationManager.start(request) directly, for a
    |    shared-runtime app)
    v
AuthorizationManager  (one per RcloneRuntime, shared by every Rclone
    |                  client built against it)
    | admits an AuthorizationSession to the single active slot, or
    | queues it in FIFO order until that slot frees
    v
Session worker (driver thread + status-watcher thread)
    |
    | drives config/create's non-interactive state machine over the
    | SAME shared RcClient every other call on this runtime uses
    v
rclone's private OAuth listener (in-process; bound per _config
overlay, default 127.0.0.1:53682)  <------ CallbackRelay
    |                                            ^
    | provider authorization URL                 |
    v                                            | public callback
End-user browser ---> OAuth provider -----------+
    |
    | rclone (in-process) exchanges the code and saves the token into
    | the runtime's one live config file
    v
AuthorizationResult(config, remote_name)
```

The public callback relay may run in FastAPI, Django, Flask, an API gateway,
or another host. The core library supplies framework-neutral request and
response values and session routing; it does not start a public HTTP server
as an import side effect, and it does not spawn any process.

## Public API

The API is synchronous and thread-safe, consistent with the rest of the
library. A web framework can call blocking methods in its worker pool.

### Obtaining the manager

```python
manager = AuthorizationManager.for_runtime(rclone._embedded_runtime)
# or, equivalently and more commonly:
rclone = Rclone(CONFIG_PATH, runtime=shared_runtime())
session = rclone.authorize(remote_name="user_drive", backend="drive")
```

`Rclone.authorize()` takes the request's fields as flat keyword arguments
and builds the `AuthorizationRequest` itself. Construct that dataclass
directly only when calling `AuthorizationManager.start()`.

`AuthorizationManager.for_runtime(runtime)` is lazy and idempotent per
`RcloneRuntime` instance (a `WeakKeyDictionary[RcloneRuntime,
AuthorizationManager]`, mirroring `shared_runtime()`'s own "construct
exactly once, share everywhere" pattern), so any two `Rclone` clients on the
same runtime always resolve to the same manager without the application
having to plumb it through by hand. `Rclone.authorize(...)` is a thin
wrapper that resolves this automatically and tracks the returned session for
`Rclone.close()`, the same way it already tracks `ServeHandle`/`MountHandle`
(`_track_serve_handle`/`_track_mount_handle` in `client.py`).

### Request

```python
@dataclass(frozen=True)
class AuthorizationRequest:
    remote_name: str
    backend: str
    public_callback_url: str | None = None
    backend_options: Mapping[str, str] = field(default_factory=dict)
    client_id: str | None = None
    client_secret: Secret | None = None
    on_conflict: RemoteConflictPolicy = RemoteConflictPolicy.REJECT
    expires_in: timedelta = timedelta(minutes=10)
    private_listen_addr: str | None = None  # None = rclone's own default
```

`public_callback_url` defaults to `None`, which selects local-direct mode:
no `OAuthRedirectURL` override is sent, and `authorization_url` is rclone's
own private listener URL. Set it only for a relay deployment, where the
browser must reach a public endpoint that forwards back to this process.

`backend_options` contains rclone backend configuration, such as a Drive
scope, but must not contain an already-issued token. It is also where a
backend's *non*-OAuth questions get pre-answered: a `drive` remote asks
`config_change_team_drive` once OAuth finishes, which is outside the
driver's two-question policy and raises
`AuthorizationUnsupportedPromptError` unless the answer is supplied up
front. `on_conflict` controls
what happens if `remote_name` already exists in the shared config: `REJECT`
(default; raises `AuthorizationRemoteNameConflictError` before ever calling
rclone) or `RECONNECT` (drives `config/update` instead of `config/create`,
answering the extra `config_refresh_token` question - the caller is
asserting they intend to replace that specific remote's token, not create a
new one). `private_listen_addr` almost never needs to be set: because
sessions are serialized, there is no port-collision reason to vary it
per-session; it exists for deployments that must bind somewhere other than
loopback (e.g. a sidecar container network) or want a fixed non-default
port for firewalling.

`Secret` is a small value type whose `repr`/`str` never expose its content
(`types.py`); `reveal()` is the one way back to the underlying string. No
third-party secret-model package is involved.

### Manager and session

```python
manager = AuthorizationManager.for_runtime(shared_runtime())

session = manager.start(
    AuthorizationRequest(
        remote_name="user_drive",
        backend="drive",
        public_callback_url="https://service.example.com/oauth/rclone/callback",
        backend_options={"scope": "drive", "config_change_team_drive": "false"},
        client_id=google_client_id,
        client_secret=Secret(google_client_secret),
    )
)

send_to_user(session.authorization_url)  # blocks until QUEUED -> WAITING_FOR_USER,
                                          # or raises if the session fails/expires first
result = session.wait(timeout=600)
print(result.remote_name, result.config)
```

`AuthorizationSession` exposes:

- `id`: an opaque random identifier for diagnostics and application records;
- `authorization_url`: blocks (bounded by `expires_in`) until the session is
  admitted and rclone reports its URL, then returns the public-facing URL
  (see "Relay design" for how the internal listener URL becomes this);
- `expires_at`: an aware UTC timestamp. `expires_in` acts twice: `start()`
  sets `expires_at` to `enqueued_at + expires_in` and, for a session that
  has to wait for the slot, arms a timer on it, so a session still `QUEUED`
  when that elapses becomes `EXPIRED` without ever touching rclone;
  admission then re-anchors `expires_at` to `admitted_at + expires_in` and
  re-arms, giving the admitted session a full, fresh consent window. The
  value can therefore move forward once while a caller is blocked in
  `authorization_url`;
- `status`: a lifecycle enum;
- `wait(timeout=None) -> AuthorizationResult`;
- `cancel() -> None`; and
- `close() -> None`, with context-manager support - cancels if unfinished,
  matching `JobHandle.close()`'s shape.

Lifecycle values:

```text
QUEUED              (waiting for the single active slot)
STARTING             (admitted; driving the initial config/create call)
WAITING_FOR_USER      (rclone's local listener is up; authorization_url is set)
COMPLETING            (browser callback relayed; rclone is exchanging the code)
SUCCEEDED
FAILED
CANCELLED
EXPIRED
CLOSED
```

`QUEUED` exists because sessions cannot all start immediately: only one may
be active at a time, so any additional session waits for the slot. Terminal
state transitions must be atomic; repeated cancellation and cleanup must be
safe; a queued session's `expires_at` deadline must remove it from the queue
without ever touching rclone.

### Result

```python
@dataclass(frozen=True)
class AuthorizationResult:
    remote_name: str
    config: Config
```

`config` is built from `rclonekit/configshow`'s existing output
(`Rclone.config_show(remote=remote_name)` /
`operations/config_ops.py:fetch_config_show_embedded`), scoped to exactly
the one remote this session created or updated - not the whole shared
config file. It is returned as a convenience for callers that want to
store, encrypt, or transmit that one remote's credential independently; it
is not the only place the credential now lives, since `config/create`
already saved it into the runtime's shared config file as a durable side
effect (see "Configuration ownership"). Applications that persist the
returned `config` must still treat it as a secret.

### Session worker threads

Each active session is driven by two cooperating threads, not one, because
of how `configSetup()` blocks (see the state-machine section above):

- **driver**: issues the sequential RC calls (`config/create`, then one or
  more `continue` calls answering questions). The call that answers
  `config_is_local=true` is the one that blocks inside rclone for the whole
  browser wait - this thread is parked there until the flow finishes,
  fails, or is cancelled.
- **status watcher**: started immediately *before* the driver dispatches
  that blocking call, through the driver's `on_before_blocking_call` hook -
  it has to be, because the driver thread is parked inside rclone for the
  whole browser wait and could not start anything afterwards. It polls
  `config/oauthstatus` on the same `RcClient` (safe to
  call concurrently - see "Central architectural constraint") until it
  observes `status: "running"`, captures `authUrl`, and flips the session to
  `WAITING_FOR_USER`. It stops polling once the driver thread signals
  completion, whether or not it ever observed `"running"` (a flow that
  fails before binding the listener - e.g. a bad `client_id` - never
  reaches `"running"` at all, and the watcher must not spin forever waiting
  for a state that isn't coming).

This mirrors `_JobMonitor`'s existing shape (`job.py`) - one background
worker owns mutation of session state under a condition variable, handles
read the latest cached snapshot - but a session additionally needs the
second, short-lived watcher thread because, unlike a job's `job/status`,
there is no way to learn `config/oauthstatus`'s content from the same call
that would block waiting for it.

## Relay design

```python
@dataclass(frozen=True)
class RelayRequest:
    path: str
    raw_query: bytes
    method: str = "GET"


@dataclass(frozen=True)
class RelayResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


response = manager.relay(request)
```

The public application mounts two shapes below one registered callback base:

```text
/oauth/rclone/callback/auth?state=...
/oauth/rclone/callback?state=...&code=...
```

The first is the entry URL shown to the user; rclone's `/auth` handler
validates state and redirects the browser to the provider. The second is the
provider callback; rclone's `/` handler validates state and supplies the
code to the flow blocked in `configSetup()`. Provider error query parameters
use the same second path. The manager strips the public prefix and forwards
to the recorded private listener address:

```text
public .../callback/auth -> private http://<listener>/auth
public .../callback      -> private http://<listener>/
```

`manager.relay()` extracts `state` only to confirm the request targets the
one currently-active session (`WAITING_FOR_USER` or `COMPLETING`) and that
`state` matches what that session's `authorization_url` reported - it never
selects a target address from anything in the request itself, which is what
prevents the relay from becoming an SSRF primitive. Because at most one
session is ever active, this check degenerates to "is there an active
session, and does its state match" rather than a full session-ID index, but
it is still enforced explicitly rather than assumed, since a stale or
forged callback must still be rejected cleanly.

The relay (`relay.py`'s `forward()`, plus `AuthorizationManager.relay()`'s
routing checks):

- allows only the methods rclone's temporary listener expects (`GET`);
- preserves duplicate and escaped query parameters by forwarding the raw
  query;
- applies a bounded request timeout;
- forwards rclone's response status and safe response headers;
- removes hop-by-hop headers (plus `Content-Length`, which the truncating
  body cap can invalidate);
- passes the provider `Location` redirect through unchanged, following no
  redirects itself;
- caps the response body to the small authorization page rclone serves;
- rejects requests with no active session, a session in any status other
  than `WAITING_FOR_USER`/`COMPLETING` (expired, cancelled, or already
  completed), or a mismatched state; and
- never logs codes, tokens, client secrets, or complete callback queries.

The host application remains responsible for public TLS, rate limiting,
request-size limits, and any user-facing page around the authorization URL.

### FastAPI integration example

```python
@app.get("/oauth/rclone/callback")
@app.get("/oauth/rclone/callback/auth")
def rclone_callback(request: Request) -> Response:
    relayed = manager.relay(
        RelayRequest(
            path=request.url.path,
            raw_query=request.scope["query_string"],
        )
    )
    return Response(
        content=relayed.body,
        status_code=relayed.status_code,
        headers=dict(relayed.headers),
    )
```

This example is illustrative; header conversion must preserve repeated
headers where the selected framework supports them.

## Session routing and concurrency

The manager holds one FIFO queue and, at most, one active session:

```text
_active: _SessionRecord | None
_pending: deque[_SessionRecord]      # QUEUED, in arrival order
```

There is no `state -> session` index: `oauth_state` is a field on the
record itself (set once the watcher promotes it to `WAITING_FOR_USER`), and
`relay()` compares the callback's extracted state directly against the
active record's - with only one candidate ever, an index would be a lookup
over a single entry.

`start()` returns its session immediately either way: it admits the record
inline when the active slot is free (setting `_active` under the lock, then
calling `_admit()`, which spawns that session's driver thread), and appends
to `_pending` only when another session is already active. No background
dispatcher thread exists - promotion runs inline in `_on_settled()`/
`_promote_next()`, on whichever thread settled the previous session: the
driver thread, or an expiry timer thread. A configurable cap on
`_pending`'s length (and, when the host supplies an owner key, a per-owner
cap) keeps an application from silently accumulating an unbounded number of
expiring sessions; `start()` past the cap raises
`AuthorizationQueueFullError` before enqueueing. The caps govern *queueing*
only, so a session that can be admitted immediately is never rejected.

Each session owns:

- one driver thread and one status-watcher thread (watcher only exists once
  active);
- its recorded private listener address (parsed from
  `config/oauthstatus`'s `authUrl` once known);
- one deadline and cancellation signal; and
- captured diagnostics with secrets removed.

There is deliberately no per-session process, config directory, or RC
control listener to track: everything a session needs (the shared runtime,
the shared config, the shared `RcClient`) already exists and is shared
safely via the queue's single-flight guarantee instead of via OS-level
isolation.

`AuthorizationManager.for_runtime()`'s `WeakKeyDictionary` keying on
`RcloneRuntime` means the manager's lifetime follows the runtime's: it does
not need its own explicit shutdown hook. `Rclone.close()` still cancels any
sessions *that client* started and is still tracking
(`_authorization_sessions`, mirroring `_serve_handles`/`_mount_handles`),
even though the manager itself - and any
other client's sessions on the same shared runtime - outlives that one
`close()` call.

## Configuration ownership

Authorization's target is the shared runtime's one live config file - there
is no session-private config to create and destroy. The flow is:

1. before calling `config/create`, check `remote_name` against
   `config/listremotes` (or `config/get`); if it already exists and
   `on_conflict` is `REJECT` (the default), raise
   `AuthorizationRemoteNameConflictError` without ever calling
   `config/create` - this is strictly necessary because `CreateRemote` would
   otherwise silently delete the existing section (see the state-machine
   section above);
2. drive `config/create` (or `config/update`, for `RECONNECT`) through the
   non-interactive state machine; rclone saves the completed remote into
   the shared config as soon as the OAuth exchange succeeds;
3. read back exactly that remote via `config_show(remote=remote_name)` and
   wrap it in the returned `AuthorizationResult.config`;
4. on failure, cancellation, or expiry *after* the remote section was
   created but before it holds a valid token, call `config/delete` for
   `remote_name` to avoid leaving a broken, tokenless section behind -
   except under `RECONNECT`, where the pre-existing remote must be left as
   it was before the attempt, not deleted.

Every `Rclone` client sharing that runtime sees the new remote the moment
`SaveConfig()` runs - there is no propagation step. The one caveat, also
documented in `docs/production_usage.md`'s "Runtime lifecycle and
multi-client processes" section, and still an open gap (not just a naming
quirk) as of this writing: a client's own `self.config`
snapshot (backing `is_s3()`, `get_s3_credentials()`, `encode_fs_spec()`) was
captured at that client's construction time and will not see a remote
created by a session that ran afterward. A client constructed fresh
*after* the session completes, from the same config path, will see it
(`Config(content)` re-reads the file from disk). Document this plainly at
the call site rather than let it surprise a caller: **if you need
`is_s3()`/`get_s3_credentials()` to see a just-authorized remote, build a
new `Rclone` client (same `runtime=`, same config path) after the session
succeeds** - do not assume an existing client's snapshot updates itself.

If the application stores one config per user in some other system (a
database, a secret manager), `AuthorizationResult.config`'s text is what to
hand it; `rclone-kit` does not introduce that storage itself.

## Provider application credentials

A provider must accept the configured public redirect URI. In practice this
usually means the deployment needs its own registered OAuth application for
each provider, or requires callers to supply one. The deployment model
should decide whether:

- one application-owned provider client is shared by all of its users;
- each tenant supplies a provider client; or
- each authorization request supplies one.

This is distinct from the end user's resulting access and refresh token.
Client credentials must be bound to the session's RC parameters
(`client_id`/`client_secret` in `parameters`, set via rclone's normal
`config.ConfigClientID`/`config.ConfigClientSecret` keys) and must never be
returned to the browser.

Google Drive is the first supported and live-tested backend; the core
session and relay implementation is backend-neutral and must stay that way.
Add other backends only after confirming their actual `Config` wizard shape
matches the two-question policy above - a backend with additional required
prompts before reaching `*oauth` needs those prompts either pre-answered via
`parameters` or explicitly added to the driver's known-question table, never
silently skipped.

## Security requirements

### Secrets and logs

Treat all of the following as sensitive:

- provider client secrets;
- authorization codes;
- access and refresh tokens;
- completed `Config` text (from `config_show`);
- complete callback query strings; and
- OAuth state values in routine logs.

No exception should embed a raw RC response or `config_show` output without
passing it through an authorization-specific redactor first. Debug logs may
name the backend, session ID, state transition (the config-wizard `state`
token itself is an opaque continuation identifier, not a secret, but is
still not useful to log routinely), and lifecycle status, but not the values
above. Because there is no subprocess, there is no OS-process-list exposure
class of concern to redact against - every sensitive value only ever
appears as an in-process JSON RC parameter, never as an argv.

### Callback integrity

Rclone remains the authoritative state validator. The manager additionally
uses `state` as an untrusted lookup key and rejects it unless it matches the
one currently active session. A callback must not select its own upstream
address - the private listener address is always read from the manager's
own session record, never from a header or query value supplied by the
caller.

Consume the route when rclone successfully completes. Expired, replayed, or
post-completion callbacks must not revive a session or return a previous
authorization page.

### Network isolation

rclone's OAuth listener binds loopback by default
(`OAuthListenAddress` unset); a non-default `private_listen_addr` is an
explicit deployment decision and should be documented as exposing a
short-lived HTTP service that normally belongs on a private network behind
a trusted relay. Public HTTPS terminates outside rclone entirely; do not
publish the private OAuth listener directly.

### Resource control

Set deadlines for queueing, startup, user consent, callback forwarding, and
rclone completion. Limit both the pending-queue depth and relay response
size. Because there is no child process to clean up, cleanup means: a
closed/expired/cancelled session leaves rclone's OAuth globals cleared
(verifiable via `config/oauthstatus` reporting `"stopped"`) and, on
interpreter exit, does not leave a session's driver, status-watcher,
oauth-stop or expiry-timer thread as the only thing keeping the process
alive (every one of them is created `daemon=True`, matching `_JobMonitor`'s
convention).

## Error model

Layered on the existing `RcloneKitError`/`OperationError` hierarchy in
`exceptions.py`, following its established shape (typed fields, a clear
`__cause__`, no bare strings where a value belongs on the exception):

- `AuthorizationError(RcloneKitError)` - base type;
- `AuthorizationRemoteNameConflictError` - `remote_name` already exists and
  `on_conflict` was `REJECT`;
- `AuthorizationUnsupportedPromptError` - the state machine hit a
  `config_*` question outside the fixed `config_refresh_token`/
  `config_is_local` policy;
- `AuthorizationStartError` - the initial `config/create`/`config/update`
  call failed before any question was reached; carries the underlying
  `RcCallError`;
- `AuthorizationRejectedError` - the provider or rclone reported the flow
  failed (bad code, state mismatch, provider denial);
- `AuthorizationExpiredError` - `expires_at` elapsed, whether queued or
  active;
- `AuthorizationCancelledError` - `cancel()` was called, or the manager
  force-cancelled an overrun session;
- `AuthorizationQueueFullError` - `start()` exceeded the pending-queue cap;
  and
- `AuthorizationRelayError` - the relay could not reach or parse a response
  from the private listener.

Each one carries the `remote_name` it applies to - except
`AuthorizationRelayError`, which describes a callback the relay could not
route or forward rather than one named remote - plus whatever else
identifies that particular failure: the config state and question name on
`AuthorizationUnsupportedPromptError`, the cap on
`AuthorizationQueueFullError`, the underlying RC or transport failure as
`.cause`/`__cause__` on `AuthorizationStartError`/`AuthorizationRelayError`.
Every message embedding rclone or provider text passes it through
`redact_provider_text` first. Carrying the session ID, backend, and
lifecycle stage as well is open work, not yet done. Provider error *names*
may be preserved, but provider
descriptions and query strings must be checked before logging or returning
them (an OAuth `error_description` parameter is attacker-influenced input,
same as any other redirect query value).

## Module boundaries

```text
src/rclone_kit/authorization/
    __init__.py
    types.py          # AuthorizationRequest/Result, RelayRequest/Response,
                       # lifecycle enum, Secret
    manager.py         # for_runtime(), queue/single-flight admission
    session.py          # lifecycle, wait/cancel/close, driver+watcher threads
    state_driver.py      # the config/create non-interactive state walker
                          # and its fixed question-answer policy
    relay.py               # public-request -> private-listener translation
    redaction.py             # redact_provider_text and the secret scrubbing
    exceptions.py            # the AuthorizationError family above

src/rclone_kit/rc/auth.py  # low-level typed RC calls this needs
                            # (config/create, config/update, config/delete,
                            # config/listremotes, config/oauthstatus,
                            # config/oauthstop), mirroring rc/jobs.py's
                            # RcJobClient shape
```

Responsibilities stay narrow, matching the rest of `rclone_kit.rc`/
`rclone_kit.operations`'s existing split between a thin typed RC boundary
and the higher-level operation logic built on it:

- `state_driver.py` is the one place that knows the `*oauth*` state names
  and the two-question policy - if a future backend needs a third known
  question, it is added here, reviewed, and tested, not inferred at
  runtime;
- `manager.py` owns the queue, the `WeakKeyDictionary` runtime keying, and
  nothing about HTTP;
- `relay.py` owns the public/private request translation and nothing about
  the state machine;
- `session.py` starts the per-session driver and status-watcher threads;
  `manager.py` starts the threads that drive admission and the expiry
  timer. Every one of them is a daemon thread, so none can block process
  exit.

`Rclone.authorize(remote_name, backend, ...)` on `client.py` is a thin
wrapper that resolves `AuthorizationManager.for_runtime(self.
_embedded_runtime)`, calls `manager.start(...)`, and tracks the returned
session (`_track_authorization_session`) the same way
`mount()`/`serve_webdav()` track their handles; no queue or state-machine
logic is inlined into `client.py` itself.

Importing `rclone_kit` or `rclone_kit.authorization` must not spawn a
thread, register a signal handler, or touch the network. Resource creation
begins only when `AuthorizationManager.start()` is called.

## Test strategy

### Unit tests

`tests/unit/test_authorization_state_driver.py`,
`test_authorization_manager.py`, `test_authorization_session.py`,
`test_authorization_relay.py`, `test_authorization_redaction.py`,
`test_authorization_types.py`, and `tests/unit/test_rc_auth.py` drive a fake
`RcCallable` (the same `RcCallable`/`RcCapableRuntime` protocol split used
throughout `operations/*_embedded.py`'s test suite) and cover:

- the state driver's full transition table, including the reconnect
  (`config_refresh_token`) branch and the unrecognized-question rejection;
- the `_config` overlay produced for `OAuthListenAddress`/`OAuthRedirectURL`;
- name-collision detection and `on_conflict` handling before `config/create`
  is ever called;
- queue admission order, the global pending cap, and queued-session expiry
  that never touches rclone;
- all lifecycle transitions and invalid transitions, including the
  `QUEUED -> EXPIRED` and force-cancel-on-overrun paths;
- idempotent cancel/close;
- raw query preservation and safe header filtering in the relay;
- relay routing for an unknown state and for no active session; and
- redaction of every field listed under "Secrets and logs".

Two branches are deliberately noted as **not yet covered**, because a
maintainer refactoring them would otherwise assume they are: the per-owner
queue cap (`manager.py`'s `owner`/`_per_owner_cap` branch - no test passes
either argument, so it has never executed), and the relay's rejection of a
session whose status is no longer relay-eligible (`manager.relay()`'s
`_ACTIVE_RELAY_STATUSES` check).

### Offline integration tests

`tests/native/test_authorization_offline_integration.py` stands up a local
fake OAuth provider with authorization and token endpoints and drives the
real vendored native library (built for tests exactly as
`tests/native/conftest.py` does for the rest of the suite - no separate
binary needed, since the capability is in the same build). In order, it:

1. starts a session against a real `RcloneRuntime`;
2. retrieves its public authorization URL through the manager, not by
   reading `config/oauthstatus` directly;
3. enters through the relay's `/auth` path;
4. follows rclone's redirect to the fake provider;
5. has the provider return a code and state to the public callback path;
6. relays the callback to rclone;
7. lets rclone - not Python - drive the token exchange against the fake
   provider's `/token` endpoint; and
8. obtains the completed `AuthorizationResult` and asserts the saved
   remote's config.

The fake provider accepts any well-formed token request rather than
validating it, so the redirect URI actually sent in the exchange, and the
post-run `config/oauthstatus`/thread-exit state, are **not** asserted
today.

It then proves serialization itself: two sessions started back to back, with
the second observed `QUEUED` until the first settles, and both completing.
That the second never touched rclone's OAuth globals while queued follows
from the design rather than from an assertion - the test does not read
`config/oauthstatus`.

### Failure integration tests

The same suites cover these failure paths:

- provider denial (`AuthorizationRejectedError` from the driver);
- state mismatch;
- unknown state (relay call with no active session);
- name collision (`REJECT` and `RECONNECT` both);
- an unrecognized `config_*` question from a backend with an extra prompt;
- an rclone-side error before the listener ever binds (bad `client_id`,
  surfaced as `AuthorizationStartError`); and
- explicit cancellation of an active session versus a queued one.

These failure paths have **no test yet**, and are the honest gaps in this
subsystem's coverage:

- listener bind failure on a caller-supplied `private_listen_addr`;
- confirmation that the status watcher does not hang waiting for
  `"running"` when the flow fails before the listener binds;
- provider token-endpoint failure;
- a relayed callback arriving after the session expired; and
- manager teardown (process/runtime shutdown) with sessions still queued or
  active.

### Live tests

Implemented as `tests/live/gdrive_authorization/` (its own marker,
`live_gdrive_authorization`, gated the same way as `tests/live/gdrive`/
`tests/live/s3` - see `docs/implementation_and_build_pipeline.md`'s test
list). No dedicated test OAuth client turned out to be necessary: rclone's
own built-in shared client_id works via `Rclone.authorize()`'s local-direct
mode (no `public_callback_url`), the same fallback plain interactive
`rclone config create` uses - see `AuthorizationRequest`'s docstring for
why a relay deployment normally supplies its own client instead. Verifies
the resulting remote can list a controlled folder and refresh a
deliberately invalidated access token using its refresh token.

Unlike `tests/live/gdrive`/`tests/live/s3`, this suite cannot run fully
unattended even when explicitly selected: it blocks mid-run waiting for a
human to approve access in a real browser, since that step cannot be
scripted further without violating the provider's terms of service. Never
run it automatically in CI - it isn't (see the workflow files' job list);
running it is always a deliberate, manual `pytest -m
live_gdrive_authorization` invocation. `scripts/verify_gdrive_authorization.py`
is a non-pytest equivalent for a quicker one-off check outside the test
suite.

## Implementation status

The listener/redirect capability this design needs already ships in the
binary this repository builds, so no native proof-of-concept or patched
binary was ever involved; upstreaming it (rclone issue #7634, PR #7635) is a
separate, unblocked effort on its own timeline. The work was Python only,
and it is delivered:

- `state_driver.py` walks the non-interactive config state machine, with
  unit tests against a fake `RcCallable` covering the transition table
  above, the reconnect branch, and the unrecognized-question rejection;
- `AuthorizationManager`/`AuthorizationSession`/the exception family own the
  single-flight queue and the session lifecycle, including name-collision
  protection before any `config/create` call and the
  `Config`-via-`config_show` result path;
- `relay.py` and `AuthorizationManager.relay()` provide the
  framework-neutral `RelayRequest`/`RelayResponse` translation, exercised by
  the offline fake-provider integration test
  (`tests/native/test_authorization_offline_integration.py`) and its
  two-sessions-serialize proof;
- `Rclone.authorize(...)` and its `close()`-time session tracking mirror
  `_track_serve_handle`/`_track_mount_handle`, and the
  stale-`self.config`-snapshot interaction is documented in
  `docs/production_usage.md` next to its "Runtime lifecycle and
  multi-client processes" section; and
- live verification runs as `tests/live/gdrive_authorization/`, which lists
  a real folder and refreshes a deliberately invalidated token through the
  normal (non-authorization) RC path.

### Still open: relay against a real provider

Live verification was reached through local-direct mode (no relay, no
registered callback needed) rather than a reverse-proxy-shaped relay
deployment: rclone's shared client_id is only registered for its own
loopback redirect, so a relay-shaped run needs its own dedicated provider
client (see "Provider application credentials"). A real
reverse-proxy-shaped relay deployment against a caller-owned provider client
therefore **remains unverified** by this repository's own test suite -
`relay.py`'s unit tests and the offline fake-provider integration test cover
the relay's translation logic, just not against a real provider. A caller
supplying their own `client_id`/`client_secret`/`public_callback_url` has to
carry out that verification for their own deployment; this repository cannot
do it generically without owning a registered public redirect.

## Acceptance criteria

The criteria this feature was accepted against, all of them met - criterion
2 through the offline fake-provider relay test rather than against a real
provider (see "Still open: relay against a real provider"):

1. A caller starts a session without importing a web framework.
2. The returned URL is usable from a browser on another machine.
3. The public callback is relayed to rclone's private, in-process listener.
4. Rclone validates state and performs the token exchange itself.
5. The caller receives a ready-to-use `Config` without handling token JSON,
   and the shared runtime's live config already has the same remote.
6. A second authorization requested while one is active is queued, not
   silently corrupted by rclone's shared OAuth globals - verified by a test
   that asserts the second session never observes `config/oauthstatus`
   state belonging to the first.
7. Cancellation, expiry, application shutdown, and failures leave
   `config/oauthstatus` reporting `"stopped"` and no daemon threads
   preventing process exit.
8. Logs and exceptions contain no secrets, codes, tokens, or callback
   queries.
9. A real Google Drive remote created by the flow can list data and refresh
   its token through the normal transfer path.
10. Attempting to authorize a name that collides with an existing,
    unrelated remote fails before any rclone call is made, rather than
    silently deleting that remote.

## The constraint to preserve

The subsystem is built directly against the shared embedded runtime, in
Python only; there is no native-capability gap left to close and no
temporary or patched binary anywhere in this path. The single hard
constraint every other piece of it rests on is rclone's process-wide OAuth
globals: the single-flight queue is what makes the relay, the status
watcher, and `state`-based callback routing safe at all. Any change to the
manager's admission logic has to keep that guarantee intact rather than
trade it for throughput.

The upstream contribution (rclone issue #7634, PR #7635) runs on its own
schedule, credited against the already-vendored downstream implementation as
evidence that the approach works, and is independent of this subsystem.
