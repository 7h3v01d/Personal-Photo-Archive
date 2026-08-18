from pathlib import Path

from PIL import Image

from ppa import catalogue
from ppa.db import connect
from ppa.scanner import scan_library


def _img(path: Path, color="red", size=(40, 30)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)


def _built(tmp_path: Path):
    library = tmp_path / "library"
    _img(library / "IMG_0001.jpg", "red")
    _img(library / "IMG_0002.jpg", "blue")
    # exact duplicate of 0001
    (library / "backup").mkdir(parents=True)
    Image.open(library / "IMG_0001.jpg").save(library / "backup" / "copy.jpg")
    conn = connect(tmp_path / "catalogue.sqlite3")
    scan_library(conn, library)
    return conn, library


def test_stats_count_photos_files_and_duplicates(tmp_path: Path) -> None:
    conn, _ = _built(tmp_path)
    s = catalogue.library_stats(conn)
    assert s.photos == 2          # two logical photos
    assert s.files == 3           # three physical files
    assert s.duplicate_files == 2 # the two copies of photo 0001
    assert s.last_library_path is not None


def test_all_view_lists_active_files(tmp_path: Path) -> None:
    conn, _ = _built(tmp_path)
    items = catalogue.grid_items(conn, catalogue.VIEW_ALL)
    assert len(items) == 3
    assert all(i.status == "active" for i in items)
    assert all(i.sha256 for i in items)


def test_duplicates_view_only_clustered(tmp_path: Path) -> None:
    conn, _ = _built(tmp_path)
    dups = catalogue.grid_items(conn, catalogue.VIEW_DUPLICATES)
    # both copies of photo 0001, none of the unique 0002
    assert len(dups) == 2
    assert len({d.photo_id for d in dups}) == 1


def test_missing_view(tmp_path: Path) -> None:
    conn, library = _built(tmp_path)
    (library / "IMG_0002.jpg").unlink()
    scan_library(conn, library)
    missing = catalogue.grid_items(conn, catalogue.VIEW_MISSING)
    assert len(missing) == 1
    assert missing[0].filename == "IMG_0002.jpg"


def test_file_detail_includes_events_and_copies(tmp_path: Path) -> None:
    conn, _ = _built(tmp_path)
    dup = catalogue.grid_items(conn, catalogue.VIEW_DUPLICATES)[0]
    detail = catalogue.file_detail(conn, dup.file_id)
    assert detail is not None
    assert detail.copy_count == 2
    assert len(detail.sha256) == 64
    assert len(detail.path_history) >= 1


def test_unknown_view_rejected(tmp_path: Path) -> None:
    conn, _ = _built(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        catalogue.grid_items(conn, "nonsense")
