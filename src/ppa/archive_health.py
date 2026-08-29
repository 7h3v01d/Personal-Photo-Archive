"""Phase 12.2 — read-only Backup & Archive Health with origin ambiguity.

Archive Health describes what the PPA catalogue can currently prove about copy
coverage inside one registered Library.  Phase 12.1 adds filesystem-object
evidence captured by normal scans: an opaque device id, filesystem object id
(``st_ino`` / platform file index), and link count when the platform exposes
them.

This lets the read model identify hard-linked directory entries and distinguish
them from distinct filesystem objects.  It still does *not* call either case an
"independent backup": distinct filesystem device ids are stronger evidence, but
are not proof of separate physical hardware or failure domains.

Phase 12.2 also surfaces Files that were catalogued under explicit ambiguous-origin
evidence rather than being arbitrarily mapped onto one historical missing File.

No source files are opened or modified here.  The model reads only catalogue
state already established by scanning / verification.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from sqlite3 import Connection, Row

from ppa.current_identity import verified_current_sha256_sql
from ppa.organization_browse import OrganizationBrowseView, build_membership_browse

ARCHIVE_HEALTH_SCHEMA = "ppa-archive-health/4"


@dataclass(frozen=True)
class ArchiveHealth:
    schema: str
    read_only: bool
    library_id: int
    total_photos: int
    total_files: int
    present_files: int
    missing_files: int
    no_present_photo_ids: tuple[str, ...]
    single_present_photo_ids: tuple[str, ...]
    multiple_exact_present_photo_ids: tuple[str, ...]
    photos_with_missing_copies: tuple[str, ...]
    unhealthy_present_photo_ids: tuple[str, ...]
    unknown_hash_photo_ids: tuple[str, ...]
    divergent_photo_ids: tuple[str, ...]
    exact_storage_unknown_photo_ids: tuple[str, ...]
    hardlink_overstated_photo_ids: tuple[str, ...]
    distinct_file_object_photo_ids: tuple[str, ...]
    distinct_device_photo_ids: tuple[str, ...]
    ambiguous_origin_photo_ids: tuple[str, ...]

    @property
    def no_present_count(self) -> int:
        return len(self.no_present_photo_ids)

    @property
    def single_present_count(self) -> int:
        return len(self.single_present_photo_ids)

    @property
    def multiple_exact_present_count(self) -> int:
        return len(self.multiple_exact_present_photo_ids)

    @property
    def missing_copy_photo_count(self) -> int:
        return len(self.photos_with_missing_copies)

    @property
    def unhealthy_present_count(self) -> int:
        return len(self.unhealthy_present_photo_ids)

    @property
    def unknown_hash_count(self) -> int:
        return len(self.unknown_hash_photo_ids)

    @property
    def divergent_count(self) -> int:
        return len(self.divergent_photo_ids)

    @property
    def exact_storage_unknown_count(self) -> int:
        return len(self.exact_storage_unknown_photo_ids)

    @property
    def hardlink_overstated_count(self) -> int:
        return len(self.hardlink_overstated_photo_ids)

    @property
    def distinct_file_object_count(self) -> int:
        return len(self.distinct_file_object_photo_ids)

    @property
    def distinct_device_count(self) -> int:
        return len(self.distinct_device_photo_ids)

    @property
    def ambiguous_origin_count(self) -> int:
        return len(self.ambiguous_origin_photo_ids)

    @property
    def attention_photo_ids(self) -> tuple[str, ...]:
        # De-duplicate overlapping indicators while preserving deterministic id
        # ordering. Hard-link inflation and unknown storage identity matter here
        # because either can make a path-count redundancy claim misleading.
        ids = set(self.no_present_photo_ids)
        ids.update(self.single_present_photo_ids)
        ids.update(self.photos_with_missing_copies)
        ids.update(self.unhealthy_present_photo_ids)
        ids.update(self.unknown_hash_photo_ids)
        ids.update(self.divergent_photo_ids)
        ids.update(self.exact_storage_unknown_photo_ids)
        ids.update(self.hardlink_overstated_photo_ids)
        ids.update(self.ambiguous_origin_photo_ids)
        return tuple(sorted(ids))

    @property
    def attention_count(self) -> int:
        return len(self.attention_photo_ids)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update({
            "counts": {
                "no_present": self.no_present_count,
                "single_present": self.single_present_count,
                "multiple_exact_present": self.multiple_exact_present_count,
                "photos_with_missing_copies": self.missing_copy_photo_count,
                "unhealthy_present": self.unhealthy_present_count,
                "unknown_hash": self.unknown_hash_count,
                "current_hash_divergence": self.divergent_count,
                "exact_sets_with_unknown_storage_identity": self.exact_storage_unknown_count,
                "exact_sets_with_hardlink_path_inflation": self.hardlink_overstated_count,
                "exact_sets_with_distinct_file_objects": self.distinct_file_object_count,
                "exact_sets_spanning_distinct_device_ids": self.distinct_device_count,
                "photos_with_recorded_ambiguous_origin": self.ambiguous_origin_count,
                "attention_photos": self.attention_count,
            },
            "attention_photo_ids": list(self.attention_photo_ids),
        })
        return data

    def to_json(self, *, pretty: bool = True) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


def _storage_key(row: Row) -> tuple[str, str] | None:
    device = row["fs_device_id"]
    obj = row["fs_object_id"]
    if device is None or obj is None:
        return None
    return str(device), str(obj)


def build_archive_health(conn: Connection, *, library_id: int) -> ArchiveHealth:
    """Build catalogue-only copy/availability/storage health for one Library.

    Semantic boundaries:

    * ``multiple_exact_present_photo_ids`` means two or more healthy present
      Files with one shared *verified-current* SHA-256.
    * ``distinct_file_object_photo_ids`` means the latest scan observed at
      least two distinct ``(device, object)`` keys for such an exact set.
    * ``distinct_device_photo_ids`` means the OS reported more than one device
      id.  This is *not* a claim of independent physical disks/failure domains.
    * ``hardlink_overstated_photo_ids`` means at least two catalogue paths in
      the exact set resolve to one known filesystem object, so path count
      overstates filesystem-object count.
    """
    if conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
        raise ValueError(f"unknown library {library_id}")

    before = conn.total_changes
    current_sha = verified_current_sha256_sql("f", "r")
    rows = conn.execute(
        f"SELECT f.photo_id, f.presence_status, f.health_status, "
        f"f.fs_device_id, f.fs_object_id, f.fs_link_count, "
        f"f.sha256 AS expected_sha256, {current_sha} AS verified_current_sha256 "
        f"FROM files f "
        f"LEFT JOIN file_revisions r ON r.id=f.current_revision_id AND r.file_id=f.id "
        f"WHERE f.library_id=? ORDER BY f.photo_id, f.id",
        (library_id,),
    ).fetchall()

    ambiguity_rows = conn.execute(
        "SELECT DISTINCT f.photo_id FROM file_origin_ambiguities a "
        "JOIN files f ON f.id=a.observed_file_id "
        "WHERE a.library_id=? AND f.library_id=? ORDER BY f.photo_id",
        (library_id, library_id),
    ).fetchall()
    ambiguous_origin = tuple(str(r["photo_id"]) for r in ambiguity_rows)

    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["photo_id"])].append(row)

    no_present: list[str] = []
    single_present: list[str] = []
    multiple_exact: list[str] = []
    with_missing: list[str] = []
    unhealthy: list[str] = []
    unknown_hash: list[str] = []
    divergent: list[str] = []
    storage_unknown: list[str] = []
    hardlink_overstated: list[str] = []
    distinct_objects: list[str] = []
    distinct_devices: list[str] = []
    total_files = present_files = missing_files = 0

    for photo_id in sorted(grouped):
        photo_rows = grouped[photo_id]
        present = [r for r in photo_rows if r["presence_status"] == "present"]
        missing = [r for r in photo_rows if r["presence_status"] == "missing"]
        unhealthy_now = [r for r in present if r["health_status"] != "ok"]
        # Current-byte identity is fail-closed.  A present File whose expected
        # revision is mismatched/unhealthy/incoherent has UNKNOWN current SHA
        # here even though its immutable expected SHA remains catalogued.
        unknown_hash_now = [r for r in present if r["verified_current_sha256"] is None]
        known_hashes = {
            str(r["verified_current_sha256"]) for r in present
            if r["verified_current_sha256"] is not None
        }

        total_files += len(photo_rows)
        present_files += len(present)
        missing_files += len(missing)

        if not present:
            no_present.append(photo_id)
        if len(present) == 1:
            single_present.append(photo_id)
        if missing and present:
            with_missing.append(photo_id)
        if unhealthy_now:
            unhealthy.append(photo_id)
        if unknown_hash_now:
            unknown_hash.append(photo_id)
        if len(known_hashes) > 1:
            divergent.append(photo_id)

        exact_set = (
            len(present) >= 2
            and len(known_hashes) == 1
            and not unknown_hash_now
            and not unhealthy_now
        )
        if not exact_set:
            continue

        multiple_exact.append(photo_id)
        keys = [_storage_key(r) for r in present]
        known_keys = [k for k in keys if k is not None]
        if len(known_keys) != len(present):
            storage_unknown.append(photo_id)

        # We can prove a hard-link relationship between the known members even
        # if another member's identity is unknown.  We cannot, however, assert
        # the total number of distinct objects/devices until all members are
        # known, so those classifications remain fail-closed below.
        counts = Counter(known_keys)
        if any(count >= 2 for count in counts.values()):
            hardlink_overstated.append(photo_id)

        if len(known_keys) == len(present):
            unique_objects = set(known_keys)
            if len(unique_objects) >= 2:
                distinct_objects.append(photo_id)
            device_ids = {device for device, _obj in unique_objects}
            if len(device_ids) >= 2:
                distinct_devices.append(photo_id)

    if conn.total_changes != before:
        raise RuntimeError("archive health projection must be read-only")

    return ArchiveHealth(
        ARCHIVE_HEALTH_SCHEMA,
        True,
        int(library_id),
        len(grouped),
        total_files,
        present_files,
        missing_files,
        tuple(no_present),
        tuple(single_present),
        tuple(multiple_exact),
        tuple(with_missing),
        tuple(unhealthy),
        tuple(unknown_hash),
        tuple(divergent),
        tuple(storage_unknown),
        tuple(hardlink_overstated),
        tuple(distinct_objects),
        tuple(distinct_devices),
        ambiguous_origin,
    )


_CATEGORY_MAP = {
    "attention": ("attention_photo_ids", "Archive Health — Needs Attention",
                  "Logical Photos with one or more current catalogue health, copy-coverage, or storage-identity indicators."),
    "no_present": ("no_present_photo_ids", "No Present Catalogued File",
                   "Logical Photos for which this Library currently has no present catalogued File."),
    "single_present": ("single_present_photo_ids", "One Present Catalogued File",
                       "Only one present File is known in this Library. PPA cannot infer whether an off-catalogue backup exists."),
    "multiple_exact": ("multiple_exact_present_photo_ids", "Multiple Exact Present Files",
                       "Two or more healthy present Files share one current SHA-256. Filesystem-object evidence is reported separately."),
    "missing_copies": ("photos_with_missing_copies", "Some Catalogued Copies Missing",
                       "At least one catalogued File is present and at least one other catalogued File for the same logical Photo is missing."),
    "unhealthy": ("unhealthy_present_photo_ids", "Present Files with Health Warnings",
                  "At least one present File has a current catalogue health state other than ok."),
    "unknown_hash": ("unknown_hash_photo_ids", "Present Files without Current SHA-256",
                     "At least one present File lacks a current catalogue SHA-256, so exact-copy coverage cannot be proven."),
    "divergent": ("divergent_photo_ids", "Current Content Divergence",
                  "One logical Photo currently owns present Files with more than one known SHA-256; investigate identity before making redundancy claims."),
    "storage_unknown": ("exact_storage_unknown_photo_ids", "Exact Sets with Unknown Storage Identity",
                        "At least one member of a multiple-exact set lacks a current device/object identity. Re-scan before interpreting filesystem-object redundancy."),
    "hardlinks": ("hardlink_overstated_photo_ids", "Exact Sets with Hard-Link Path Inflation",
                  "At least two catalogue paths in the exact set share one observed device/object identity, so path count overstates filesystem-object count."),
    "distinct_objects": ("distinct_file_object_photo_ids", "Exact Sets with Distinct Filesystem Objects",
                         "All members have current storage identity and the exact set spans at least two distinct filesystem objects. This is not yet a physical-device independence claim."),
    "distinct_devices": ("distinct_device_photo_ids", "Exact Sets Spanning Distinct Device IDs",
                         "All members have current storage identity and the OS reports at least two device ids. Device ids are filesystem evidence, not proof of separate physical failure domains."),
    "ambiguous_origin": ("ambiguous_origin_photo_ids", "Recorded Ambiguous File Origins",
                         "At least one currently catalogued File was observed when multiple byte-identical historical Files could explain its origin. PPA preserved the ambiguity instead of choosing a candidate."),
}


def build_archive_health_browse(
    conn: Connection, health: ArchiveHealth, category: str
) -> OrganizationBrowseView:
    """Build a read-only logical-Photo browser for one Archive Health category."""
    if category not in _CATEGORY_MAP:
        raise ValueError(f"unknown archive-health category {category!r}")
    attr, name, description = _CATEGORY_MAP[category]
    photo_ids = getattr(health, attr)
    return build_membership_browse(
        conn,
        library_id=health.library_id,
        photo_ids=photo_ids,
        object_kind="archive_health",
        object_id=category,
        name=name,
        description=description,
    )


def concise_text(health: ArchiveHealth) -> str:
    return "\n".join([
        "PPA Backup & Archive Health",
        "===========================",
        f"Library: {health.library_id}",
        f"Logical photos: {health.total_photos}",
        f"Catalogued Files: {health.total_files} ({health.present_files} present, {health.missing_files} missing)",
        f"No present catalogued File: {health.no_present_count}",
        f"One present catalogued File: {health.single_present_count}",
        f"Multiple exact present Files: {health.multiple_exact_present_count}",
        f"Photos with some catalogued copies missing: {health.missing_copy_photo_count}",
        f"Present-file health warnings: {health.unhealthy_present_count}",
        f"Present Files without verified current SHA-256: {health.unknown_hash_count}",
        f"Current hash divergence: {health.divergent_count}",
        f"Exact sets with unknown storage identity: {health.exact_storage_unknown_count}",
        f"Exact sets whose path count is inflated by hard links: {health.hardlink_overstated_count}",
        f"Exact sets spanning distinct filesystem objects: {health.distinct_file_object_count}",
        f"Exact sets spanning distinct filesystem device ids: {health.distinct_device_count}",
        f"Logical photos with recorded ambiguous File origin: {health.ambiguous_origin_count}",
        f"Logical photos needing attention: {health.attention_count}",
        "Filesystem-object/device evidence improves redundancy accounting, but PPA still does not call it proof of independent physical backup hardware.",
    ])
