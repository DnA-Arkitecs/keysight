"""Rep #1: Finger Zone. Did the Index or Middle finger steal an outer key?

Touch typing gives every finger a home column. Ring and Pinky own the outer
columns. A "steal" is when the Index or Middle fingertip drifts sideways into
the Ring or Pinky column.

MediaPipe gives 21 points per hand. We only need these (X is left-right):
    5 = Index knuckle     8 = Index tip     12 = Middle tip
    17 = Pinky knuckle   16 = Ring tip      20 = Pinky tip
"""

# Purity points per video frame. Tweak these three numbers for Rep #1.
CLEAN_REWARD = 0.1    # hand stayed home: purity creeps back up
RING_PENALTY = -0.3   # a thief stepped into the Ring column
PINKY_PENALTY = -0.6  # a thief stepped all the way into the Pinky column


def classify_finger_steal(hand_label, landmarks):
    """Return {steal, thief_finger, zone, purity_delta} for one hand."""
    if hand_label not in ("Left", "Right"):
        raise ValueError(f"hand_label must be 'Left' or 'Right', got {hand_label!r}")
    if len(landmarks) != 21:
        raise ValueError(f"need 21 landmarks, got {len(landmarks)}")
    home = {"steal": False, "thief_finger": "", "zone": "home", "purity_delta": CLEAN_REWARD}

    # Step 1: which way is "outward"? Knuckle line from Index to Pinky points there.
    # Using knuckles (not the label) works for either hand, mirrored or not.
    width = landmarks[17][0] - landmarks[5][0]
    if abs(width) < 0.01:
        return home  # hand is edge-on to the camera, so X tells us nothing
    out = 1 if width > 0 else -1
    reach = lambda i: landmarks[i][0] * out  # bigger number = further outward

    # Step 2: a zone starts half a finger-gap before the Ring / Pinky fingertip.
    half_gap = abs(width) / 6
    pinky_zone = reach(20) - half_gap
    ring_zone = reach(16) - half_gap

    # Step 3: the thief is whichever of Index (8) / Middle (12) reaches furthest out.
    thief, thief_reach = max((("Index", reach(8)), ("Middle", reach(12))), key=lambda t: t[1])
    if thief_reach >= pinky_zone:
        return {"steal": True, "thief_finger": thief, "zone": "pinky", "purity_delta": PINKY_PENALTY}
    if thief_reach >= ring_zone:
        return {"steal": True, "thief_finger": thief, "zone": "ring", "purity_delta": RING_PENALTY}
    return home
