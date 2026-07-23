# Rclone-owned remote authorization design

## Status

Proposal, not implemented. This document defines how `rclone-kit` should
offer remote authorization while keeping OAuth implementation inside rclone.

The required upstream or downstream rclone change is described separately in
the [upstream change brief](../reference/rclone_remote_oauth_upstream_change.md).

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

## Design decisions

The following decisions define the proposal:

1. Run one isolated rclone process per pending authorization.
2. Give every session its own private config file and OAuth listener port.
3. Use a stable public callback URL registered with the provider.
4. Route requests to the correct session using rclone's generated state.
5. Forward browser requests to rclone without interpreting the authorization
   code or exchanging it in Python.
6. Return an `rclone-kit.Config` assembled from rclone's completed result.
7. Keep web-framework integration outside the core library API.
8. Accept an explicit rclone executable so a temporary patched binary can be
   used before the upstream change is released.
9. Do not persist user tokens in an implicit global store. The calling
   application decides how completed configuration is stored.

One process per session costs more than multiplexing every user through one
`rclone rcd`, but it matches rclone's current single-active-OAuth model,
provides failure isolation, and makes cleanup and ownership unambiguous.

## Architecture

```text
Application
    |
    | start authorization
    v
AuthorizationManager
    |
    | creates AuthorizationSession
    | starts isolated rclone process
    v
Private rclone OAuth listener <------ CallbackRelay
    |                                      ^
    | provider authorization URL           |
    v                                      | public callback
End-user browser ---> OAuth provider ------+
    |
    | rclone exchanges code and emits/stores token
    v
AuthorizationResult(Config)
```

The public callback relay may run in FastAPI, Django, Flask, an API gateway,
or another host. The core library supplies framework-neutral request and
response values and session routing; it does not start a public HTTP server as
an import side effect.

## Required rclone capability

The manager requires a rclone binary that can independently configure:

- the private OAuth listener address; and
- the public OAuth redirect URI.

The ideal binary also reports the externally usable authorization URL through
structured status. Until that is available, a dedicated adapter may parse the
stable authorization line printed by `rclone authorize`.

The manager should probe the executable for the exact required flags before
starting a session and raise a specific `UnsupportedAuthorizationBinaryError`
when they are unavailable. Capability probing is safer than assuming support
from a version string, especially while downstream builds exist.

The proposal uses these placeholder flag names:

```text
--oauth-listen-addr
--oauth-redirect-url
```

Implementation must use the names actually accepted upstream or by the
temporary patch.

## Public API proposal

The API should be synchronous and thread-safe, consistent with the existing
library. A web framework can call blocking methods in its worker pool. An
asynchronous convenience layer can be added later without changing the core
session model.

### Request

```python
@dataclass(frozen=True)
class AuthorizationRequest:
    remote_name: str
    backend: str
    public_callback_url: str
    backend_options: Mapping[str, str]
    client_id: str | None = None
    client_secret: Secret | None = None
    expires_in: timedelta = timedelta(minutes=10)
```

`backend_options` contains rclone backend configuration, such as a Drive
scope, but must not contain an already-issued token. Provider application
credentials may be supplied by the deployment or incorporated into a
provider-specific configuration profile.

`Secret` in this sketch means a small value type whose `repr` and `str` do not
expose its content. It does not require a third-party secret-model package.

### Manager and session

```python
manager = AuthorizationManager(
    rclone_exe=patched_rclone,
    private_listen_host="127.0.0.1",
)

session = manager.start(
    AuthorizationRequest(
        remote_name="user_drive",
        backend="drive",
        public_callback_url=(
            "https://service.example.com/oauth/rclone/callback"
        ),
        backend_options={"scope": "drive"},
        client_id=google_client_id,
        client_secret=Secret(google_client_secret),
    )
)

send_to_user(session.authorization_url)
result = session.wait(timeout=600)
rclone = Rclone(result.config)
```

`AuthorizationSession` should expose:

- `id`: an opaque random identifier for diagnostics and application records;
- `authorization_url`: the public URL to show the user;
- `expires_at`: an aware UTC timestamp;
- `status`: a lifecycle enum;
- `wait(timeout=None) -> AuthorizationResult`;
- `cancel() -> None`; and
- `close() -> None`, with context-manager support.

Suggested lifecycle values are:

