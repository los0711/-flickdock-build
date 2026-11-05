"""Public service location; deployment supplies a verified HTTPS origin."""
DEFAULT_CLOUD_URL = 'https://flickdock.cc.cd'
CLOUD_ORIGINS=(DEFAULT_CLOUD_URL,)
# Credential-store identity belongs to the backend service, not its public
# network entrance. Changing routing must not change which secure token it owns.
SESSION_REALM = 'https://flickdock-cloud.roger000416.workers.dev'
API_SCHEMA_VERSION = 1
