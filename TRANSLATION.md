# Clipboard translation

Copying local prose shows a translation invitation when Settings → 翻译 is enabled. Links, image copies and file copies do not show this invitation. Video link recognition is independent. Source-language detection is local and lazy, using ELDC. Short or uncertain text displays 自动识别 instead of guessing.

The flag toggles a scrollable eight-row chooser with 30 persisted targets (Simplified Chinese first, Traditional Chinese second); it opens at its start. Expanded content collapses on an outside click or two seconds after losing focus. Returning focus cancels the deadline; choosing a language counts as inside interaction. The existing native input tick covers clicks in other applications, without another global hook or timer loop. Copying never automatically calls the paid API. The right button starts translation, then copies the complete result. The middle button expands original and translated plain text upwards. New copies, target changes, disabling translation and shutdown fence stale results. Writing a translation does not recursively show another translation invitation.

Both new translation invitations and completed translation capsules remain for five seconds. Opening the language chooser or expanded content pauses dismissal; returning to the capsule resumes a fresh five-second window. In-flight translation has no dismissal deadline.

## Service and key

The client calls the public `/v1/translation` endpoint without an account or session token with text, target and a UUID. It contains no DeepSeek key or provider configuration screen. The service uses `deepseek-flash` with thinking disabled and only returns the translated text. Keep `DEEPSEEK_API_KEY` in Cloudflare Worker Secrets, never a public variable, repository file, package, client configuration or chat transcript. The request uses existing D1 nonce storage; it does not wake the notification Durable Object. No migration or dependency is required on the server.

The user explicitly requested no account/global usage quotas. There are no daily counters, burst restrictions or concurrency caps for translation. A unique request nonce prevents automatic network retries from creating duplicate paid calls. Only this nonce is persisted for one day in the existing `rates` table; source and translated text are not logged or persisted. Input (16,000 characters), output, response bytes and request deadlines are bounded to reject malformed or incomplete responses. These are request validity boundaries, not a usage quota. Failed/incomplete translations never display a copy-success state.

Translation requires no login; the provider key remains a server Secret and is never returned to a caller. Account and sync routes still require authentication. Settings uses a single scroll document with left anchor navigation and scroll tracking. It retains existing control data flows and immediate-save switches.

## Verification

- `python -m unittest -v test_translation` checks eligibility, explicit calls, stale-result fences, persisted preferences, local detection and settings anchors without the real clipboard/provider.
- `node cloud/test/translation.mjs` checks provider request shape, lack of usage quotas, idempotency, bounded validation, incomplete responses and credential-safe errors using a provider fixture.
- `node cloud/node_modules/typescript/bin/tsc --noEmit -p cloud/tsconfig.json` checks the server.
- Run adjacent existing settings, account layout, native notice, clipboard and architecture checks. UI rendering uses Qt offscreen and disposable profiles only.
- Real provider verification completed through the authenticated app endpoint: a fixed English sentence returned a Chinese translation in about 1.1 seconds. Repeating its request ID returned 409 without a second paid call. The temporary QA account was removed. The DeepSeek key was never retrieved: it remained a Worker Secret. The user subsequently authorized completing the release after this feature was added. Installer and update-feed verification is recorded separately in the delivery report.

Completed copy notices shrink and fade together with a monotonic exit curve, without a compact-state detour, delayed fade or rebound. The three-second success hold and pending-work compact state are preserved.
