import cv2
import numpy as np
import pytest

from clipper.branding import (DEFAULTS, check_defaults, clip_branding, list_assets, load_defaults, logo_xy,
                              make_bar, save_asset, save_defaults)
from clipper.util import ClipperError


def png_bytes(color=(0, 0, 255, 255), w=40, h=20):
    img = np.zeros((h, w, 4), np.uint8)
    img[:] = color
    return cv2.imencode(".png", img)[1].tobytes()


@pytest.fixture
def brand(tmp_path):
    return tmp_path / "branding"


def test_uploads_must_be_readable_images(brand):
    assert save_asset(brand, "My Logo.PNG", png_bytes())["name"] == "My Logo.png"
    for filename, data in [("notes.txt", png_bytes()), ("fake.png", b"not really"), ("empty.png", b"")]:
        with pytest.raises(ClipperError):
            save_asset(brand, filename, data)
    assert [a["name"] for a in list_assets(brand)] == ["My Logo.png"]


def test_same_name_twice_keeps_both_files(brand):
    first = save_asset(brand, "logo.png", png_bytes())["name"]
    second = save_asset(brand, "logo.png", png_bytes(color=(0, 255, 0, 255)))["name"]
    assert first == "logo.png" and second != first


def test_defaults_are_checked_before_they_are_saved(brand):
    save_asset(brand, "logo.png", png_bytes())
    save_defaults(brand, {"logo": "logo.png", "logo_position": "bottom-left", "logo_size": 20, "logo_opacity": 80,
                          "progress": {"enabled": True, "style": "gradient", "color": "#112233",
                                       "color2": "#AABBCC", "edge": "top"}})
    saved = load_defaults(brand)
    assert saved["logo"] == "logo.png" and saved["logo_position"] == "bottom-left"
    assert saved["progress"]["enabled"] is True and saved["progress"]["edge"] == "top"
    bad_inputs = [{"logo_position": "middle"}, {"logo_size": 60}, {"logo": "missing.png"},
                  {"progress": {"color": "red"}}, {"progress": {"style": "image", "image": None}}]
    for bad in bad_inputs:
        with pytest.raises(ClipperError):
            check_defaults(brand, bad)


def test_a_deleted_logo_is_forgotten(brand):
    save_asset(brand, "logo.png", png_bytes())
    save_defaults(brand, {"logo": "logo.png"})
    (brand / "logo.png").unlink()
    assert load_defaults(brand)["logo"] is None


def test_logo_positions_map_to_overlay_coordinates():
    assert logo_xy("top-left", 30) == ("30", "30")
    assert logo_xy("middle-center", 30) == ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2")
    assert logo_xy("bottom-right", 30) == ("main_w-overlay_w-30", "main_h-overlay_h-30")


def test_plain_clip_draws_nothing_and_clip_overrides_win(brand, tmp_path):
    work = tmp_path / "work"
    assert clip_branding(None, None, brand, work, 1080, 1920) is None
    save_asset(brand, "logo.png", png_bytes())
    job = {**DEFAULTS, "logo": "logo.png", "progress": {**DEFAULTS["progress"]}}
    only_logo = clip_branding(job, None, brand, work, 1080, 1920)
    assert only_logo["logo"]["path"].endswith("logo.png") and only_logo["progress"] is None
    assert clip_branding(job, {"logo_position": "none"}, brand, work, 1080, 1920) is None
    with_bar = clip_branding(job, {"progress": True}, brand, work, 1080, 1920)
    assert with_bar["logo"] and with_bar["progress"]["path"].endswith(".png") and with_bar["progress"]["edge"] == "bottom"
    assert clip_branding(job, {"progress": False}, brand, work, 1080, 1920)["progress"] is None


def test_progress_bar_strips_match_the_frame_and_the_colours(tmp_path):
    solid = make_bar("solid", "#FFD400", "#000000", None, 1080, 17, tmp_path)
    img = cv2.imread(str(solid), cv2.IMREAD_UNCHANGED)
    assert img.shape == (17, 1080, 4)
    assert tuple(img[0, 0, :3]) == (0, 212, 255)  # #FFD400 in BGR
    gradient = cv2.imread(str(make_bar("gradient", "#000000", "#FFFFFF", None, 1080, 17, tmp_path)), cv2.IMREAD_UNCHANGED)
    assert tuple(gradient[0, 0, :3]) == (0, 0, 0) and tuple(gradient[0, -1, :3]) == (255, 255, 255)
    picture = tmp_path / "pic.png"
    cv2.imwrite(str(picture), np.full((50, 300, 3), 128, np.uint8))
    cropped = cv2.imread(str(make_bar("image", "#000000", "#000000", picture, 1080, 17, tmp_path)), cv2.IMREAD_UNCHANGED)
    assert cropped.shape == (17, 1080, 4)
    assert make_bar("solid", "#FFD400", "#000000", None, 1080, 17, tmp_path) == solid  # cached
