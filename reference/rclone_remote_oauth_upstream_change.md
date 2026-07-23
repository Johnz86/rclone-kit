# Remote OAuth support needed from rclone

## Status and scope

This document records the business reason for changing rclone, the smallest
upstream contract that would satisfy that reason, and the state of the existing
upstream proposal. It is a design and contribution brief, not a statement that
the change has already been implemented.

The upstream state described here was reviewed on 2026-07-22 against:

- rclone issue [#7634](https://github.com/rclone/rclone/issues/7634);
- rclone pull request [#7635](https://github.com/rclone/rclone/pull/7635);
- related pull request
  [#9466](https://github.com/rclone/rclone/pull/9466), which added the current
  `config/oauthstatus` result;
- the local rclone checkout in `reference/rclone`, currently at
  `v1.74.0-289-g2f3895fa3` (`2f3895fa3`).

## Business objectives

### User outcome

An application using `rclone-kit` must be able to ask rclone to authorize a
user-owned cloud-storage account and give the application a URL that it can
show to that user. The user may open the URL on a different computer and must
not need:

- shell access to the application host;
- rclone installed on their computer;
- an SSH tunnel;
- access to a port on the application's container; or
- knowledge of rclone configuration files or token formats.

After the user consents, rclone must complete the provider's OAuth flow and
produce credentials it can use for normal storage operations. The application
should not reimplement provider authorization, token exchange, token response
normalization, or token refresh.

### Deployment outcome

The flow must work when `rclone-kit` and rclone run:

- in a container behind FastAPI, Django, Flask, or another web application;
- behind a reverse proxy or ingress controller that terminates TLS;
- on a host with no browser;
- on a private network that cannot expose rclone's temporary listener
  directly; and
- in a multi-user service where more than one authorization can be pending.

The public web application owns a stable HTTPS callback URL. Rclone may listen
on a separate private address and port. Those two addresses are intentionally
different and must not be inferred from one another.

### Product boundary

`rclone-kit` is a library wrapper around rclone, not a hosted OAuth product.
The desired ownership boundary is:

- rclone selects provider behavior and scopes, generates and validates OAuth
  `state`, builds the provider authorization request, exchanges the code,
  checks provider-specific results, and produces the token;
- `rclone-kit` manages a temporary rclone process, exposes its authorization
  URL, relays browser requests, captures the completed result, and cleans up;
- the host application connects the framework-neutral relay to its web
  framework and decides how user identity, authorization records, and secrets
  are persisted;
- the reverse proxy or ingress owns public TLS.

The callback relay must not become a replacement authorization server. It
forwards requests to the rclone flow that created them. In particular, it does
not proxy or rewrite provider token endpoints.

### Success criteria

The upstream capability is sufficient when all of the following can be
demonstrated:

1. Rclone listens on a caller-selected private address.
2. The provider receives a separately selected public redirect URI.
3. The URL given to the user is publicly reachable and leads into the same
   rclone authorization flow.
4. The redirect URI used during code exchange exactly matches the one used in
   the authorization request.
5. Rclone still validates its generated `state` after the request passes
   through a relay.
6. Existing commands behave exactly as before when no new option is supplied.
7. Two isolated rclone processes can authorize concurrently with different
   listener addresses and redirect URIs.
8. The caller can reliably discover the authorization URL and whether the
   flow has stopped.

### Non-objectives for upstream rclone

The rclone contribution should not include:

- a FastAPI or other application-specific service;
- a multi-tenant session database;
- user authentication or account ownership rules;
- a public callback broker operated by the rclone project;
- provider credentials belonging to `rclone-kit`;
- token storage outside rclone's existing configuration mechanisms; or
- general rewriting of provider authorization and token endpoints.

Those concerns either belong to the embedding application or would make a
small reusable rclone capability unnecessarily difficult to review.

## Ideal rclone change

### Separate three distinct addresses

The implementation should treat these values as distinct:

1. **Listener address**: the private TCP address passed to `net.Listen`, for
   example `127.0.0.1:49152` or `0.0.0.0:53682` inside an isolated network.
2. **OAuth redirect URI**: the exact public URI registered with the provider,
   for example `https://service.example.com/oauth/rclone/callback`.
3. **User authorization URL**: the public URL initially opened by the user,
   normally the public callback base plus rclone's `/auth` entry path and the
   rclone-generated state.

The listener address is not a URL. The redirect URI is not a bind address.
The authorization URL must not be constructed by blindly prepending `http://`
or concatenating unvalidated slashes.

An acceptable CLI surface would use narrowly named options such as:

```text
--oauth-listen-addr 127.0.0.1:49152
--oauth-redirect-url https://service.example.com/oauth/rclone/callback
```

The final names should be agreed with the maintainer. Names such as
`--redirect` are too broad for a global rclone flag and do not communicate
that the value is an OAuth redirect URI.

### Keep configuration local to the flow

The listener and redirect values should be passed through the authorization
call and stored on the individual auth server or OAuth configuration copy.
They should not mutate package-level redirect variables.

Per-flow values matter even if the current RC implementation allows only one
active OAuth flow per rclone process:

- independent `rclone authorize` processes must not share implicit state;
- tests must be race-safe;
- token refresh clients must not accidentally inherit a temporary callback
  setting; and
- a later internal concurrency improvement should not require undoing global
  state introduced by this feature.

Provider defaults must remain unchanged when the options are absent. The
custom redirect must be applied consistently to both authorization URL
generation and authorization-code exchange.

### Expose the URL as data

Current master exposes `status` and `authUrl` through
`config/oauthstatus`. A modern implementation should make that endpoint
return the externally usable authorization URL when a public redirect has
been configured. Human-readable command output may remain, but wrappers
should not have to depend exclusively on log wording.

For the `rclone authorize` command, a structured-output option would be useful
but is not required for the first upstream change. `rclone-kit` can initially
parse the command's existing stable markers while keeping that parsing behind
one tested adapter.

If `:0` is accepted as the listener port, rclone must report the actual address
selected by the operating system. Otherwise `:0` is not useful to a relay.
Supporting this would remove a port-allocation race, but it can be a follow-up
if it would delay the core listener/redirect change.

### Preserve rclone's existing web flow

Rclone's temporary server already has two relevant handlers:

- `/auth` validates the state in the initial URL and redirects the browser to
  the provider;
- `/` receives the provider callback, validates state, and supplies the code
  to the waiting rclone flow.

The new configuration should preserve that behavior. A relay can strip its
public prefix and forward these two requests:

```text
GET /oauth/rclone/callback/auth?state=S
    -> GET http://127.0.0.1:49152/auth?state=S

GET /oauth/rclone/callback?state=S&code=C
    -> GET http://127.0.0.1:49152/?state=S&code=C
```

The relay must preserve all query parameters, including provider errors. The
rclone server remains the component that accepts or rejects the state and
authorization result.

### Security and compatibility requirements

The upstream defaults should continue binding to loopback. Listening on a
non-loopback address must be an explicit operator decision and should be
documented as exposing a short-lived HTTP service that normally belongs on a
private network behind a trusted relay.

The change must account for the fact that OAuth behavior is shared by many
backends. It must not be a Google Drive-only conditional. Backends using
device authorization, out-of-band codes, client-credentials flows, or custom
OAuth handling must retain their current behavior unless they explicitly use
the new web-listener options.

An arbitrary public redirect URI will usually require an OAuth client whose
provider registration contains that exact URI. The option cannot make
rclone's built-in provider client registrations accept a new callback. This
is an application deployment requirement, not something the rclone patch can
solve.

### Tests expected upstream

The contribution should add focused tests for:

- unchanged default listener and redirect behavior;
- a custom listener with the default redirect untouched;
- a custom redirect with the default listener untouched;
- simultaneous listener and redirect overrides;
- propagation of the custom redirect into both the authorization request and
  the code exchange;
- correct construction with `https`, paths, trailing slashes, IPv4, and IPv6;
- invalid listener and redirect inputs;
- `config/oauthstatus` returning the usable authorization URL;
- state acceptance and rejection through the `/auth` and callback handlers;
- cancellation and listener cleanup; and
- race-detector coverage proving that independent flows do not rely on
  mutable package globals.

An offline integration test should use a fake OAuth authorization and token
server. A real provider test with a custom client and registered public
redirect should also be completed before claiming the deployment is proven.

### Documentation expected upstream

The option help and user documentation should explain:

- why the listener address and redirect URI differ behind a proxy;
- that defaults remain loopback-only;
- that TLS normally terminates at the public proxy;
- the exact paths the proxy must route and whether it must strip a prefix;
- that the redirect URI must be registered with the OAuth provider;
- that a custom provider client may be required; and
- that exposing the temporary listener directly to the internet is not the
  recommended deployment.

The rclone contribution guide requires general flags to be documented in
`docs/content/docs.md`. Autogenerated command documentation should be changed
through its Go source rather than edited directly.

## Current state of the upstream repository and proposal

### Existing issue and pull request

Issue #7634, opened on 2024-02-14, describes the same essential problem:
rclone in Docker needs a configurable callback listener and a redirect URL
that can point at a reverse proxy. It remains open.

PR #7635, also opened on 2024-02-14, proposes two global options:

```text
--auth-addr
--redirect
```

The PR remains open with six commits. The last code update and last substantive
author comment were on 2025-01-15. On 2025-01-14, maintainer Nick Craig-Wood
said he wanted to merge it for rclone 1.70 and asked the author to rebase,
resolve conflicts, and force-push a clean history. The author rebased by
merging upstream and replied that it might have been done correctly but they
were not sure.

There have been no submitted reviews and no useful CI result on the current
head. GitHub reports the PR as conflicting with master. Its milestone has
repeatedly moved forward and was moved to v1.75 on 2026-05-05, but milestone
maintenance is not evidence that the code has been made current or reviewed.

This history shows maintainer interest in the capability, but the existing PR
is not merge-ready and should not be treated as an implementation to vendor.

### Changes made by PR #7635

The proposed code:

- adds `AuthAddr` and `RedirectURL` fields to global `fs.ConfigInfo`;
- registers `--auth-addr` and the broadly named `--redirect` global flags;
- adds user documentation for both flags;
- copies the provider OAuth configuration and replaces its redirect URL;
- changes the package-level bind address when `--auth-addr` is supplied;
- overwrites the package-level public and localhost redirect variables when
  `--redirect` is supplied; and
- prints a second authorization URL intended for use through a reverse proxy.

The PR also contains unrelated or mechanical changes, including editor ignore
configuration, file-mode changes, formatting churn, and merge commits. These
make the actual feature harder to review.

### Problems in the proposed code

The old implementation should not be applied unchanged:

1. It builds the public authorization URL with logic equivalent to
   `"http://" + redirect + "/auth"`. A real value beginning with `https://`
   therefore produces an invalid URL.
2. It mutates package-level listener and redirect variables. That is unsafe
   for tests, later flows in the same process, and any concurrent use.
3. It applies the temporary redirect override in client-construction paths
   beyond the short-lived authorization exchange, increasing the chance of
   unintended effects on normal token clients.
4. The fields lack the same explicit configuration tags and lifecycle design
   used by current global options.
5. It assumes a textual concatenation relationship between callback and
   authorization URLs instead of modeling and validating URLs.
6. It does not robustly support an operating-system-selected port.
7. It predates the merged `config/oauthstatus` endpoint and therefore does
   not integrate the public URL with the current structured status API.
8. It has not been rebased onto current OAuth changes, including cancellation
   and status management.
9. It has no focused automated tests demonstrating a real proxied OAuth code
   exchange.
10. Its broad global names and unrelated changes are likely to invite another
    review cycle even after conflicts are resolved.

### Relevant changes already on master

PR #9466 was merged on 2026-05-28. Current rclone master now has:

- `config/oauthstatus`, returning `status` and `authUrl`; and
- `config/oauthstop`, cancelling a running OAuth server.

These are useful building blocks for a wrapper. They still describe one active
OAuth flow per rclone process through package-level status and cancellation
state. This reinforces the recommendation that `rclone-kit` isolate each
authorization in its own process rather than ask the first upstream change to
redesign RC for multi-session operation.

Other related open work includes listener-only, port, IPv6, and diagnostic
proposals. None replaces the required separation between private listener and
public provider redirect URI.

### Current state of `rclone-kit`

`rclone-kit` currently bundles official rclone 1.74.4 builds. It already has:

- explicit executable selection;
- checksum-verified, platform-specific bundled binaries;
- process-private temporary configuration files;
- a long-lived `Process` abstraction with process-tree cleanup;
- command logging with flag-value redaction; and
- a free-port helper.

There is no current authorization implementation. The previous document that
proposed implementing provider OAuth directly in Python was intentionally
removed. The remaining work should preserve rclone as the OAuth owner.

The existing free-port helper checks and then releases a candidate port before
rclone binds it, so it has a time-of-check/time-of-use race. The temporary
custom binary can initially retry on bind failure; the ideal upstream solution
is `:0` plus reporting the actual listener address.

## Recommendations for contributing the change

### Confirm how the maintainer wants the stalled PR handled

Do not silently open a duplicate. Add a concise, substantive comment to PR
#7635, addressing both the original author and maintainer. Offer to prepare a
clean current-master implementation and ask whether they prefer:

- a replacement PR that explicitly supersedes #7635;
- commits contributed to a refreshed original branch; or
- a narrower first PR with follow-up work.

The comment should explain the concrete container/library use case, the need
to preserve rclone's ownership of OAuth, and the planned tests. If a new PR is
requested, credit the original author and link #7634, #7635, and the related
status work.

### Reimplement cleanly on current master

Use the old PR as requirement evidence, not as a code base. Start from current
rclone master and keep the change limited to:

1. a per-flow listener setting;
2. a per-flow public redirect setting;
3. correct public authorization URL reporting;
4. tests; and
5. focused source documentation.

Avoid unrelated formatting, generated documentation, editor files, broad
refactoring, and merge commits. Use a house-style commit subject such as:

```text
oauthutil: support separate callback listener and redirect URL
```

The PR description should include a short reverse-proxy example and an exact
end-to-end test result. It should state explicitly that no-option behavior is
unchanged.

### Follow rclone's contribution requirements

Before submission:

- ensure the approach is explicitly agreed in the linked issue or old PR;
- read `reference/rclone/AGENTS.md` and `CONTRIBUTING.md`;
- understand and be able to explain every submitted line;
- run `go build`;
- run `make quicktest`;
- run focused `lib/oauthutil` tests;
- run `make racequicktest` where practical;
- run `golangci-lint run ./...` or `make check`;
- exercise a real provider with a registered public redirect URI; and
- add only the source documentation rclone expects.

### Use a temporary patched binary without losing the upstream path

A custom binary is a reasonable way to unblock `rclone-kit` while upstream
review proceeds, provided it is treated as a maintained downstream patch and
not as an undocumented binary replacement.

Recommended temporary workflow:

1. Create a named branch in a maintained rclone fork from a recorded upstream
   commit.
2. Implement the same clean change intended for upstream; do not vendor the
   stale #7635 head.
3. Keep the downstream difference to one or a few reviewable commits that can
   be rebased and exported as patch files.
4. Give the build an identifiable version suffix such as
   `v1.75.0-rclone-kit.1`.
5. Build Windows amd64 and Linux amd64 artifacts reproducibly from that exact
   source revision.
6. Record the upstream commit, downstream commit, build toolchain, archive
   hashes, executable hashes, and rclone MIT license.
7. Update `rclone-kit`'s artifact manifest and distribution verification in
   the same review that changes the bundled executable.
8. Run the upstream Go tests plus `rclone-kit` unit, integration, distribution,
   and live authorization tests against the custom binary.
9. Document that the wheel contains a downstream rclone build and provide the
   corresponding source and patch, satisfying both operational traceability
   and license obligations.
10. Define the removal condition: once an official rclone release contains
    the accepted behavior and passes the same tests, switch artifact metadata
    back to the official release and delete the downstream patch.

Applications should also be able to supply an explicit `rclone_exe` path so a
custom binary can be tested before it becomes the bundled default.

### Stage the work

The lowest-risk sequence is:

1. build and test the clean patch in the local reference checkout;
2. prove one real authorization through a small relay using a custom provider
   client and registered callback;
3. implement the framework-neutral `rclone-kit` session and relay API against
   that binary;
4. test concurrent isolated authorization processes;
5. request upstream direction on #7635 with concrete results available;
6. submit the upstream PR in the maintainer's preferred form; and
7. retain the custom binary only until the change appears in a verified
   official rclone release.

This sequence delivers the product capability without making the product
dependent on the timing of upstream review, while keeping the downstream work
shaped for eventual deletion rather than permanent divergence.