```text
STARTING
WAITING_FOR_USER
COMPLETING
SUCCEEDED
FAILED
CANCELLED
EXPIRED
CLOSED
```

Terminal state transitions must be atomic. Repeated cancellation and cleanup
must be safe.

### Result

```python
@dataclass(frozen=True)
class AuthorizationResult:
    remote_name: str
    config: Config
```

The normal result should not separately expose access and refresh tokens.
Returning a `Config` keeps the credential inside the rclone abstraction and
reduces accidental logging. Applications that persist it must treat the
entire configuration as a secret.

The result may also provide a deliberate method for merging the new remote
into another `Config`. Merge behavior must reject duplicate remote names by
default rather than silently overwriting credentials.

## Browser relay API

The relay must not depend on FastAPI request or response types. A narrow
contract is sufficient:

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

The public application should mount two shapes below one registered callback
base:

```text
/oauth/rclone/callback/auth?state=...
/oauth/rclone/callback?state=...&code=...
```

The first is the entry URL shown to the user. Rclone redirects it to the
provider. The second is the provider callback. Provider error query parameters
use the same second path.

The manager extracts `state` only to find the target session. It forwards the
raw query unchanged so rclone performs the authoritative state check. The
public path prefix is removed before forwarding:

```text
public .../callback/auth -> private /auth
public .../callback      -> private /
```

The target listener address must come only from the manager's internal session
record, never from a URL or header supplied by the caller. This prevents the
relay from becoming an SSRF primitive.

The relay should:

- allow only the methods rclone's temporary listener expects;
- preserve duplicate and escaped query parameters by using the raw query;
- set a short connection and response timeout;
- forward the rclone response status and safe response headers;
- remove hop-by-hop headers;
- pass the provider `Location` redirect through unchanged;
- cap response size to the small authorization page expected from rclone;
- reject unknown, expired, completed, or mismatched sessions; and
- never log codes, tokens, client secrets, or complete callback queries.

The host application remains responsible for public TLS, rate limiting,
request-size limits, and any user-facing page around the authorization URL.

### FastAPI integration example

An adapter can be only a few lines and belongs in application code or an
optional integration module:

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

This example is illustrative. Header conversion must preserve repeated headers
where the selected framework supports them.

## Execution proposals

### Proposal A: one `rclone authorize` process per session

This is the smallest implementation and the recommended first functional
spike.

For each request:

1. Allocate a private listener port.
2. Start `rclone authorize <backend>` with no automatic browser, the private
   listener, the public redirect URI, and the provider client configuration.
3. Read merged output incrementally until the public authorization URL is
   available.
4. Parse its rclone-generated state and register it to the session.
5. Return the session to the caller.
6. Relay `/auth` and the callback to that process's private listener.
7. Wait for rclone's delimited result:

   ```text
   Paste the following into your remote machine --->
   ...
   <---End paste
   ```

8. Validate and place the exact rclone-produced token into the requested
   remote section.
9. Return `AuthorizationResult` and dispose the process.

Advantages:

- closely matches rclone's existing headless authorization command;
- one subprocess and no separate RC control server;
- rclone directly returns the token intended for a remote; and
- easy isolation and cancellation.

Limitations:

- authorization URL and result discovery depend on command output unless
  upstream adds structured output;
- custom client credentials are positional arguments in the existing command
  and can be visible in the host's process list;
- backend option blobs are also positional and must be redacted from all
  diagnostics;
- a checked-then-released port allocation has a bind race; and
- the wrapper must assemble the final remote section.

The existing `format_command` redacts sensitive flag values but cannot know
that positional `rclone authorize` arguments contain a client secret or an
encoded configuration blob. The authorization runner must supply its own
command formatter that redacts those positions. This fixes logs but not
operating-system process-list exposure.

Proposal A is acceptable for a controlled prototype. Proposal B is preferable
for production deployments that need stronger credential handling and a
structured control plane.

### Proposal B: one isolated `rclone rcd` process per session

This is the recommended production target after the end-to-end behavior is
proven.

For each request:

1. Write the incomplete remote and its provider client credentials to an
   owner-only, process-private rclone config file.
2. Start an isolated `rclone rcd` bound to loopback with randomly generated RC
   credentials, a private OAuth listener, and the public redirect URI.
3. Drive `config/create` or `config/update` with `nonInteractive`, `state`,
   `continue`, and `result` through the loopback RC API.
