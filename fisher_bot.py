"""
PW-Fisher — Automated 2D Fishing Minigame Bot
==============================================
Uses screen capture (mss), color detection (OpenCV/HSV), and direct keyboard
input (pydirectinput) to automate a fishing minigame on Windows.

Install:
    pip install mss opencv-python numpy pydirectinput keyboard

Usage:
    python fisher_bot.py

Controls:
    Q            — stop the bot safely (works from any window)
    Mouse to 0,0 — emergency failsafe stop (pydirectinput FAILSAFE)
"""

import ctypes
import random
import time
import sys
import traceback

import numpy as np
import cv2
import mss
import pydirectinput
import keyboard

# ---------------------------------------------------------------------------
#  DPI awareness — ensures mss pixel coords match pydirectinput coords
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = True

# ===========================================================================
#  CONFIGURATION — tweak these values to match your game
# ===========================================================================
CONFIG = {
    # ---- Screen regions {"left", "top", "width", "height"} ----------------
    "bobber_region":    {"left": 1000, "top": 650,  "width": 200,  "height": 165},
    "bite_region":      {"left": 1620, "top": 825,  "width": 190,  "height": 180},
    "bar_region":       {"left": 420,  "top": 170,  "width": 1040, "height": 80},
    "progress_region":  {"left": 600,  "top": 250,  "width": 700,  "height": 75},
    "take_fish_region": {"left": 785,  "top": 815,  "width": 315,  "height": 85},

    # ---- HSV colour ranges [H, S, V]  (H 0-179, S/V 0-255) ---------------
    # Bobber icon  ~#6A777C  (desaturated blue-gray,  HSV ≈  98, 37, 124)
    "bobber_hsv_low":   [80,  8,  75],
    "bobber_hsv_high":  [120, 95, 175],
    "bobber_min_area":  35,

    # Bite / catch green icon  ~#1ED22A  (bright green, HSV ≈ 62, 219, 210)
    "bite_hsv_low":   [38, 120, 120],
    "bite_hsv_high":  [90, 255, 255],
    "bite_min_area":  120,

    # Fish normal (dark blue)  ~#0F3560  (HSV ≈ 106, 215, 96)
    "fish_dark_hsv_low":   [85,  80,  15],
    "fish_dark_hsv_high":  [130, 255, 155],

    # Fish green (near cube)  ~#03E813  (HSV ≈ 62, 252, 232)
    "fish_green_hsv_low":  [45, 190, 120],
    "fish_green_hsv_high": [85, 255, 255],

    # Fish fleeing red  ~#8F0509  (HSV ≈ 0/179, 246, 143) — wraps around 0
    "fish_red_hsv_low_a":  [0,   140, 55],
    "fish_red_hsv_high_a": [15,  255, 255],
    "fish_red_hsv_low_b":  [164, 140, 55],
    "fish_red_hsv_high_b": [179, 255, 255],

    "fish_min_area": 20,

    # Cube border  ~#9AFF66  (lime-green, HSV ≈ 50, 153, 255)
    "cube_hsv_low":   [25,  45,  190],
    "cube_hsv_high":  [70,  215, 255],
    "cube_min_area":  30,

    # Bar background  ~#2367B3  (HSV ≈ 106, 205, 179)
    "bar_bg_hsv_low":   [85,  110, 100],
    "bar_bg_hsv_high":  [125, 255, 240],
    "bar_min_area":     400,

    # Bar white border (distinguishes the actual bar from blue game world)
    "bar_border_hsv_low":    [0,   0, 200],
    "bar_border_hsv_high":   [179, 40, 255],
    "bar_border_min_pixels": 40,
    "bar_border_edge_px":    10,     # how many pixels from the edge to check

    # "Take Fish" button — same bright green as bite icon
    "take_fish_hsv_low":  [38, 120, 120],
    "take_fish_hsv_high": [90, 255, 255],
    "take_fish_min_area": 100,

    # ---- Steering ---------------------------------------------------------
    "dead_zone":  5,          # px tolerance before steering
    "key_left":   "a",
    "key_right":  "d",
    "key_hook":   "space",

    # ---- Timing (seconds) -------------------------------------------------
    "loop_delay":        0.016,   # ≈60 FPS target
    "cast_delay":        1.5,
    "post_hook_delay":   0.4,
    "post_catch_delay":  1.0,
    "post_take_delay":   1.0,
    "bar_grace_period":  1.5,     # time for bar to appear after hooking
    "bar_gone_frames":   12,      # consecutive frames w/o bar → fish escaped
    "catch_confirm_frames": 1,    # react on first green-icon frame (instant)
    "catch_min_elapsed":    4.0,  # ignore green catch icon for this long after minigame starts
    "catch_verify_delay":   1.2,  # seconds to wait after space before verifying catch
    "verify_delay":      0.4,     # seconds to wait before verifying an action succeeded
    "max_retries":       3,       # how many times to retry a failed action
    "startup_delay":     3,

    # ---- Progress bar -----------------------------------------------------
    "progress_fill_thresh": 0.80,

    # ---- Random delay before actions (seconds) ----------------------------
    "action_delay_min": 0.08,
    "action_delay_max": 0.35,

    # ---- Debug / UI -------------------------------------------------------
    "debug":     True,
    "exit_key":  "q",
}


