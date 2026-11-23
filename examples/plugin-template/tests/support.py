"""Template-specific synthetic layouts over the public offline test helpers."""

from moyu_harmony_pilot.testing import FakeClickTarget, OfflineTestCase, make_layout

from harmony_pilot_example.toggle import DEMO_BUNDLE, TOGGLE_ID

OTHER_BUTTON_ID = "other_button"
TOGGLE_TARGET = FakeClickTarget(TOGGLE_ID, DEMO_BUNDLE)

__all__ = ["OTHER_BUTTON_ID", "TOGGLE_TARGET", "OfflineTestCase", "toggle_layout"]


def toggle_layout(enabled=False, *, sensitive=False):
    controls = [{
        "id": TOGGLE_ID, "type": "Toggle", "text": "Demo toggle",
        "checkable": "true", "checked": str(enabled).lower(),
    }, {"id": OTHER_BUTTON_ID, "text": "Other action"}]
    if sensitive:
        controls.append({"type": "Text", "clickable": "false", "text": "Synthetic captcha"})
    return make_layout(*controls, bundle_name=DEMO_BUNDLE)
