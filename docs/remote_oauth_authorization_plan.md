# Remote OAuth authorization plan

## Status

Proposal, not implemented. No code in this repository yet reflects this
document. It exists to align on an approach before writing any of it.

## Problem

rclone supports many OAuth-based backends (Google Drive, Dropbox, OneDrive,
Box, and others). Setting one up today needs either an interactive terminal
with a local browser (`rclone config create` / `rclone authorize`), or the
documented SSH-tunnel trick for headless machines - see "Why `rclone
authorize` alone doesn't solve this" below.

None of that fits a service shape: `rclone-kit` running inside an isolated,
remote Linux process (for example, behind a FastAPI endpoint) that needs to
let an arbitrary end user - who has no shell access to that machine at all -
grant access to their own Google Drive through a link the service hands
them, from whatever machine and browser the user happens to be using.

## Why `rclone authorize` alone doesn't solve this

Verified against the bundled rclone binary (`rclone-v1.74.4`):

```text
$ rclone authorize drive --auth-no-open-browser
NOTICE: Make sure your Redirect URL is set to "http://127.0.0.1:53682/" in your custom config.
NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=ZNTYXJypfDskY-uGlmmtyQ
NOTICE: Log in and authorize rclone for access
NOTICE: Waiting for code...
```

`--auth-no-open-browser` only stops rclone from trying to open a local
browser itself - it does not change where the printed link points. rclone
always starts its own short-lived HTTP listener on `127.0.0.1:53682` on
whichever machine runs the `authorize`/`config create` process, and that
loopback address is also the OAuth `redirect_uri` Google is configured to
send the browser back to after consent.

Handing that link to an end user on their own workstation cannot work:
`127.0.0.1:53682` resolves to *their* machine, not the server's, so the
redirect after they grant access has nowhere valid to land - nothing is
listening on their own port 53682.

This is exactly why rclone's own guidance for headless setups recommends
one of:

- running `rclone authorize` on a *different* machine that has both rclone
  and a browser, then pasting the printed token into the remote config by
  hand; or
- SSH port-forwarding the remote's `127.0.0.1:53682` back to the operator's
  own machine (`ssh -L 53682:localhost:53682 remote-host`) before running
  `rclone authorize` on the remote side.

Both assume an *operator* with shell access to the remote host, not an
arbitrary *end user* authorizing through a web app. Neither fits a service
that hands out a link to its own users.

## Proposed approach: run the OAuth Authorization Code flow ourselves

Don't route the end-user-facing step through rclone's `authorize` helper at
all. Implement the standard OAuth2 Authorization Code flow directly in the
service, using our own Google OAuth client (Google Cloud Console, an OAuth
client ID of type "Web application") with a real public redirect URI the
service owns, for example `https://service.example.com/oauth/gdrive/callback`.
Only the *token*, once obtained, needs to end up shaped the way rclone
expects; nothing about acquiring it needs to go through the rclone binary or
its loopback server.

Flow:

1. `GET /connect/gdrive` (a service endpoint) builds and returns Google's
   authorization URL:

   ```text
   https://accounts.google.com/o/oauth2/v2/auth
     ?client_id=...
     &redirect_uri=https://service.example.com/oauth/gdrive/callback
     &response_type=code
     &scope=https://www.googleapis.com/auth/drive
     &access_type=offline
     &prompt=consent
     &state=<opaque, single-use, per-request token>
   ```

2. The end user opens that link on *their own* machine, signs into Google,
   and grants access. Because the redirect URI is a real public HTTPS
   endpoint the service owns, this works from anywhere - there is no
   loopback problem, since Google never needs to reach back into the
   service's private network at all until step 3, and that happens
   server-to-server.
3. Google redirects the user's browser to
   `GET /oauth/gdrive/callback?code=...&state=...`. The service validates
   `state` (CSRF/replay protection, see below), then exchanges `code` for
   tokens directly against Google's token endpoint
   (`POST https://oauth2.googleapis.com/token`) using our `client_id` and
   `client_secret`.
4. Google's token response (`access_token`, `refresh_token`, `expires_in`,
   ...) is reshaped into the exact JSON rclone stores in a remote's `token`
   config field: `access_token`, `token_type`, `refresh_token`, and
   `expiry` - an RFC3339 timestamp computed as `now + expires_in`, not the
   raw `expires_in` seconds value itself.
5. The service builds the `[gdrive]`-equivalent config section (`type =
   drive`, `client_id`, `client_secret`, `scope`, `token = <the JSON from
   step 4>`) - either as a per-user `rclone_kit.Config` object held in
   memory or secrets storage, or written into a per-user config file - and
   hands it to `Rclone(...)` exactly like any other config.

From this point everything already works as documented: rclone itself
refreshes the access token using the refresh token whenever it expires, the
same as any locally-configured remote (see the discussion of `expires_in`
and refresh tokens already covered for the manually-created `gdrive`
remote in this project).