# ===========================================================================
#  STATES
# ===========================================================================
class State:
    IDLE             = "IDLE"
    CASTING          = "CASTING"
    WAITING_FOR_BITE = "WAITING_FOR_BITE"
    HOOKED_MINIGAME  = "HOOKED_MINIGAME"
    TAKE_FISH        = "TAKE_FISH"


# ===========================================================================
#  KEY TRACKING
# ===========================================================================
_keys_held = {"a": False, "d": False}


def press_left(cfg: dict) -> None:
    """Hold left, release right if held."""
    if _keys_held["d"]:
        pydirectinput.keyUp(cfg["key_right"])
        _keys_held["d"] = False
    if not _keys_held["a"]:
        pydirectinput.keyDown(cfg["key_left"])
        _keys_held["a"] = True


def press_right(cfg: dict) -> None:
    """Hold right, release left if held."""
    if _keys_held["a"]:
        pydirectinput.keyUp(cfg["key_left"])
        _keys_held["a"] = False
    if not _keys_held["d"]:
        pydirectinput.keyDown(cfg["key_right"])
        _keys_held["d"] = True


def random_action_delay(cfg: dict) -> float:
    """Sleep a random duration before an action. Returns the delay used."""
    delay = random.uniform(cfg["action_delay_min"], cfg["action_delay_max"])
    time.sleep(delay)
    return delay


def release_keys(cfg: dict) -> None:
    """Release both movement keys."""
    if _keys_held["a"]:
        pydirectinput.keyUp(cfg["key_left"])
        _keys_held["a"] = False
    if _keys_held["d"]:
        pydirectinput.keyUp(cfg["key_right"])
        _keys_held["d"] = False


# ===========================================================================
#  SCREEN CAPTURE
# ===========================================================================
def screenshot_region(sct, region: dict) -> np.ndarray:
    """Grab a screen region via mss and return a BGR numpy array."""
    raw = sct.grab(region)
    return np.array(raw, dtype=np.uint8)[:, :, :3]  # BGRA → BGR


# ===========================================================================
#  GENERIC VISION HELPERS
# ===========================================================================
def hsv_mask(img_bgr: np.ndarray, low: list, high: list) -> np.ndarray:
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array(low, np.uint8), np.array(high, np.uint8))


def px_count(mask: np.ndarray) -> int:
    return int(cv2.countNonZero(mask))


