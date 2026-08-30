# API package

# Dashboard position-preview and pending-rewind routes decorate the already-
# registered KoSync admin blueprint. Import them here so the decorators run
# before web_server registers that blueprint with Flask during dependency setup.
from src.api import reading_position_preview_routes as _reading_position_preview_routes  # noqa: F401,E402
from src.api import pending_rewind_routes as _pending_rewind_routes  # noqa: F401,E402
