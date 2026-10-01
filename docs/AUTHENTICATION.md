# Authentication

[Back to AppTrail](../README.md) · [Self-hosting](SELF_HOSTING.md)

AppTrail has one owner account per database. Creating an account signs you in. There is no open registration after that account exists, no guest mode, and no HTTP Basic fallback. Your username and Argon2id password hash persist in the same SQLite database as your apps. AppTrail does not collect an email address or send verification emails.

## First launch

AppTrail requires account setup when no owner account exists. Background checks wait until setup is complete.

The server generates a random one-time setup code, stores it in a permission-protected `setup-token` file in the data directory, and prints it at startup. Enter that code on the account creation page. It is not available through the public API and is removed after successful setup. Concurrent registration attempts cannot create a second owner. If the server restarts before setup, the same code remains valid.

Choose a username of 3–64 letters, numbers, dots, underscores, or hyphens, starting with a letter or number. Usernames are case-insensitive. Passwords require 8–128 characters, including at least one number (0–9) and one special character, such as `!` or `@`. Spaces are preserved but do not count as special characters.

If you already have a SQLite backup, select **Have a backup? Restore your data** below **Create account**. Enter this server's setup code and upload the backup. After restoration, sign in with the username and password saved in the backup. This option is available only before an owner account exists.

## Sessions and requests

A successful login issues a random cookie with `HttpOnly`, `SameSite=Strict`, and a host-only scope. Requests that AppTrail sees as HTTPS also use `Secure` and the `__Host-` cookie prefix. A proxy can forward the original scheme through the optional [forwarded-header settings](SELF_HOSTING.md#public-https-hosting). Sessions are bound to the scheme, host, and port seen by AppTrail when signing in. SQLite stores a hash of the session token, not the token itself. Tokens are not stored in browser local storage.

Sessions last 24 hours from sign-in, including time spent away from the app. The persistent cookie survives browser restarts, and saved sessions survive server restarts when using the same database and origin. Requests do not extend the 24-hour limit. Logout revokes the current session immediately. Changing or recovering a password revokes all sessions; an in-app password change starts a fresh session for the current browser. Downloaded backups exclude login sessions.

Every workspace route requires a valid session, including reads, searches, discovery, key changes, exports, backups, and the dashboard HTML. Only the login/setup page, its static assets, authentication status and entry endpoints, and the minimal health probe are public. Data responses use `Cache-Control: no-store`.

Writes require JSON, a custom request header, and a CSRF token tied to the authenticated session. SQLite restoration accepts a binary upload with `Content-Type: application/vnd.sqlite3` and `X-AppTrail-Confirm-Restore: overwrite`, with the same session, custom header, and CSRF checks. Login and setup require JSON and the custom header before a session exists. Requests marked `cross-site` by the browser's `Sec-Fetch-Site` header are rejected, and no cross-origin access is enabled through CORS. AppTrail does not compare the browser's `Origin` header with the internal server address, so an HTTPS proxy can forward HTTP without blocking account setup or login. Login, setup, and password changes share a persisted limit of ten attempts per client address per five minutes and a global limit of 100 per minute. See the proxy instructions before forwarding client addresses.

These controls follow the [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), and [CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) guidance. They are covered by automated tests and browser checks, not an independent security assessment. There is no MFA or SSO in this version.

Before account creation, `POST /api/auth/restore` accepts the same SQLite upload and overwrite confirmation with `X-AppTrail-Setup-Token` containing the server's setup code, in place of a session and CSRF token. It requires the custom request header and rejects cross-site requests. Attempts share the setup and login rate limits. AppTrail checks that no owner exists both before accepting the upload and immediately before restoring, then removes the setup code after success.

## API clients

Use an HTTP client with a cookie jar. Submit JSON to `POST /api/auth/login` with `username` and `password`, plus `X-AppTrail-Request: 1` and `Content-Type: application/json`. Retain the returned cookie and `csrf_token`. Send the cookie on subsequent requests; writes also need `X-CSRF-Token` with that value and the same JSON/custom headers. `GET /api/auth/status` returns the current session's CSRF token. Never place passwords or session tokens in URLs.

HTTP 401 means authentication is required. HTTP 403 indicates a failed setup code, browser cross-site check, request-header requirement, or CSRF check. HTTP 429 indicates login throttling. Server IPs and custom domains work over HTTP or HTTPS without an origin setting or host allowlist.

## Recovery and background work

Restoring a SQLite backup in Settings replaces the owner account and password with those saved in the upload. It revokes all current and uploaded sessions. Sign in again with the restored credentials. Invalid uploads leave the current account and sessions intact. See [backups and restoration](SELF_HOSTING.md#backups-and-restoration) for compatibility limits and recovery copies.

Change a known password in Settings. Recover a forgotten password with `apptrail --reset-password` on the server, using the same data directory and operating-system user. Stop the AppTrail service first. Server filesystem access is required; there is no unauthenticated web password-reset endpoint.

The worker waits for an owner account before processing searches. After setup, scheduled monitoring continues while you are signed out. Logging out stops browser access, not scheduled work. Pause queries in the dashboard to stop monitoring.

Anyone with access to the server's data files or process environment may access the stored SerpApi credential. Authentication protects HTTP access; it does not replace filesystem permissions, TLS, private backups, or host security. The SerpApi key remains server-side and is never returned by the UI API.
