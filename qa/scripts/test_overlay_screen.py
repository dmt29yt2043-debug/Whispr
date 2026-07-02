"""TC_OVL_* — overlay screen-pick math for multi-monitor setups.

Regression for: "equalizer shows on the neighbouring monitor". The
CG→Cocoa Y-flip must use the PRIMARY screen height (screens()[0]) —
the old code used mainScreen() (screen with keyboard focus), which has
a different height whenever the user works on a secondary display,
landing the point outside every screen (→ mouse fallback → wrong
monitor) or inside the wrong one.
"""
from _harness import case, run_all

from overlay import _pick_screen_for_cg_point

# Real-world geometry: MacBook Air (primary, 1470×956 pt) with a 4K
# monitor (3840×2160 pt) arranged ABOVE it in Displays settings.
# Cocoa frames (bottom-left origin at primary's bottom-left):
_MACBOOK = (0.0, 0.0, 1470.0, 956.0)
_MONITOR_ABOVE = (0.0, 956.0, 3840.0, 2160.0)
_PRIMARY_H = 956.0


@case("TC_OVL_PICK_PRIMARY", "overlay",
      "window on the primary MacBook screen → primary picked")
def test_pick_primary():
    # Window centered at CG (700, 500) — middle of the MacBook screen.
    idx = _pick_screen_for_cg_point(700.0, 500.0, [_MACBOOK, _MONITOR_ABOVE], _PRIMARY_H)
    assert idx == 0, f"expected primary (0), got {idx}"


@case("TC_OVL_PICK_SECONDARY_ABOVE", "overlay",
      "window near the top of a 4K monitor above the MacBook → monitor picked (old code returned None)")
def test_pick_secondary_above():
    # Window near the top of the monitor: CG y is deeply negative
    # (monitor above primary spans CG y in [-2160, 0]).
    # Old formula with mainScreen()==monitor (h=2160) computed
    # cocoa_y = 2160 + 1900 = 4060 → outside all screens → None →
    # mouse-position fallback → overlay on the wrong monitor.
    idx = _pick_screen_for_cg_point(1920.0, -1900.0, [_MACBOOK, _MONITOR_ABOVE], _PRIMARY_H)
    assert idx == 1, f"expected monitor (1), got {idx}"


@case("TC_OVL_PICK_SIDE_BY_SIDE", "overlay",
      "side-by-side arrangement: window on the right monitor → right monitor picked")
def test_pick_side_by_side():
    # External QHD to the RIGHT of the MacBook, tops aligned.
    macbook = (0.0, 0.0, 1470.0, 956.0)
    external = (1470.0, 956.0 - 1440.0, 2560.0, 1440.0)  # cocoa y0 = -484
    # Window centered on the external: CG (2750, 720)
    idx = _pick_screen_for_cg_point(2750.0, 720.0, [macbook, external], 956.0)
    assert idx == 1, f"expected external (1), got {idx}"


@case("TC_OVL_PICK_NOWHERE", "overlay",
      "point outside every screen → None (caller falls back to mouse)")
def test_pick_nowhere():
    idx = _pick_screen_for_cg_point(99999.0, 99999.0, [_MACBOOK, _MONITOR_ABOVE], _PRIMARY_H)
    assert idx is None


if __name__ == "__main__":
    run_all("test_overlay_screen")
