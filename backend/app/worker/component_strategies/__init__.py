"""Component interaction strategies — split into focused modules.

Re-exports public API for backward compatibility. All existing imports
of `from app.worker.component_strategies import X` continue to work.
"""

from app.worker.component_strategies.autocomplete import fill_autocomplete
from app.worker.component_strategies.date_picker import pick_date
from app.worker.component_strategies.dropdown import select_dropdown
from app.worker.component_strategies.file_upload import upload_file
from app.worker.component_strategies.modal import interact_in_modal

__all__ = [
    "fill_autocomplete",
    "interact_in_modal",
    "pick_date",
    "select_dropdown",
    "upload_file",
]
