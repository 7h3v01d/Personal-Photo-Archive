"""Tests for the pure logic behind the UI polish pass."""

from pathlib import Path

from PIL import Image

from ppa import catalogue
from ppa.db import connect
from ppa.scanner import scan_library
from ppa.geometry import project


def test_project_corners_and_centre():
    # Equirectangular: centre of the world maps to the middle.
    assert project(0, 0, 360, 180) == (180, 90)
    # Top-left is lon -180, lat +90.
    assert project(90, -180, 360, 180) == (0, 0)
    # Bottom-right is lon +180, lat -90.
    assert project(-90, 180, 360, 180) == (360, 180)


def test_project_clamps_out_of_range():
    # Absurd inputs clamp inside the canvas rather than painting off it.
    x, y = project(999, 999, 360, 180)
    assert 0 <= x <= 360 and 0 <= y <= 180


def test_project_brisbane_is_lower_right_quadrant():
    # ~ -27.47, 153.02 -> right of centre (east), below centre (south).
    x, y = project(-27.47, 153.02, 360, 180)
    assert x > 180 and y > 90


def _img(path: Path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path)


def test_grid_items_carry_copy_count(tmp_path: Path):
    library = tmp_path / "lib"
    _img(library / "a.jpg", "red")
    Image.open(library / "a.jpg").save(library / "a_copy.jpg")  # exact dup
    _img(library / "b.jpg", "blue")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, library)

    by_name = {i.filename: i for i in catalogue.grid_items(conn, catalogue.VIEW_ALL)}
    assert by_name["a.jpg"].copy_count == 2
    assert by_name["a_copy.jpg"].copy_count == 2
    assert by_name["b.jpg"].copy_count == 1