4. When the OAuth step blocks waiting for the browser, query
   `config/oauthstatus` to obtain the authorization URL.
5. Register state and return the session.
6. Relay the browser requests to the separate private OAuth listener.
7. Continue the non-interactive configuration state machine until rclone
   completes the remote.
8. Read the finished process-private config and return it as `Config`.
9. Call `config/oauthstop` on cancellation when appropriate, then terminate
   and clean up the process.

The RC control listener and OAuth callback listener are different private
ports and must be modeled separately.

Advantages:

- structured status and cancellation;
- rclone builds the whole remote rather than returning only a token;
- provider credentials can remain in an owner-only config instead of command
  arguments;
- configuration questions use rclone's backend state machine; and
- the result is the exact config rclone created.

Limitations:

- more moving parts and two private listeners;
- the non-interactive config state machine needs a dedicated tested driver;
- an OAuth-related RC request may remain blocked while the browser flow is
  pending and must run in a worker;
- current rclone status is process-global, which is why every session still
  needs its own rcd process; and
- RC authentication and lifecycle add implementation work.

### Proposal C: implement provider OAuth in Python

Do not pursue this as the product design. It would require `rclone-kit` to own
provider URLs, scopes, PKCE details, token exchanges, response normalization,
refresh compatibility, and provider-specific maintenance. It would also
repeat behavior already maintained by rclone.

This approach is useful only as an emergency provider-specific experiment and
must not be presented as the general rclone authorization wrapper.

## Session routing and concurrency

The manager should maintain two private indexes under one lock:

```text
session_id -> AuthorizationSessionState
oauth_state -> session_id
```

`oauth_state` is learned from the rclone-generated authorization URL. It must
be removed when the session reaches any terminal state. Session IDs and states
must never be reused.

Each session owns:

- one rclone process;
- one temporary config directory;
- one private OAuth listener address;
- optionally one private RC listener address;
- one worker responsible for reading or driving rclone;
- one deadline and cancellation signal; and
- captured diagnostics with secrets removed.

The manager should enforce configurable limits on pending sessions globally
and, when the host supplies an owner key, per owner. Capacity exhaustion must
fail before starting another process.

Port allocation should retry when rclone reports a bind failure. Once rclone
can use `:0` and expose the actual selected address, switch to operating-system
allocation and remove the checked-free-port race.

## Configuration ownership

Every authorization must begin with an isolated config. It must not modify the
process-wide config discovered from `RCLONE_CONFIG` unless the caller
explicitly chooses to merge the successful result.

The recommended result flow is:

1. create an incomplete remote in a session-private config;
2. let rclone add the token;
3. read and validate the completed remote;
4. return a new `Config` value;
5. destroy the session-private files after the caller has received the value;
6. let the caller persist, encrypt, or merge the result.

If the application stores one config per user, that file or secret record is
its responsibility. `rclone-kit` should document safe patterns but should not
silently introduce a database or global credential directory.

## Provider application credentials

A provider must accept the configured public redirect URI. In practice this
usually means the deployment needs its own registered OAuth application for
each provider or requires callers to supply one.

The deployment model should decide whether:

- one application-owned provider client is shared by all of its users;
- each tenant supplies a provider client; or
- each authorization request supplies one.

This is distinct from the end user's resulting access and refresh token.
Client credentials must be bound to the session configuration and must never
be returned to the browser.

The first supported and live-tested backend should be Google Drive, but the
core session and relay implementation must remain backend-neutral. Add other
backends only after testing their actual rclone authorization behavior.

## Temporary patched rclone binary

The authorization API must accept an explicit `rclone_exe`, using the same
resolution rules as the rest of the library. This provides three deployment
modes:

1. an explicitly supplied development build;
2. a temporary patched build bundled in `rclone-kit`; and
3. an official rclone build after the upstream release contains the feature.

Before a custom binary becomes the bundled default:

- build it from a maintained fork and recorded commit;
- include an identifiable downstream version suffix;
- publish or otherwise retain the corresponding source and patch;
- record archive and executable hashes for every platform;
- update `runtime/platform.py` and the distribution-verification fixtures;
- keep the rclone license adjacent to the binary;
- run all existing wheel integrity checks; and
- run authorization-specific end-to-end tests using the packaged executable,
  not only a binary from the source checkout.

