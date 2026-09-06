# Gemini Developer API compliance record

- Last checked: 2026-08-04
- Integration: Gemini Developer API (`generativelanguage.googleapis.com`)
- SDK: `google-genai>=2.16.0,<3.0.0`
- Authentication: bring your own API key (BYOK), loaded from `.gemini_key` or `GEMINI_API_KEY`
- Models offered by the regular application: `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.5-flash`, plus stable 2.5 IDs for saved-setting compatibility

## Primary sources

- Additional terms: https://ai.google.dev/gemini-api/terms
- Google APIs Terms of Service: https://developers.google.com/terms/
- Pricing: https://ai.google.dev/gemini-api/docs/pricing
- Rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Model lifecycle: https://ai.google.dev/gemini-api/docs/deprecations
- SDK migration: https://ai.google.dev/gemini-api/docs/migrate
- Prohibited use policy: https://policies.google.com/terms/generative-ai/use-policy

## Commercial distribution assessment

The terms contemplate API clients being made available to users and describe the service as being for professional or business purposes. They do not state a blanket ban on charging for a value-added application. This is an implementation assessment, not a legal opinion.

Required constraints:

- Do not direct the application to, or make it likely to be accessed by, people under 18.
- Offer it only in an available region. API clients offered in the EEA, Switzerland, or the United Kingdom must use Paid Services.
- Do not sublicense or resell the API itself, and do not ship a client that functions substantially the same as the Gemini API.
- Do not imply Google partnership, sponsorship, or endorsement.
- Require each customer to provide and control their own API key. Never put the seller's key in a repository, application bundle, ZIP, log, fixture, or support message.
- Require users to have the rights needed for every transcript, video, prompt, and other input they submit.
- Comply with the prohibited-use policy and do not bypass model safety controls.

Clip Extractor adds transcription, highlight selection, video rendering, subtitles, thumbnails, chapters, and editor export; it is not designed as a general Gemini API proxy. Legal review is still required before public sale.

## Data and output rights

- The app sends transcript text, the system instruction, and the user's custom prompt to Google. It does not send the source video or audio to Gemini.
- Under Unpaid Services, Google may use submitted content and generated responses to improve products, and human reviewers may process them. Do not send sensitive, confidential, or personal information through unpaid quota.
- Under Paid Services, Google states that prompts and responses are not used to improve its products. Limited safety and abuse logging still applies.
- Google states that it does not claim ownership of original generated content. Similar output may be generated for others, and the developer/user remains responsible for lawful use and any required attribution.
- This integration does not use Google Search or Maps grounding. Their separate storage, display, and attribution terms are therefore out of scope.

## Runtime secret controls

- A saved API key is resolved only on the server when an analysis request runs. It is never used as the initial value of the Gradio textbox or embedded in the browser configuration.
- Both supported launch paths bind the web UI to `127.0.0.1`; other devices on the LAN cannot open the app or trigger requests through it.
- Gemini requests have a 300-second HTTP timeout and are not automatically retried, limiting both indefinitely stalled workers and accidental duplicate paid requests.
- `.gemini_key` is written through `secret_store.py`: user-scoped DPAPI encryption on Windows (the only supported OS), with plaintext existing only on unsupported platforms. It is gitignored either way.

## Pricing snapshot

Standard paid rates per 1,000,000 tokens in USD on the checked date:

| Model | Input | Output, including thinking |
|---|---:|---:|
| `gemini-3.5-flash-lite` | $0.30 | $2.50 |
| `gemini-3.6-flash` | $1.50 | $7.50 |
| `gemini-3.5-flash` | $1.50 | $9.00 |

All three showed free input and output in the published free tier. Pricing and limits can change; verify the primary pricing page when producing each release artifact.

## Free-tier usage guidance

- A normal Clip Extractor analysis makes one Gemini request per video. The integration does not automatically retry API failures.
- Use a few videos per day as a conservative onboarding guide, not as a promised quota.
- Google measures limits using requests per minute (RPM), input tokens per minute (TPM), and requests per day (RPD), applies them per project, and states that actual capacity can vary.
- Long transcripts may reach an input-token limit before reaching a request-count limit.
- Check the customer's current limits at https://aistudio.google.com/rate-limit?timeRange=last-28-days. A `429 RESOURCE_EXHAUSTED` can reflect RPM, TPM, RPD, or other tier/capacity limits; wait for the displayed reset, reduce input/frequency, or change tier as appropriate rather than promising recovery in a few minutes.

## Distribution profile decision

The future sales ZIP should expose only:

- `gemini-3.5-flash-lite` as the cost-focused default
- `gemini-3.6-flash` as the quality-focused option

The distribution build should disable arbitrary model IDs and migrate unsupported saved IDs to the default. Model restrictions improve supportability but do not prove Paid Service status; billing status belongs to the customer's Cloud project.

## Release blockers

`COMMERCIAL-READINESS: BLOCKED`

Before public sale or paid distribution:

- [x] Publish product terms and a privacy policy covering Google, YouTube, Drive, local files, logs, and retention. `docs/TERMS.md` and `docs/PRIVACY.md` are complete for the decided model (individual distributor `machaniconico`, free of charge, GitHub Release only, Japan-resident users aged 18+, Windows 10/11 only, support via GitHub Issues, Japanese law, distributor's home-district court). Human legal review is still outstanding (below).
- [x] Decide the sales regions and implement the age/region/Paid-Service restrictions required by the Gemini terms. Decided 2026-09-07: distribution is limited to Japan-resident users aged 18 or over, stated in `docs/TERMS.md` §1 and `docs/PRIVACY.md` §6 and in `README.md`. EEA/Switzerland/UK users are out of scope, so no Paid-Service gating is needed.
- [x] Replace or formally accept the risk of plaintext `.gemini_key` storage. Replaced: `secret_store.py` encrypts it with user-scoped DPAPI on Windows, and support is declared Windows-only, so the plaintext fallback applies only to unsupported platforms.
- [x] Build the distribution profile and verify that secrets, tokens, credentials, logs, outputs, and prior ZIPs are absent from the artifact. `.github/workflows/release.yml` builds the ZIP with `git archive` (gitignored files can never be included), rejects any entry matching the secret/log/output deny-list, and runs `tools/check_secrets.py --directory` on the extracted archive.
- [x] Complete dependency/SBOM, OSS/font/media license, secret, SAST, and artifact vulnerability reviews for the exact release artifact. The release workflow generates the SBOM from the extracted archive, checks `THIRD_PARTY_NOTICES.md` against it, and runs `pip-audit` and `bandit` on the extracted archive before attaching assets to a draft release.
- Re-check current API terms, prices, model lifecycle, rate limits, and data handling.
- Obtain human legal review for consumer-protection, privacy, copyright, store, refund, and jurisdiction-specific obligations. Points to raise: the liability clause in `docs/TERMS.md` §8 (free software, cap of 0 yen, carve-out for intent/gross negligence, Consumer Contract Act override), and the venue clause in §12 (distributor's home-district court).

`LEGAL-REVIEW: HUMAN-REVIEW`
