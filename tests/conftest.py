"""Keep the test process independent from a developer's local .env settings."""

import os


# Most API fixtures intentionally construct Settings(auth_enabled=False).  The
# local application may enable auth for browser testing, but that must not make
# unrelated tests depend on the developer's database or token state.
os.environ["AUTH_ENABLED"] = "false"
os.environ["AUTH_REGISTRATION_ENABLED"] = "false"
