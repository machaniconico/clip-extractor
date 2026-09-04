# X API compliance record

- Last checked: 2026-09-02
- Integration: X API v2 `POST https://api.x.com/2/tweets`
- Authentication: OAuth 1.0a user context (API Key / API Key Secret / Access Token / Access Token Secret)
- Dependency: `requests-oauthlib>=2.0.0,<3.0.0` (ISC license)
- Account model: bring your own developer app and credentials (BYOK)

## Primary sources

- Create Post endpoint: https://docs.x.com/x-api/posts/create-post
- OAuth 1.0a user context: https://docs.x.com/fundamentals/authentication/oauth-1-0a/overview
- Authentication security guidance: https://docs.x.com/fundamentals/authentication/guides/authentication-best-practices
- Pricing: https://docs.x.com/x-api/getting-started/pricing
- Post-management limits: https://docs.x.com/x-api/posts/manage-tweets/integrate
- Automation rules: https://help.x.com/en/rules-and-policies/x-automation
- Developer Agreement: https://docs.x.com/developer-terms/agreement
- Developer Policy: https://docs.x.com/developer-terms/policy
- `requests-oauthlib` source and license: https://github.com/requests/requests-oauthlib

## Current scope and user consent

- The feature posts only the user's own stream announcement. It does not read, analyze, cache, redistribute, or resell X Content.
- Automatic posting is disabled by default and requires both the existing stream-start announcement switch and the separate **X APIで完全自動投稿する** opt-in.
- The UI explains the action before opt-in. Turning either switch off stops future automated actions.
- One active stream can trigger at most one API request. Duplicate stream-start events are ignored until a stream-finished event resets the guard.
- The implementation does not retry a failed API write. A failure opens the prefilled composer for an optional manual post instead.
- The user remains responsible for the post body, linked content, account behavior, developer use-case registration, and ongoing compliance with the X Rules and Automation Rules. Duplicate, spammy, misleading, abusive, private, or unauthorized content is not permitted.

This is intended to meet the Automation Rules for explicit consent and helpful informational posts while limiting duplicate activity. It is an implementation assessment, not a legal opinion or approval from X.

## Credential and data handling

- Each user supplies credentials generated from their own X developer app. Seller or shared credentials must never be bundled, logged, placed in fixtures, or provided to another user.
- The four values are stored in the gitignored local `.x_credentials.json` sidecar, matching the existing `.gemini_key` / `.obs_password` pattern. They are excluded from `default_settings.json`. Writes are atomic and owner-only where POSIX modes are supported.
- Credential fields are password inputs and always start empty. Saved values are resolved only on the server when OBS integration starts and are never returned to the browser as component values.
- On Windows, all three secret sidecars are encrypted at rest with user-scoped Windows DPAPI. DPAPI normally binds decryption to the same Windows user account and computer; it does not protect secrets from malware running as that user. Non-Windows installs (including WSL, Linux, and macOS) fall back to plaintext sidecars for development compatibility.
- The signed HTTPS request sends the rendered announcement text to X and the OAuth signature in the authorization header. The app retains only the returned Post ID long enough to show `https://x.com/i/web/status/<id>` in the session status.
- HTTP response bodies and underlying exception strings are not logged or displayed. User-facing errors contain only a fixed category and HTTP status.
- To stop posting, turn off automatic posting. To fully revoke access, revoke/regenerate the tokens in X Developer Console and remove `.x_credentials.json` from the install directory.

## Cost and limits snapshot

On the checked date, X documented credit-based pay-per-use pricing. A content-create request was listed at USD $0.015, or USD $0.200 when the Post contains a URL. Most stream announcements contain a URL and should be budgeted at the latter rate. Successful writes consume the user's developer credits; exhausted credits cause requests to fail.

X's official pages currently conflict: the Post-management integration guide lists 200 POST requests per user per 15 minutes and a combined create/Retweet limit of 300 requests per 3 hours, while the central Rate Limits table lists `POST /2/tweets` as 100 requests per user per 15 minutes and 10,000 requests per app per 24 hours. Until X reconciles those pages, this integration treats the stricter 100-per-user/15-minute value and the separate 300-per-3-hour combined ceiling as conservative maximums. This feature is intentionally far below either limit, does not retry, and must not be modified to bypass them. Pricing, credits, plan eligibility, and limits can change; the final authority for each account and release is its Developer Console and returned rate-limit headers.

## Distribution and rights assessment

- `requests-oauthlib` 2.0.0 is ISC-licensed and may be redistributed subject to retaining its license notice in the distribution's dependency notices.
- The integration calls the official X API and does not automate the X website, share API keys, or expose the API as a proxy/service bureau.
- X's self-serve terms describe limited-user and early-stage integrations and require the registered use case to remain accurate. A public or paid distribution must confirm that its exact scale, regions, account model, branding, and commercial use fit the user's X plan and approved use case.
- The application must not imply X sponsorship or approval. The created Post's source label is controlled by the user's developer app configuration.

## Release blockers

`COMMERCIAL-READINESS: BLOCKED`

Before public sale or paid distribution:

- Re-check the current Developer Agreement, Developer Policy, Automation Rules, pricing, credits, and write limits for the exact release date.
- Confirm with X that the final distribution and BYOK account model fit the approved use case and plan; obtain Enterprise terms if the self-serve scope is exceeded.
- For any non-Windows distribution, decide whether the documented plaintext fallback is acceptable or replace it with that platform's credential store; supported Windows installs use user-scoped DPAPI.
- [x] Include the ISC license notice for `requests-oauthlib` in the dependency notices. `THIRD_PARTY_NOTICES.md` is generated by `tools/generate_notices.py` from the installed dependency graph and includes the shipped ISC text.
- [x] Produce a machine-readable SBOM for the installed release dependency graph. CI generates CycloneDX 1.6 JSON and uploads it as the `sbom` workflow artifact.
- Attach the SBOM to a released distributable. The repository does not build a distributable yet.
- [x] Run secret, dependency, and SAST scans over the repository and verify that `.x_credentials.json`, logs, test fixtures, and real credentials are absent. `tools/check_secrets.py`, `tools/generate_notices.py --check`, `pip-audit`, and `bandit` run as hard-fail gates in the CI `scan` job.
- Add `scan` to the required status checks for `main`. The job exists but branch protection is repository configuration, so a failing scan does not block a merge until it is marked required.
- Scan the final packaged artifact before release. CI scans the source tree only; no distributable is built in this repository yet.
- For dependency updates, regenerate `requirements.lock` and `THIRD_PARTY_NOTICES.md` together in the same PR; CI installs and audits the lock, while the notices check validates the installed locked graph.
- Publish user-facing terms and privacy disclosures covering X API posting, credentials, costs, revocation, retention, and support.
- Obtain human legal review for the intended sales regions and distribution model.

`LEGAL-REVIEW: HUMAN-REVIEW`