The custom-binary feature probe should run once per resolved executable and
cache only a non-secret capability result. Do not silently fall back to direct
Python OAuth when the binary lacks support.

The exit condition is an official rclone release with the required behavior.
After verifying that release, update the pinned artifact and remove the fork
patch and downstream version handling.

## Security requirements

### Secrets and logs

Treat all of the following as sensitive:

- provider client secrets;
- authorization codes;
- access and refresh tokens;
- completed `Config` text;
- encoded rclone authorization blobs;
- complete callback query strings; and
- OAuth state values in routine logs.

No exception should embed raw rclone output without passing it through an
authorization-specific redactor. Debug logs may name the backend, session ID,
state transition, process ID, and redacted listener address, but not the
values above.

### Callback integrity

Rclone remains the authoritative state validator. The manager additionally
uses state as an untrusted lookup key and rejects it unless it maps to a live
session. A callback must not select its own upstream address.

Consume the route when rclone successfully completes. Expired or replayed
callbacks must not revive a process or return a previous authorization page.

### Network isolation

Bind RC control to loopback and protect it with random per-session
credentials. Bind the OAuth listener to loopback when the relay shares the
network namespace. If a sidecar requires a non-loopback bind, expose it only
on the private container network and restrict ingress to the relay.

Public HTTPS terminates outside rclone. Do not publish the private OAuth or RC
port directly.

### Resource control

Set deadlines for startup, user consent, callback forwarding, rclone
completion, and process shutdown. Limit concurrent sessions and relay response
size. Ensure interpreter-exit cleanup terminates every child process and
removes temporary configs.

## Error model

Add focused exceptions rather than returning raw subprocess failures:

- `UnsupportedAuthorizationBinaryError`;
- `AuthorizationStartupError`;
- `AuthorizationOutputError`;
- `AuthorizationRelayError`;
- `AuthorizationRejectedError`;
- `AuthorizationExpiredError`;
- `AuthorizationCancelledError`;
- `AuthorizationProcessError`; and
- `AuthorizationConfigError`.

Exceptions should include the session ID, backend, lifecycle stage, rclone
return code when applicable, and sanitized diagnostics. Provider error names
may be preserved, but provider descriptions and query strings must be checked
before logging or returning them.

## Proposed module boundaries

An initial package layout could be:

```text
src/rclone_kit/authorization/
    __init__.py
    types.py
    manager.py
    session.py
    relay.py
    process_runner.py
    output_parser.py
    exceptions.py
```

Responsibilities should remain narrow:

- `types.py`: immutable requests, results, relay values, statuses, and secret
  wrappers;
- `manager.py`: indexes, capacity, session creation, and routing;
- `session.py`: lifecycle, wait, cancel, result, and cleanup;
- `relay.py`: safe translation of public requests to the stored private
  listener;
- `process_runner.py`: rclone command or RC lifecycle and private config;
- `output_parser.py`: the isolated compatibility boundary for CLI output; and
- `exceptions.py`: sanitized public failure types.

Do not add authorization methods directly to the large `Rclone` client until
the session design is stable. Export a small curated surface from
`rclone_kit.authorization`; later, `Rclone.authorize(...)` can be a thin
convenience wrapper if there is clear value.

Importing `rclone_kit` or `rclone_kit.authorization` must not spawn a process,
bind a port, register signal handlers, or start a thread. Resource creation
begins only when `AuthorizationManager.start` is called.

## Test strategy

### Unit tests

Use fake process and transport boundaries to cover:

- capability detection;
- authorization URL parsing and validation;
- delimited result parsing across arbitrary stream chunk boundaries;
- redaction of every positional and flag-based secret;
- all lifecycle transitions and invalid transitions;
- startup, consent, completion, and shutdown timeouts;
- idempotent cancel and close;
- config creation and duplicate-name merge rejection;
- raw query preservation;
- safe header filtering;
- unknown, expired, replayed, and completed state routing;
- port bind retry; and
- global and per-owner capacity limits.

### Offline integration tests

Build a local fake OAuth provider with authorization and token endpoints. The
test must exercise a real patched rclone process:

