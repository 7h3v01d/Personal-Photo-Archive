from pathlib import Path

from PIL import ExifTags, Image

from ppa import metadata
from ppa.db import connect
from ppa.scanner import scan_library


def _jpeg_with_exif(path: Path, *, make="Canon", model="PowerShot A70",
                    dto="2004:12:25 09:14:32", serial="ABC123",
                    gps=True, color="red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (120, 90), color)
    exif = img.getexif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    exif[0x0131] = "PPA-test/1.0"     # Software
    exif[0x0112] = 6                   # Orientation
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    if dto:
        sub[0x9003] = dto              # DateTimeOriginal
    sub[0x8827] = 200                  # ISO
    sub[0x829D] = 2.8                  # FNumber
    sub[0x920A] = 5.4                  # FocalLength
    if serial:
        sub[0xA431] = serial           # BodySerialNumber
    if gps:
        g = exif.get_ifd(ExifTags.IFD.GPSInfo)
        g[1] = "S"; g[2] = (27.0, 28.0, 12.0)
        g[3] = "E"; g[4] = (153.0, 1.0, 30.0)
    img.save(path, format="JPEG", exif=exif)


def test_extract_reads_core_exif(tmp_path: Path) -> None:
    src = tmp_path / "IMG_0001.jpg"
    _jpeg_with_exif(src)
    result = metadata.extract_observations(src)

    kv = {o.key: o.value for o in result.observations}
    assert kv["DateTimeOriginal"] == "2004:12:25 09:14:32"
    assert kv["Make"] == "Canon"
    assert kv["Model"] == "PowerShot A70"
    assert kv["ISOSpeedRatings"] == "200"
    assert result.make == "Canon"
    assert result.model == "PowerShot A70"
    assert result.serial == "ABC123"


def test_extract_derives_decimal_gps(tmp_path: Path) -> None:
    src = tmp_path / "IMG_0001.jpg"
    _jpeg_with_exif(src, gps=True)
    result = metadata.extract_observations(src)
    kv = {o.key: o.value for o in result.observations}
    # S/E -> negative lat, positive lon
    assert kv["GPSLatitudeDecimal"].startswith("-27.")
    assert kv["GPSLongitudeDecimal"].startswith("153.")


def test_extract_handles_no_exif(tmp_path: Path) -> None:
    src = tmp_path / "plain.png"
    Image.new("RGB", (10, 10), "blue").save(src)
    result = metadata.extract_observations(src)
    assert result.make is None
    assert all(o.source != "exif" for o in result.observations) or not result.observations


def test_store_links_camera_and_is_idempotent(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _jpeg_with_exif(library / "IMG_0001.jpg")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, library)

    n1 = metadata.extract_stale(conn)
    assert n1 == 1

    # Camera row created and linked
    cams = conn.execute("SELECT * FROM cameras").fetchall()
    assert len(cams) == 1
    assert cams[0]["make"] == "Canon"
    file_row = conn.execute("SELECT camera_id FROM files").fetchone()
    assert file_row["camera_id"] == cams[0]["id"]

    obs_count_1 = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]

    # Running again does nothing (hash unchanged) and doesn't duplicate rows.
    n2 = metadata.extract_stale(conn)
    assert n2 == 0
    obs_count_2 = conn.execute("SELECT COUNT(*) AS n FROM metadata_observations").fetchone()["n"]
    assert obs_count_1 == obs_count_2
    assert len(conn.execute("SELECT * FROM cameras").fetchall()) == 1  # no dup camera


def test_reextraction_on_content_change(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    img = library / "IMG_0001.jpg"
    _jpeg_with_exif(img, dto="2004:12:25 09:14:32")
    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, library)
    metadata.extract_stale(conn)

    from ppa import catalogue
    fid = conn.execute("SELECT id FROM files").fetchone()["id"]
    assert dict(catalogue.curated_metadata(conn, fid))["capture date (observed)"] == "2004:12:25 09:14:32"

    # Change the file's content AND its embedded date, rescan, re-extract.
    _jpeg_with_exif(img, dto="2005:01:01 10:00:00")
    scan_library(conn, library)
    n = metadata.extract_stale(conn)
    assert n == 1  # a new revision needs reading

    # The CURRENT view shows the new date...
    assert dict(catalogue.curated_metadata(conn, fid))["capture date (observed)"] == "2005:01:01 10:00:00"
    # ...but the historical observation is preserved (attached to the old
    # revision), not overwritten. Both dates survive in the ledger.
    dates = {
        r["value"]
        for r in conn.execute(
            "SELECT DISTINCT value FROM metadata_observations WHERE key='DateTimeOriginal'"
        ).fetchall()
    }
    assert dates == {"2004:12:25 09:14:32", "2005:01:01 10:00:00"}
    # Two revisions exist; one superseded, one current.
    revs = conn.execute("SELECT COUNT(*) AS n FROM file_revisions").fetchone()["n"]
    assert revs == 2


def test_originals_untouched_by_extraction(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    img = library / "IMG_0001.jpg"
    _jpeg_with_exif(img)
    before = img.read_bytes()

    conn = connect(tmp_path / "cat.sqlite3")
    scan_library(conn, library)
    metadata.extract_stale(conn)

    assert img.read_bytes() == before  # extraction reads, never writes