## What rclone-kit would need to add

None of the above requires the rclone binary at all until step 5, so it
does not belong in `rclone_kit.client.Rclone` (which is a thin wrapper
*around* the rclone process). It fits better as a new, optional module,
imported lazily like `rclone_kit.s3` and `rclone_kit.db` are today:

- `rclone_kit.oauth` (new, optional; needs an HTTP client extra, for
  example `httpx`, which the project already depends on for
  `http_server.py`):
  - `build_authorize_url(provider: OAuthProviderConfig, state: str) -> str`
  - `exchange_code_for_token(provider: OAuthProviderConfig, code: str) -> OAuthToken`
  - `OAuthToken.to_rclone_config_value() -> str` - the exact JSON shape
    rclone's `token` field expects.
  - `OAuthProviderConfig` - a small, provider-specific dataclass
    (`client_id`, `client_secret`, `auth_url`, `token_url`, `scope`,
    `redirect_uri`); ship a `GOOGLE_DRIVE` constant, add others
    (Dropbox, OneDrive) only once there is a real second consumer.

This keeps `rclone_kit`'s core promise intact: importing the package must
not require optional extras or start network activity, and this module
would only be imported by a caller that actually wants to drive an OAuth
flow.

## Security considerations

- **`state` must be single-use, short-lived, and bound to the request that
  created it** (store it server-side keyed by a session or pending-request
  ID, not just echoed back). Reject a callback with an unknown, expired, or
  already-consumed `state`.
- **Expire the pending-authorization link** (a few minutes is standard) so
  an unopened `/connect/gdrive` link can't be replayed indefinitely.
- **Never log tokens.** `client.py`'s command logging already redacts
  recognized credential flags for rclone subprocess calls; this new code
  path must apply the same discipline to its own logging - log that a
  token exchange happened, never the token JSON itself.
- **Store tokens as secrets**, not inline in application logs, error
  messages, or client-visible responses. If tokens are persisted per user,
  encrypt at rest the same as any other credential material.
- **Scope minimization**: request the narrowest Drive scope the feature
  actually needs (for example `drive.file` instead of the blanket `drive`
  scope) where that's sufficient.
- **Multi-tenant isolation**: a real service authorizes many end users
  against many of their own Drive accounts, not one shared remote. Token
  storage must be keyed per user/account, and one user's config must never
  be constructible from another user's request.
- **Revocation**: document how a user disconnects (Google's own
  "Third-party apps with account access" page always works regardless of
  what this service does; consider also calling Google's token revocation
  endpoint when a user explicitly disconnects, so the refresh token stops
  working immediately rather than just being deleted from local storage).
- **Rate-limit the callback endpoint** like any other public,
  unauthenticated-by-design endpoint.

## Alternative considered: rclone's own `rc` config-creation flow

rclone's `rc` HTTP API exposes a `config/create` call with a `state`/
`continue` continuation mechanism - this is what drives the "Configure a
new remote" wizard in `rclone rcd --rc-web-gui`'s browser UI, which people
do run on a remote host and complete from a separate browser. If that rc
server's own OAuth redirect handling can be bound to a publicly reachable
address (rather than assuming `127.0.0.1`), it would let the *actual rclone
OAuth implementation* drive this instead of reimplementing the
Authorization Code flow by hand.

This was not chosen as the primary approach because that redirect-handling
behavior has not been verified in this environment - only the plain
`rclone authorize` loopback behavior above has been confirmed directly. If
someone wants to pursue it, treat it as a separate spike: stand up `rclone
rcd --rc-web-gui --rc-addr <public-bind>`, trigger `config/create` for
`drive` with a non-interactive `state` continuation, and confirm whether
the redirect URI it hands to Google is the rc server's own public address
or still a fixed `127.0.0.1` value. If it's the former, this becomes the
preferred approach, since it avoids reimplementing OAuth token exchange and
stays on rclone's own maintained code path; if it's the latter, it has the
exact same problem as plain `authorize` and the hand-rolled flow above
remains the only option.

## Rollout plan

1. Spike the `rc` web-GUI alternative above; decide which approach to
   build based on the result.
2. If proceeding with the hand-rolled flow: add `rclone_kit.oauth` with
   Google Drive as the only provider, behind its own tests using a fake
   HTTP transport (no real Google calls in unit tests - mirror the
   fake-S3 pattern already used for the multipart upload tests).
3. Wire a minimal example service endpoint (not part of the library) that
   exercises the flow against real Google OAuth once, manually, before
   trusting the unit tests alone.
4. Extend `tests/live/gdrive` (see `implementation_and_build_pipeline.md`'s
   test list) to cover a token minted through this flow, not just one
   created via `rclone config create`, once the module exists.
5. Add Dropbox/OneDrive providers only when there is a real consumer for
   them; do not speculatively generalize `OAuthProviderConfig` beyond what
   Google Drive actually needs today.
