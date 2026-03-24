"""English locale constants for component interaction strategies.

Centralizes all hardcoded English strings (ARIA labels, placeholder
text, navigation button labels) so they can be overridden for i18n
in the future.
"""

# Date picker
NEXT_MONTH_LABEL = "Next month"
PREV_MONTH_LABEL = "Previous month"
SELECT_DATE_PLACEHOLDER = "Select date"
CHOOSE_DATE_PLACEHOLDER = "Choose date"
PICK_DATE_PLACEHOLDER = "Pick date"

# Date input format placeholders
DATE_FORMAT_MDY = "mm/dd/yyyy"
DATE_FORMAT_DMY = "dd/mm/yyyy"

PLACEHOLDER_PATTERNS = (
    SELECT_DATE_PLACEHOLDER.lower(),
    CHOOSE_DATE_PLACEHOLDER.lower(),
    PICK_DATE_PLACEHOLDER.lower(),
    DATE_FORMAT_MDY,
    DATE_FORMAT_DMY,
)

# Modal / Dialog
CLOSE_BUTTON_LABEL = "Close"
