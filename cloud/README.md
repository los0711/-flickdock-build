# FlickDock Cloud

TypeScript Workers + D1 + private R2. Runtime/API/security details: ../CLOUD_SYNC.md.

Local: npm ci; npm run typecheck; npm test. Tests use isolated actual local workerd/D1/R2, no cloud credentials or emails. The 18 scenarios include auth challenges, Unicode uniqueness, sequential persisted full SHA256, 64 MiB transfers, commit/cancel races, device expiry, CAS, quotas, tombstones and cleanup/read leases.

Deployment is restricted to the authorized FlickDock account. wrangler.jsonc contains the dedicated non-secret D1 database ID and private bucket name. No source contains provider credentials. Provision AUTH_PEPPER (random >=32 chars) using wrangler secret put with stdin, apply migrations to the dedicated DB, then deploy. Never rotate AUTH_PEPPER after users exist without a deliberate account migration. This service does not enable Workers Paid or R2 public access. Limits: 64 MiB/file, 8 GiB globally shared, daily request and monthly R2 API budgets. Free Workers CPU must be verified on the actual provider; local latency does not establish CPU headroom. The initial deployed probe transferred 8 MiB in separate 1 MiB chunks with complete SHA256 validation.

EMAIL_PROVIDER remains disabled. For free optional email binding, configure a verified sender domain via Resend, EMAIL_FROM, RESEND_API_KEY and random EMAIL_CODE_SECRET. For Cloudflare arbitrary-recipient Email Sending, the operator must first authorize/onboard the paid capability and sender domain; then add the EMAIL binding. No fake email success, test-recipient mail or registration verification.

The application sends derived password-equivalent auth material only over TLS; never log request bodies, authorization headers, codes, content or provider responses. Observability is disabled in config. Hourly cleanup is required and must be monitored. There is no advertised password-recovery/account-delete API in v1.