1. start a session;
2. retrieve its public authorization URL;
3. enter through the relay `/auth` path;
4. follow rclone's redirect to the fake provider;
5. have the provider return a code and state to the public callback;
6. relay the callback to rclone;
7. assert the token exchange contains the configured redirect URI;
8. assert rclone, not Python, calls the token endpoint;
9. obtain the completed `Config`; and
10. verify all resources are gone.

Run the same test with multiple simultaneous sessions and intentionally return
callbacks in a different order. Cross-routing must fail.

### Failure integration tests

Cover:

- provider denial;
- state mismatch;
- unknown state;
- malformed authorization URL;
- listener port already occupied;
- rclone exit before readiness;
- rclone exit after callback but before result;
- token endpoint failure;
- callback after expiry;
- manager shutdown with active sessions; and
- relay inability to reach the private listener.

### Live tests

Add an explicitly selected live marker for Google Drive using a dedicated test
OAuth client and registered callback. Verify that the resulting remote can
list a controlled folder and refresh an expired or deliberately invalidated
access token using its refresh token.

Never run live authorization automatically for untrusted pull requests. Keep
provider credentials and returned config in CI secret storage and redact test
artifacts.

### Distribution tests

For a bundled custom binary, verify both supported wheels contain the expected
patched executable and that runtime resolution selects it. The end-to-end
authorization smoke test should execute the resolved packaged binary.

## Delivery plan

### Phase 1: downstream rclone proof

1. Implement the clean listener/redirect patch in the reference checkout or a
   maintained fork.
2. Add upstream-quality Go tests.
3. Build a local binary with an identifiable version.
4. Prove a manual browser flow through a minimal relay.

### Phase 2: `rclone authorize` prototype

1. Add request, result, status, exception, and secret value types.
2. Add the capability probe and authorization-specific command redaction.
3. Add a per-session process runner and incremental output parser.
4. Add the state-indexed relay.
5. Return a process-private completed `Config`.
6. Add offline and concurrent integration tests.

This phase proves the product boundary quickly and provides useful feedback
for the upstream contribution.

### Phase 3: isolated RC production path

1. Add a private authenticated rcd runner.
2. Implement the non-interactive config state-machine driver.
3. Use `config/oauthstatus` and `config/oauthstop` for structured lifecycle.
4. Keep the CLI runner as a compatibility path until RC coverage is complete.
5. Compare resulting configs across both paths.

### Phase 4: packaged temporary binary

1. Produce reproducible Windows and Linux artifacts from the downstream fork.
2. Update pinned artifact metadata, hashes, licenses, and distribution tests.
3. Document the downstream build prominently in release notes.
4. Release only after live authorization and normal storage regression tests
   pass on both platforms.

### Phase 5: upstream and migration

1. Coordinate on rclone PR #7635 before opening a replacement.
2. Submit the clean rclone change with the demonstrated relay use case and
   test evidence.
3. Track the accepted commit and first official release containing it.
4. Replace the patched artifacts with that official release.
5. Remove downstream-only compatibility code after the minimum supported
   rclone version contains the feature.

## Acceptance criteria

The `rclone-kit` feature is complete when:

1. A caller starts a session without importing a web framework.
2. The returned URL is usable from a browser on another machine.
3. The public callback is relayed to a private rclone listener.
4. Rclone validates state and performs the token exchange.
5. The caller receives a ready-to-use `Config` without handling token JSON.
6. Concurrent users cannot receive or overwrite one another's credentials.
7. Cancellation, expiry, application shutdown, and failures leave no rclone
   processes, open listeners, or temporary config files.
8. Logs and exceptions contain no secrets, codes, tokens, blobs, or callback
   queries.
9. A real Google Drive remote created by the flow can list data and refresh
   its token.
10. The feature works with an explicit patched executable and later with the
    official upstream binary without changing the public session API.

## Recommendation

Proceed with one process per authorization and implement Proposal A as the
first end-to-end proof. Keep its parser and command-specific secret handling
isolated so they can be deleted. Use the result of that proof to validate and
contribute the narrow rclone listener/redirect change.

For the production path, move to Proposal B: an isolated authenticated rcd per
session, rclone's non-interactive configuration state machine, and structured
OAuth status. Keep the relay framework-neutral and state-routed in both paths.

Use a custom rclone binary to unblock delivery only when it is built from the
same clean patch intended for upstream, fully identified and verified by the
existing artifact pipeline, and accompanied by an explicit migration plan to
the first suitable official rclone release.
