# Data and Cookie Consent Implementation Plan

**Goal:** Add a legally defensible consent flow without blocking authentication when a user declines optional analytics.

**Design source:** `docs/superpowers/specs/2026-09-05-data-and-cookie-consent-design.md`

**Provider-neutral refinement status:** Design approved and recorded in commit `1afcff6`; implementation and its tests are still pending. Terra execution stopped at its usage limit. Incomplete config/modal edits were rolled back to the previously tested consent behavior; the existing cookie and consent implementation remains intact.

## 1. Consent domain and persistence

- Add explicit document revisions and document hashes for the user terms, AI consent, and analytics consent.
- Add an append-only `legal_events` table for acceptance and withdrawal evidence.
- Store only the minimum evidence required to prove an action; do not copy profile or resume content into audit events.
- Issue a signed `site_consent` cookie for `necessary` or `analytics` choices.

## 2. Server enforcement

- Expose `POST /api/site-consent` and provide the verified consent state to every rendered page.
- Reject `/api/track` unless the signed cookie grants analytics.
- Keep session, anonymous generation, and OAuth-state cookies available as necessary cookies.
- Carry user-terms acceptance through email magic-link and OAuth flows, then persist evidence after identity is established.

## 3. Browser behavior

- Render a shared, non-modal cookie banner on every public and authenticated page while the state is unknown.
- Lazy-load Yandex Metrika only after an explicit analytics choice; remove the fallback tracking pixel.
- Gate preference storage and funnel events on analytics consent.
- Treat the resume profile separately: persist it locally only after its own explicit checkbox is enabled.

## 4. Legal documents and user controls

- Publish standalone user terms and AI-processing consent pages.
- Update the privacy policy with purposes, legal bases, retention, recipients, cross-border processing, withdrawal, and deletion procedures.
- Add a persistent control that reopens cookie settings.

## 5. External AI transfer guard

- Use a universal document template backed by an explicit deployment provider profile; never accept a blanket consent for unnamed future providers.
- Treat local processing by the Operator as the requested service operation, without storing an external-transfer consent.
- Require an external profile with stable ID, legal name, country, address/contact, and terms URL.
- Bind external consent to a canonical provider fingerprint and immutable event snapshot so a provider change invalidates old consent automatically.
- Keep local/private model endpoints working by default and fail closed for an external endpoint with an incomplete profile.
- Fail closed for a cross-border AI endpoint until the operator explicitly confirms the required deployment compliance.
- Record external-transfer payloads first in the configured local database, delete them after the request, and clean stale buffer rows.

## 6. Verification

- Test signed-cookie validation, tamper rejection, cookie attributes, analytics gating, and withdrawal.
- Test terms enforcement for email and OAuth without coupling it to analytics consent.
- Test that Metrika is absent before opt-in and that browser profile persistence is a separate opt-in.
- Test local processing without an external consent event, external provider re-consent after fingerprint changes, immutable provider snapshots, and incomplete-profile rejection before the model call.
- Run the complete automated suite, inspect rendered desktop/mobile states, and verify UTF-8/CRLF preservation.