def largest_blob(mask: np.ndarray):
    """Return (center_xy, bounding_rect) of the largest contour, or (None, None)."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None
    c = max(contours, key=cv2.contourArea)
    M = cv2.moments(c)
    if M["m00"] == 0:
        return None, None
    cx = int(M["m10"] / M["m00"])
    cy = int(M["m01"] / M["m00"])
    return (cx, cy), cv2.boundingRect(c)


# ===========================================================================
#  DETECTION FUNCTIONS
# ===========================================================================
def detect_bobber(img: np.ndarray, cfg: dict):
    """Detect bobber icon by colour + morphology.
    Returns (center_xy | None, mask).
    """
    mask = hsv_mask(img, cfg["bobber_hsv_low"], cfg["bobber_hsv_high"])
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    if px_count(mask) < cfg["bobber_min_area"]:
        return None, mask
    center, _ = largest_blob(mask)
    return center, mask


def detect_green_icon(img: np.ndarray, cfg: dict):
    """Detect bright-green bite/catch icon.
    Returns (detected: bool, mask, pixel_count).
    """
    mask = hsv_mask(img, cfg["bite_hsv_low"], cfg["bite_hsv_high"])
    cnt = px_count(mask)
    return cnt >= cfg["bite_min_area"], mask, cnt


def detect_bar_visible(img: np.ndarray, cfg: dict):
    """Check whether the minigame bar is on-screen.
    Requires BOTH blue background pixels AND white border pixels at
    the top/bottom edges.  The game world may contain blue, but it
    won't have the bar's white border in the exact same region.
    """
    bg_mask = hsv_mask(img, cfg["bar_bg_hsv_low"], cfg["bar_bg_hsv_high"])
    if px_count(bg_mask) < cfg["bar_min_area"]:
        return False, bg_mask

    border_mask = hsv_mask(
        img, cfg["bar_border_hsv_low"], cfg["bar_border_hsv_high"]
    )
    e = cfg["bar_border_edge_px"]
    h = img.shape[0]
    top_px    = px_count(border_mask[:e, :])
    bottom_px = px_count(border_mask[h - e:, :])
    border_ok = (top_px + bottom_px) >= cfg["bar_border_min_pixels"]

    return border_ok, bg_mask


def detect_cube_in_bar(img: np.ndarray, cfg: dict):
    """Detect the player-controlled cube via its lime-green border.
    Returns (center_xy | None, bbox | None, mask).
    """
    mask = hsv_mask(img, cfg["cube_hsv_low"], cfg["cube_hsv_high"])
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    if px_count(mask) < cfg["cube_min_area"]:
        return None, None, mask
    center, bbox = largest_blob(mask)
    return center, bbox, mask


def detect_fish_in_bar(img: np.ndarray, cfg: dict, cube_mask: np.ndarray = None):
    """Detect the fish in the bar.  Handles dark-blue, green, and red states.
    Subtracts dilated cube-border pixels to avoid confusion with the cube.
    Returns (center_xy | None, combined_mask).
    """
    m_dark  = hsv_mask(img, cfg["fish_dark_hsv_low"],  cfg["fish_dark_hsv_high"])
    m_green = hsv_mask(img, cfg["fish_green_hsv_low"], cfg["fish_green_hsv_high"])
    m_red_a = hsv_mask(img, cfg["fish_red_hsv_low_a"], cfg["fish_red_hsv_high_a"])
    m_red_b = hsv_mask(img, cfg["fish_red_hsv_low_b"], cfg["fish_red_hsv_high_b"])

    combined = m_dark | m_green | m_red_a | m_red_b

    # Subtract cube-border pixels (dilated) so we don't track the cube as fish
    if cube_mask is not None and px_count(cube_mask) > 0:
        dilated = cv2.dilate(cube_mask, np.ones((7, 7), np.uint8))
        combined = combined & cv2.bitwise_not(dilated)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, k)

    if px_count(combined) < cfg["fish_min_area"]:
        return None, combined
    center, _ = largest_blob(combined)
    return center, combined


def detect_take_fish_button(img: np.ndarray, cfg: dict):
    """Detect the green 'Take Fish' button.
    Returns (detected: bool, center_xy | None, mask).
    """
    mask = hsv_mask(img, cfg["take_fish_hsv_low"], cfg["take_fish_hsv_high"])
    cnt = px_count(mask)
    if cnt < cfg["take_fish_min_area"]:
        return False, None, mask
    center, _ = largest_blob(mask)
    return True, center, mask


def detect_progress_full(img: np.ndarray, cfg: dict):
    """Estimate progress-bar fill ratio (0.0 – 1.0).
    Counts non-background, non-dark pixels as 'fill'.
    Returns (ratio, fill_mask).
    """
    bg   = hsv_mask(img, cfg["bar_bg_hsv_low"], cfg["bar_bg_hsv_high"])
    dark = hsv_mask(img, [0, 0, 0], [179, 255, 50])
    fill = cv2.bitwise_not(bg | dark)
    total = fill.shape[0] * fill.shape[1]
    if total == 0:
        return 0.0, fill
    return px_count(fill) / total, fill


# ===========================================================================
#  DEBUG VISUALISATION
# ===========================================================================
def _paste(canvas, img, x, y, max_w, max_h):
    """Resize *img* to fit in max_w×max_h and paste onto *canvas* at (x, y)."""
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    resized = cv2.resize(img, (int(w * scale), int(h * scale)))
    if len(resized.shape) == 2:
        resized = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
    rh, rw = resized.shape[:2]
    ye = min(y + rh, canvas.shape[0])
    xe = min(x + rw, canvas.shape[1])
    canvas[y:ye, x:xe] = resized[:ye - y, :xe - x]
    return rw, rh


def build_debug_frame(
    state, fps, fish_caught,
    bobber_img, bobber_mask, bobber_pos,
    bite_img, bite_mask, bite_detected,
    bar_img, bar_visible,
    fish_pos, fish_mask,
    cube_pos, cube_bbox, cube_mask,
    progress_img, progress_ratio,
    take_fish_img, take_fish_mask, take_fish_detected,
    cfg,
):
    W, H = 1120, 600
    canvas = np.zeros((H, W, 3), dtype=np.uint8)

    # ---- header bar ----
    state_colours = {
        State.IDLE:             (200, 200, 200),
        State.CASTING:          (0,   200, 255),
        State.WAITING_FOR_BITE: (255, 200, 0),
        State.HOOKED_MINIGAME:  (0,   255, 100),
        State.TAKE_FISH:        (0,   255, 200),
    }
    sc = state_colours.get(state, (255, 255, 255))
    cv2.rectangle(canvas, (0, 0), (W, 44), (25, 25, 25), -1)
    cv2.putText(canvas, f"State: {state}", (10, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.82, sc, 2)
    cv2.putText(canvas, f"FPS: {fps:.0f}", (430, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (170, 170, 170), 1)
    cv2.putText(canvas, f"Fish: {fish_caught}", (560, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 255), 1)

    keys_txt = ""
    if _keys_held.get("a"):
        keys_txt += "[A] "
    if _keys_held.get("d"):
        keys_txt += "[D] "
    if keys_txt:
        cv2.putText(canvas, keys_txt, (700, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
    cv2.putText(canvas, "Q = quit", (1000, 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 80, 80), 1)

    # ---- row 1: bobber + mask | bite + mask ----
    r1 = 58
    if bobber_img is not None:
        cv2.putText(canvas, "Bobber ROI", (10, r1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (130, 130, 130), 1)
        vis = bobber_img.copy()
        if bobber_pos:
            cv2.circle(vis, bobber_pos, 8, (0, 0, 255), 2)
            cv2.drawMarker(vis, bobber_pos, (0, 255, 255),
                           cv2.MARKER_CROSS, 16, 2)
        _paste(canvas, vis, 10, r1, 200, 155)
        if bobber_mask is not None:
            _paste(canvas, bobber_mask, 218, r1, 140, 155)

    if bite_img is not None:
        lbl = "Bite ROI  ** DETECTED **" if bite_detected else "Bite ROI"
        lc  = (0, 255, 0) if bite_detected else (130, 130, 130)
        cv2.putText(canvas, lbl, (390, r1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, lc, 1)
        vis = bite_img.copy()
        bdr = (0, 255, 0) if bite_detected else (50, 50, 50)
        cv2.rectangle(vis, (0, 0),
                      (vis.shape[1] - 1, vis.shape[0] - 1), bdr, 2)
        _paste(canvas, vis, 390, r1, 190, 155)
        if bite_mask is not None:
            _paste(canvas, bite_mask, 590, r1, 140, 155)

    # ---- info panel (right side) ----
    ix, iy = 760, r1 + 12
    if fish_pos:
        cv2.putText(canvas, f"Fish  X: {fish_pos[0]}",
                    (ix, iy), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 80, 255), 1)
    if cube_pos:
        cv2.putText(canvas, f"Cube  X: {cube_pos[0]}",
                    (ix, iy + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 255, 80), 1)
    if fish_pos and cube_pos:
        d = fish_pos[0] - cube_pos[0]
        tag = "LEFT" if d < 0 else ("RIGHT" if d > 0 else "CENTER")
        cv2.putText(canvas, f"Diff: {d:+d}  {tag}",
                    (ix, iy + 52), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 255, 255), 1)
    cv2.putText(canvas, f"Dead-zone: +/-{cfg['dead_zone']}px",
                (ix, iy + 82), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (100, 100, 100), 1)

    # ---- row 2: bar with annotations ----
    r2 = 235
    if bar_img is not None and bar_visible:
        cv2.putText(canvas, "Minigame Bar", (10, r2 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (130, 130, 130), 1)
        vis = bar_img.copy()
        if cube_pos and cube_bbox:
            bx, by, bw, bh = cube_bbox
            cv2.rectangle(vis, (bx, by), (bx + bw, by + bh),
                          (102, 255, 154), 2)
            cv2.circle(vis, cube_pos, 4, (102, 255, 154), -1)
        if fish_pos:
            cv2.circle(vis, fish_pos, 6, (0, 0, 255), -1)
            cv2.drawMarker(vis, fish_pos, (0, 0, 255),
                           cv2.MARKER_TILTED_CROSS, 14, 2)
        if fish_pos and cube_pos:
            cv2.line(vis, fish_pos, cube_pos, (0, 255, 255), 1)
        _paste(canvas, vis, 10, r2, W - 20, 100)

        # masks row
        r2m = r2 + 108
        if fish_mask is not None:
            cv2.putText(canvas, "fish mask", (10, r2m - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.34, (90, 90, 90), 1)
            fish_vis = cv2.applyColorMap(fish_mask, cv2.COLORMAP_HOT)
            _paste(canvas, fish_vis, 10, r2m, 360, 42)
        if cube_mask is not None:
            cv2.putText(canvas, "cube mask", (390, r2m - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.34, (90, 90, 90), 1)
            cube_vis = cv2.applyColorMap(cube_mask, cv2.COLORMAP_SPRING)
            _paste(canvas, cube_vis, 390, r2m, 360, 42)

    # ---- row 3: progress bar ----
    r3 = 420
    if progress_img is not None:
        cv2.putText(canvas, "Progress", (10, r3 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (130, 130, 130), 1)
        _paste(canvas, progress_img, 10, r3, 520, 55)
        # graphical bar
        bx, by2, bw, bh2 = 10, r3 + 60, 520, 20
        cv2.rectangle(canvas, (bx, by2), (bx + bw, by2 + bh2),
                      (55, 55, 55), -1)
        fw = int(bw * progress_ratio)
        fc = (0, 255, 0) if progress_ratio >= cfg["progress_fill_thresh"] else (0, 180, 255)
        if fw > 0:
            cv2.rectangle(canvas, (bx, by2), (bx + fw, by2 + bh2), fc, -1)
        cv2.rectangle(canvas, (bx, by2), (bx + bw, by2 + bh2),
                      (100, 100, 100), 1)
        cv2.putText(canvas, f"{progress_ratio * 100:.0f}%",
                    (bx + bw + 8, by2 + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1)

    # ---- row 4: take fish button ----
    r4 = 510
    if take_fish_img is not None:
        lbl = "Take Fish  ** DETECTED **" if take_fish_detected else "Take Fish ROI"
        lc  = (0, 255, 0) if take_fish_detected else (130, 130, 130)
        cv2.putText(canvas, lbl, (560, r4 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, lc, 1)
        vis = take_fish_img.copy()
        bdr = (0, 255, 0) if take_fish_detected else (50, 50, 50)
        cv2.rectangle(vis, (0, 0),
                      (vis.shape[1] - 1, vis.shape[0] - 1), bdr, 2)
        _paste(canvas, vis, 560, r4, 300, 70)
        if take_fish_mask is not None:
            _paste(canvas, take_fish_mask, 870, r4, 200, 70)

    # ---- legend at bottom ----
    ly = H - 16
    cv2.putText(canvas,
                "RED circle = fish   GREEN rect = cube   YELLOW line = diff",
                (10, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (80, 80, 80), 1)

    return canvas


# ===========================================================================
#  MAIN LOOP
# ===========================================================================
def main():
    cfg = CONFIG
    state = State.IDLE
    sct = mss.mss()

    cast_time        = 0.0
    bite_wait_start  = 0.0
    minigame_start   = 0.0
    take_fish_start      = 0.0
    bar_gone_count       = 0
    catch_confirm_count  = 0
    fish_caught          = 0
    frame_times: list    = []

    # ---- startup countdown ----
    print("=" * 54)
    print("  PW-Fisher Bot")
    print("  Q = stop (any window)  |  mouse to (0,0) = failsafe")
    print("=" * 54)
    for i in range(cfg["startup_delay"], 0, -1):
        print(f"  Starting in {i} …  (switch to game window now)")
        time.sleep(1)
    print("  GO!\n")

    try:
        while True:
            t0 = time.time()

            # ---- exit check ----
            if keyboard.is_pressed(cfg["exit_key"]):
                print("[EXIT] Q pressed — stopping.")
                break

            # ---- screen captures (only the regions we need) ----
            bite_img = screenshot_region(sct, cfg["bite_region"])

            bobber_img     = None
            bar_img        = None
            progress_img   = None
            take_fish_img  = None

            if state == State.IDLE:
                bobber_img = screenshot_region(sct, cfg["bobber_region"])
            if state == State.HOOKED_MINIGAME:
                bar_img      = screenshot_region(sct, cfg["bar_region"])
                progress_img = screenshot_region(sct, cfg["progress_region"])
            if state == State.TAKE_FISH:
                take_fish_img = screenshot_region(sct, cfg["take_fish_region"])

            # ---- detection results (defaults) ----
            bobber_pos   = None
            bobber_mask  = None
            bite_det     = False
            bite_mask    = None
            bite_cnt     = 0
            fish_pos     = None
            fish_mask    = None
            cube_pos     = None
            cube_bbox    = None
            cube_mask    = None
            bar_vis         = False
            prog_ratio      = 0.0
            take_fish_det   = False
            take_fish_mask  = None
            take_fish_pos   = None

            bite_det, bite_mask, bite_cnt = detect_green_icon(bite_img, cfg)

            # ===========================================================
            #  STATE MACHINE
            # ===========================================================

            if state == State.IDLE:
                bobber_pos, bobber_mask = detect_bobber(bobber_img, cfg)
                if bobber_pos is not None:
                    sx = cfg["bobber_region"]["left"] + bobber_pos[0]
                    sy = cfg["bobber_region"]["top"]  + bobber_pos[1]

                    for attempt in range(1, cfg["max_retries"] + 1):
                        d = random_action_delay(cfg)
                        print(f"[IDLE] Bobber at ({sx},{sy}) — clicking "
                              f"(attempt {attempt}/{cfg['max_retries']}, "
                              f"+{d:.2f}s)")
                        pydirectinput.click(x=sx, y=sy)
                        time.sleep(cfg["verify_delay"])

                        v_img = screenshot_region(sct, cfg["bobber_region"])
                        still, _ = detect_bobber(v_img, cfg)
                        if still is None:
                            print("[IDLE] OK — bobber gone, cast sent")
                            break
                        print("[IDLE] Bobber still visible — retrying")
                    else:
                        print("[IDLE] Bobber persisted after retries — "
                              "proceeding anyway")

                    cast_time = time.time()
                    state = State.CASTING

            elif state == State.CASTING:
                if time.time() - cast_time >= cfg["cast_delay"]:
                    print("[CAST] Bait in water — waiting for bite …")
                    bite_wait_start = time.time()
                    state = State.WAITING_FOR_BITE

            elif state == State.WAITING_FOR_BITE:
                if bite_det:
                    hooked = False
                    got_item = False
                    for attempt in range(1, cfg["max_retries"] + 1):
                        d = random_action_delay(cfg)
                        print(f"[BITE] Green icon ({bite_cnt} px) — hooking! "
                              f"(attempt {attempt}/{cfg['max_retries']}, "
                              f"+{d:.2f}s)")
                        pydirectinput.press(cfg["key_hook"])
                        time.sleep(cfg["post_hook_delay"])

                        v_img = screenshot_region(sct, cfg["bar_region"])
                        bar_up, _ = detect_bar_visible(v_img, cfg)
                        if bar_up:
                            print("[BITE] OK — bar appeared, minigame started")
                            hooked = True
                            break

                        tf_img = screenshot_region(
                            sct, cfg["take_fish_region"]
                        )
                        tf_det, _, _ = detect_take_fish_button(tf_img, cfg)
                        if tf_det:
                            print("[BITE] OK — item button appeared, "
                                  "going to TAKE_FISH")
                            got_item = True
                            break

                        print("[BITE] Neither bar nor item button "
                              "appeared — retrying")

                    if hooked:
                        minigame_start = time.time()
                        bar_gone_count = 0
                        catch_confirm_count = 0
                        state = State.HOOKED_MINIGAME
                    elif got_item:
                        fish_caught += 1
                        print(f"[CATCH] Item/fish #{fish_caught} received!")
                        take_fish_start = time.time()
                        state = State.TAKE_FISH
                        print("[TAKE] Waiting for Take Fish button …")
                    else:
                        print("[BITE] Bar never appeared — resetting to IDLE")
                        state = State.IDLE


            elif state == State.HOOKED_MINIGAME:
                mg_elapsed = time.time() - minigame_start

                if bar_img is not None:
                    bar_vis, _ = detect_bar_visible(bar_img, cfg)

                if bar_vis:
                    bar_gone_count = 0

                    # --- cube detection (first, so we can subtract from fish) ---
                    cube_pos, cube_bbox, cube_mask = detect_cube_in_bar(
                        bar_img, cfg
                    )

                    # --- fish detection (subtract cube mask) ---
                    fish_pos, fish_mask = detect_fish_in_bar(
                        bar_img, cfg, cube_mask=cube_mask
                    )

                    # --- steering logic ---
                    if fish_pos and cube_pos:
                        diff = fish_pos[0] - cube_pos[0]
                        if diff < -cfg["dead_zone"]:
                            press_left(cfg)
                        elif diff > cfg["dead_zone"]:
                            press_right(cfg)
                        else:
                            release_keys(cfg)
                    elif fish_pos and not cube_pos:
                        mid = bar_img.shape[1] // 2
                        if fish_pos[0] < mid - 60:
                            press_left(cfg)
                        elif fish_pos[0] > mid + 60:
                            press_right(cfg)
                    else:
                        release_keys(cfg)

                    # --- progress estimation ---
                    if progress_img is not None:
                        prog_ratio, _ = detect_progress_full(progress_img, cfg)

                    # --- catch-ready? (green icon reappears) ---
                    if bite_det and mg_elapsed > cfg["catch_min_elapsed"]:
                        catch_confirm_count += 1
                    else:
                        catch_confirm_count = 0

                    if catch_confirm_count >= cfg["catch_confirm_frames"]:
                        # INSTANT — no random delay for catching
                        release_keys(cfg)
                        pydirectinput.press(cfg["key_hook"])
                        print("[CATCH] Green icon — space pressed INSTANTLY")
                        catch_confirm_count = 0

                        # Two-pass verify: give the game enough time to
                        # transition out of the minigame bar.
                        time.sleep(cfg["catch_verify_delay"])
                        v1 = screenshot_region(sct, cfg["bar_region"])
                        bar_still, _ = detect_bar_visible(v1, cfg)

                        if bar_still:
                            # Second check — the game might still be
                            # animating the transition.
                            time.sleep(0.6)
                            v2 = screenshot_region(sct, cfg["bar_region"])
                            bar_still, _ = detect_bar_visible(v2, cfg)

                        if not bar_still:
                            fish_caught += 1
                            print(f"[CATCH] Fish #{fish_caught} caught!")
                            time.sleep(cfg["post_catch_delay"])
                            take_fish_start = time.time()
                            state = State.TAKE_FISH
                            print("[TAKE] Waiting for Take Fish button …")
                        else:
                            print("[CATCH] Bar still visible — fish escaped "
                                  "the catch! Resuming steering …")

                else:
                    # Bar not visible — give it grace period, then assume escape
                    if mg_elapsed > cfg["bar_grace_period"]:
                        bar_gone_count += 1
                        if bar_gone_count >= cfg["bar_gone_frames"]:
                            release_keys(cfg)
                            time.sleep(0.3)
                            tf_img = screenshot_region(
                                sct, cfg["take_fish_region"]
                            )
                            tf_det, _, _ = detect_take_fish_button(tf_img, cfg)
                            if tf_det:
                                fish_caught += 1
                                print(f"[CATCH] Fish #{fish_caught} caught! "
                                      "(bar cleared)")
                                take_fish_start = time.time()
                                state = State.TAKE_FISH
                                print("[TAKE] Waiting for Take Fish "
                                      "button …")
                            else:
                                print("[ESC] Bar gone — fish escaped")
                                state = State.IDLE

            elif state == State.TAKE_FISH:
                if take_fish_img is not None:
                    take_fish_det, take_fish_pos, take_fish_mask = \
                        detect_take_fish_button(take_fish_img, cfg)
                    if take_fish_det and take_fish_pos is not None:
                        sx = cfg["take_fish_region"]["left"] + take_fish_pos[0]
                        sy = cfg["take_fish_region"]["top"]  + take_fish_pos[1]

                        for attempt in range(1, cfg["max_retries"] + 1):
                            d = random_action_delay(cfg)
                            print(f"[TAKE] Button at ({sx},{sy}) — clicking "
                                  f"(attempt {attempt}/{cfg['max_retries']}, "
                                  f"+{d:.2f}s)")
                            pydirectinput.click(x=sx, y=sy)
                            time.sleep(cfg["post_take_delay"])

                            v_img = screenshot_region(
                                sct, cfg["take_fish_region"]
                            )
                            still, _, _ = detect_take_fish_button(v_img, cfg)
                            if not still:
                                print("[TAKE] OK — button gone, fish taken")
                                state = State.IDLE
                                print("[IDLE] Ready for next cast")
                                break
                            print("[TAKE] Button still visible — retrying")
                        else:
                            print("[TAKE] Button persisted after retries — "
                                  "forcing IDLE")
                            state = State.IDLE


            # ---- FPS ----
            dt = time.time() - t0
            frame_times.append(dt)
            if len(frame_times) > 60:
                frame_times.pop(0)
            avg = sum(frame_times) / len(frame_times) if frame_times else 1
            fps = 1.0 / avg if avg > 0 else 0

            # ---- debug window ----
            if cfg["debug"]:
                dbg = build_debug_frame(
                    state=state,
                    fps=fps,
                    fish_caught=fish_caught,
                    bobber_img=bobber_img,
                    bobber_mask=bobber_mask,
                    bobber_pos=bobber_pos,
                    bite_img=bite_img,
                    bite_mask=bite_mask,
                    bite_detected=bite_det,
                    bar_img=bar_img,
                    bar_visible=bar_vis,
                    fish_pos=fish_pos,
                    fish_mask=fish_mask,
                    cube_pos=cube_pos,
                    cube_bbox=cube_bbox,
                    cube_mask=cube_mask,
                    progress_img=progress_img,
                    progress_ratio=prog_ratio,
                    take_fish_img=take_fish_img,
                    take_fish_mask=take_fish_mask,
                    take_fish_detected=take_fish_det,
                    cfg=cfg,
                )
                cv2.imshow("PW-Fisher Debug", dbg)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("[EXIT] Q in debug window — stopping.")
                    break

            # ---- throttle ----
            remaining = cfg["loop_delay"] - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        print("\n[EXIT] Ctrl+C")
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        traceback.print_exc()
    finally:
        release_keys(cfg)
        cv2.destroyAllWindows()
        print(f"\n  Session over — {fish_caught} fish caught.")
        print("  All keys released. Goodbye!")


if __name__ == "__main__":
    main()
