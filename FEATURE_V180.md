# FlickDock 1.8.0 delivery state

Windows settings rounded+smaller X; inline update status/explicit install; real missing-startup reconciliation; username/password account UI; email binding hidden per user choice; devices/revocation; account-isolated persistent cloud journal and multipart sync; macOS platform/build/update adapters.

Cloudflare authorized through official device flow. User chose FREE ONLY and enabled R2. Dedicated D1 flickdock-cloud 2f2c1b7e-9206-40fb-bfc5-e399e7fcc6b9 and private Standard R2 flickdock-private created. Public R2 URLs/domains disabled. Worker deployed https://flickdock-cloud.roger000416.workers.dev, AUTH_PEPPER provisioned through stdin, no secret persisted/logged. Default 8 GiB global storage,no separate64MiB file cap,1 MiB chunks,5000 requests/day. Real deployed 8 MiB full SHA256 upload/download and revocation passed; actual Qt client HTTPS 2560000 bytes/native text/two device sessions also passed.

No paid subscription activated. No real email sent. Private GitHub cloud builds are authorized and running for Apple Silicon and Intel; real artifact verification is still pending. Do not claim usable .app/.dmg until native jobs finish and frozen checks pass.

User constraint no desktop demos: tests offscreen, disposable APPDATA, no real startup/hotkey/keychain mutations or installer execution. Maintain private source/public release-only separation. Prior public index remains1.7.0 until a verified new Windows release is published. Full regressions and source/frozen/release QA documented in outputs.
