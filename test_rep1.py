"""Tests for Rep #1. Run with:  .venv\\Scripts\\python -m pytest -q"""
import pytest

from rep1_finger_zone import (CLEAN_REWARD, PINKY_PENALTY, RING_PENALTY,
                              classify_finger_steal)

# Knuckle spacing is 0.05, so one "finger gap" = 0.05 and half a gap = 0.025.
KNUCKLES = {5: 0.40, 9: 0.45, 13: 0.50, 17: 0.55}


def make_hand(index=0.40, middle=0.45, ring=0.50, pinky=0.55, flip=False):
    """Build 21 fake landmarks. Tips default to sitting on their own knuckle."""
    pts = [[0.5, 0.5, 0.0] for _ in range(21)]
    for i, x in KNUCKLES.items():
        pts[i][0] = x
    pts[8][0], pts[12][0], pts[16][0], pts[20][0] = index, middle, ring, pinky
    if flip:  # mirror the whole hand left-to-right
        for p in pts:
            p[0] = 1.0 - p[0]
    return pts


def test_home_position_is_clean():
    r = classify_finger_steal("Right", make_hand())
    assert r == {"steal": False, "thief_finger": "", "zone": "home", "purity_delta": CLEAN_REWARD}


def test_index_into_ring_column():
    r = classify_finger_steal("Right", make_hand(index=0.50))
    assert (r["steal"], r["thief_finger"], r["zone"]) == (True, "Index", "ring")
    assert r["purity_delta"] == RING_PENALTY


def test_middle_into_pinky_column():
    r = classify_finger_steal("Left", make_hand(middle=0.56))
    assert (r["steal"], r["thief_finger"], r["zone"]) == (True, "Middle", "pinky")
    assert r["purity_delta"] == PINKY_PENALTY


def test_middle_halfway_to_ring_is_not_yet_a_steal():
    # Ring zone starts at 0.50 - 0.025 = 0.475, so 0.47 is still home.
    assert not classify_finger_steal("Right", make_hand(middle=0.47))["steal"]
    assert classify_finger_steal("Right", make_hand(middle=0.48))["steal"]


def test_furthest_finger_is_the_thief():
    r = classify_finger_steal("Right", make_hand(index=0.51, middle=0.53))
    assert r["thief_finger"] == "Middle"


def test_mirrored_hand_gives_same_answer():
    r = classify_finger_steal("Left", make_hand(index=0.50, flip=True))
    assert (r["steal"], r["thief_finger"], r["zone"]) == (True, "Index", "ring")


def test_ring_and_pinky_moving_does_not_count():
    assert not classify_finger_steal("Right", make_hand(ring=0.58, pinky=0.65))["steal"]


def test_edge_on_hand_is_ignored():
    hand = make_hand()
    hand[17][0] = hand[5][0]
    assert not classify_finger_steal("Right", hand)["steal"]


def test_bad_inputs_raise():
    with pytest.raises(ValueError):
        classify_finger_steal("Middle", make_hand())
    with pytest.raises(ValueError):
        classify_finger_steal("Right", make_hand()[:20])
