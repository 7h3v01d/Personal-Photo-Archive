# Personal Photo Archive
## Detailed Development Roadmap

# Phase 0 — Project Foundation

### Objective

Define the archival contract before touching the photo collection.

### Deliverables

- project repository
- architecture document
- data safety rules
- supported image-format policy
- SQLite schema v1
- configuration system
- logging framework
- test framework
- development sample library

### Fundamental Rules

1. Source files are read-only by default.
2. No automatic deletion.
3. No automatic metadata rewriting.
4. Every imported photograph receives a persistent ID.
5. Every interpretation records provenance.
6. All destructive operations require explicit user action.
7. Database schema changes must be migratable.
8. Analysis must be reproducible where practical.

### Suggested Stack

- Python 3.11+
- PySide6
- SQLite
- Pillow
- ExifTool integration or equivalent robust metadata parser
- hashlib
- pytest

### Exit Criteria

- project launches
- database initialises
- configuration loads
- logging works
- test environment works
- sample files can be safely read

---

# Phase 1 — Safe Library Scanner

### Objective

Allow the application to inspect a photograph collection without changing anything.

### Features

- select library directory
- recursive scanning
- supported file detection
- file-size capture
- modification-time capture
- image-dimension detection
- file-type detection
- persistent file identity
- inaccessible-file reporting

### Initial Formats

Priority:

- JPEG
- PNG
- TIFF
- HEIC where feasible

Later:

- RAW formats
- WebP
- legacy formats

### Scanner Behaviour

The scanner must differentiate:

- new files
- known files
- moved files
- renamed files
- modified files
- missing files
- unsupported files

### Exit Criteria

10,000+ photographs can be scanned without source modification or application instability.

---

# Phase 2 — Cryptographic Identity and Integrity

### Objective

Establish trustworthy file identity.

### Features

- SHA-256 hashing
- file fingerprint database
- first-seen timestamp
- original path history
- size validation
- hash re-verification
- corruption warnings

### Database Concepts

**Photo**

Logical photographic identity.

**File**

Physical representation of a photograph.

This distinction becomes essential once duplicates and derivatives are introduced.

### Exit Criteria

The application can reliably determine whether a physical file is:

- unchanged
- moved
- renamed
- duplicated
- modified

---

# Phase 3 — Metadata Extraction

### Objective

Capture every useful piece of existing metadata.

### Extract

- DateTimeOriginal
- CreateDate
- ModifyDate
- camera manufacturer
- camera model
- camera serial where present
- lens
- focal length
- exposure
- ISO
- aperture
- orientation
- GPS
- embedded thumbnail
- software/editor information
- image dimensions

### Important Rule

Metadata extraction creates **observations**, not truths.

Example:

`EXIF.DateTimeOriginal = 2001-01-01`

should be stored separately from:

`interpreted_capture_date`

### Exit Criteria

Metadata from the historical collection is searchable and preserved in the catalogue.

---

# Phase 4 — Thumbnail and Preview Engine

### Objective

Make browsing fast without repeatedly opening full-resolution originals.

### Features

- thumbnail cache
- multiple thumbnail sizes
- lazy loading
- image orientation correction
- preview cache
- cache regeneration
- cache invalidation

### UI

Initial views:

- grid
- single image
- metadata panel
- folder browser

### Performance Target

Scrolling should remain responsive with libraries substantially larger than the initial 10,000 images.

---

# Phase 5 — Core Desktop Library UI

### Objective

Create the first genuinely useful daily application.

### Navigation

- Library
- Folders
- Timeline
- Albums
- Unplaced
- Duplicates
- Recently Added

### Photo Inspector

Display:

- image
- source path
- filename
- dimensions
- size
- hash
- original metadata
- interpreted metadata
- confidence
- tags
- rating

### Batch Selection

Users should be able to select dozens or hundreds of photographs for common operations.

### Exit Criteria

The project can replace basic filesystem browsing for normal collection navigation.

---

# Phase 6 — Date Reliability Engine

### Objective

Identify photographs whose timestamps may be unreliable.

### Signals

- suspicious default dates
- impossible chronology
- dates before camera manufacture
- large sequence discontinuities
- repeated reset timestamps
- timestamp regression
- inconsistent neighbouring images
- filename continuity
- folder-date conflicts

### Example Rules

Flag:

- 01/01/2000
- 01/01/2001
- repeated midnight timestamps
- hundreds of sequential photographs sharing implausible dates

### Result

Each photo receives something similar to:

```text
Timestamp Reliability

TRUSTED
PROBABLY_VALID
QUESTIONABLE
LIKELY_WRONG
UNKNOWN
```

### Exit Criteria

The application can identify likely broken-clock photographs without changing them.

---

# Phase 7 — Historical Date Reconstruction

### Objective

Help rebuild the chronology of older photographs.

This is one of the project's defining phases.

### Evidence Engine

Potential evidence:

- filename ordering
- EXIF sequence
- folder structure
- camera identity
- neighbouring trusted images
- filesystem dates
- import dates
- recurring people
- GPS
- known events
- manually confirmed anchors

### Chronology Anchors

A user-confirmed photo becomes a strong chronological anchor.

Example:

```text
IMG_4000 → confirmed 24 Dec 2004
IMG_4082 → confirmed 26 Dec 2004
```

Photographs between them may be inferred to belong within that interval.

### Proposed Corrections

The system should display:

```text
Current EXIF:
01 Jan 2001

Proposed:
24–26 Dec 2004

Confidence:
0.89

Evidence:
5 supporting signals
1 conflicting signal
```

### User Actions

- Accept
- Reject
- Modify
- Mark uncertain
- Apply to sequence

### Exit Criteria

Large batches of broken-timestamp photographs can be reconstructed efficiently while preserving original metadata.

---

# Phase 8 — Timeline

### Objective

Make chronology a primary navigation system.

### Levels

- decade
- year
- month
- day
- event
- sequence

### Special Areas

## Unplaced Memories

Photographs with insufficient date information.

## Uncertain Timeline

Photographs placed approximately rather than exactly.

### UI Opportunity

Confidence could be visually represented without implying false precision.

### Exit Criteria

The entire historical collection becomes navigable chronologically.

---

# Phase 9 — Album and Tag System

### Objective

Allow flexible organisation independent of filesystem structure.

### Albums

- manual albums
- smart albums
- event albums
- favourites

### Tags

Examples:

- Family
- Christmas
- Holiday
- Beach
- School
- Pets

### Smart Album Examples

`Camera = Canon IXUS`

`Date confidence < 0.60`

`Rating >= 4`

`People includes Dad`

### Exit Criteria

Photographs can participate in unlimited conceptual collections without duplication.

---

# Phase 10 — Duplicate Detection

### Objective

Understand duplication before attempting cleanup.

### Stage A — Exact Duplicates

SHA-256.

### Stage B — Near Duplicates

Perceptual hashing.

### Stage C — Derivative Detection

Attempt to identify:

- resizing
- recompression
- colour changes
- cropping

### Duplicate Review

Never default to:

`DELETE`

Instead show:

```text
Best Candidate Original
Related Copies
Storage Used
Resolution Differences
Metadata Differences
```

### Exit Criteria

Duplicate clusters are safely reviewable.

---

# Phase 11 — Photo Lineage

### Objective

Represent relationships among versions of a photograph.

Example:

```text
Original Camera JPEG
    ├── resized email copy
    ├── edited colour version
    ├── cropped version
    └── exact backup
```

### Value

This makes future cleanup substantially safer.

---

# Phase 12 — Non-Destructive Editing

### Objective

Introduce editing without compromising archival integrity.

### Version 1 Editing

- rotation
- crop
- straighten
- exposure
- brightness
- contrast
- saturation

### Later

- white balance
- highlight recovery
- shadow adjustment
- noise reduction
- sharpening
- red-eye correction

### Editing Model

```text
Original
+
Edit Recipe
=
Rendered View
```

### History

Every edit should support:

- undo
- redo
- reset
- named versions

### Exit Criteria

Users can improve photographs while always retaining the original.

---

# Phase 13 — Export Engine

### Objective

Create controlled derivative copies.

### Export Options

- JPEG
- PNG
- resize
- quality
- metadata policy
- watermark
- filename template
- folder structure

Example:

`{year}/{event}/{original_name}`

### Export Metadata Choices

- preserve original metadata
- corrected metadata
- strip private metadata
- strip GPS
- minimal metadata

---

# Phase 14 — People and Face Detection

### Objective

Introduce local identity organisation.

### Stage 1

Detect faces.

### Stage 2

Generate face embeddings.

### Stage 3

Cluster similar faces.

### Stage 4

User names clusters.

### Stage 5

System proposes identities.

### Safety Principle

Recognition remains a suggestion until confirmed.

### Exit Criteria

Users can browse photographs by person.

---

# Phase 15 — Age-Aware Identity Graph

### Objective

Improve recognition across decades.

### Person Model

```text
Person
   ├── Face Cluster 2002–2005
   ├── Face Cluster 2006–2010
   ├── Face Cluster 2011–2018
   └── Face Cluster 2019–present
```

### Benefits

- better child recognition
- better long-term matching
- stronger historical inference

---

# Phase 16 — Event Detection

### Objective

Automatically identify probable photographic events.

### Signals

- temporal proximity
- same camera
- GPS proximity
- same people
- visual similarity
- folder grouping

### Proposal

```text
Possible Event

25 Dec 2012
08:31–13:42

218 photos
7 people recognised

Suggested title:
Christmas 2012
```

The user confirms or adjusts it.

---

# Phase 17 — Places

### Objective

Create location-aware organisation.

### Sources

- GPS
- user confirmation
- recurring GPS clusters
- event association
- optional image inference

### Place Hierarchy

```text
Australia
  Queensland
    Brisbane
      Home
```

Specific home addresses should remain optional rather than required.

---

# Phase 18 — Memory Graph

### Objective

Connect photographic information into relationships rather than isolated records.

Possible graph relationships:

```text
Person → appears_in → Photo

Photo → belongs_to → Event

Event → occurred_at → Place

Photo → taken_with → Camera

Photo → derivative_of → Photo

Person → attended → Event
```

This creates the foundation for much richer contextual queries.

---

# Phase 19 — Advanced Search

### Structured Search

Examples:

```text
person:dad year:<2010 event:christmas
```

```text
camera:"Canon PowerShot" confidence:<0.5
```

### Natural Language Search

Later:

> Show me photos of Dad at Christmas before 2010.

> Find old Canon photographs whose dates are questionable.

The natural-language layer should translate requests into deterministic catalogue queries whenever possible.

---

# Phase 20 — Local AI Enrichment

### Objective

Add intelligence without making AI a dependency.

Potential capabilities:

- photo descriptions
- scene classification
- event suggestions
- object detection
- semantic search
- historical-date clues

### AI Record

Every generated observation should store:

- model
- model version
- generation date
- confidence
- prompt or analysis type
- user confirmation status

AI observations remain replaceable.

Historical evidence does not.

---

# Phase 21 — Archive Health

### Objective

Provide a single health view for the photographic archive.

### Dashboard

Possible metrics:

- total photographs
- total storage
- exact duplicates
- possible derivatives
- missing files
- hash failures
- unresolved dates
- unplaced photographs
- unreviewed people
- photos without backups
- database backup status

Example:

```text
Archive Health

10,428 photos

99.98% integrity verified

312 uncertain dates
74 unplaced photos
148 exact duplicates
23 possible missing files
```

---

# Phase 22 — Backup Awareness

### Objective

Help protect the collection without pretending the application itself is the backup system.

### Features

- define backup locations
- compare hashes
- identify unprotected originals
- verify backup freshness
- database backup scheduling
- restore catalogue

### Possible Future Rule

**2 copies minimum before destructive cleanup is permitted.**

---

# Phase 23 — Managed Archive

### Objective

Optionally move from referenced files to an application-managed archival structure.

Possible layout:

```text
Archive/
    Originals/
    Database/
    Thumbnails/
    Cache/
    Exports/
    Backups/
```

Original filenames may remain preserved internally regardless of physical layout.

---

# Phase 24 — Growing Library / Watch Folders

### Objective

Allow the archive to grow automatically.

### Watch Sources

- phone import folder
- camera SD card
- downloads
- scanner folder
- manually configured directories

### Workflow

```text
New files detected
      ↓
Scan
      ↓
Fingerprint
      ↓
Duplicate check
      ↓
Metadata
      ↓
Thumbnail
      ↓
Catalogue
      ↓
Optional analysis
```

---

# Phase 25 — Camera Profiles

### Objective

Learn characteristics of historical cameras.

A camera profile might include:

- make/model
- serial
- active years
- typical filename pattern
- clock reliability
- known clock-reset periods

Example:

```text
Canon PowerShot A70

Observed:
2003–2007

Clock resets detected:
11

Common reset value:
01 Jan 2003
```

This becomes valuable evidence for historical reconstruction.

---

# Phase 26 — Historical Reconstruction Workbench

### Objective

Create a dedicated interface for solving uncertain photographic history.

Workbench could show:

```text
Previous Known Photo
        ↓
Unknown Sequence
        ↓
Next Known Photo
```

alongside:

- camera
- filenames
- people
- event hypotheses
- metadata conflicts
- visual similarity

This turns archival reconstruction into a guided investigative process.

---

# Phase 27 — Memory Notes

### Objective

Allow photographs to contain human history that pixels alone cannot represent.

Example:

**Photo:** IMG_8214

**Memory:**

> Taken at Dad's old house. We had just finished Christmas lunch and the kids were playing outside.

These notes may eventually become among the most valuable data in the entire archive.

---

# Phase 28 — Family History Layer

Potential future additions:

- family relationships
- birthdays
- anniversaries
- residences
- major life events

This contextual information could improve both navigation and historical reconstruction.

It should remain optional and user-controlled.

---

# Phase 29 — Story Mode

### Objective

Transform albums or events into narrative presentations.

Example:

**Christmas 2007**

- opening photograph
- event description
- timeline
- people
- selected highlights
- memories

Possible exports:

- slideshow
- printable album
- PDF
- video montage
- family archive package

---

# Phase 30 — Long-Term Archive Format

### Objective

Ensure the collection remains understandable even if the application eventually disappears.

Possible archive export:

```text
Archive Export
 ├── Originals
 ├── Metadata.json
 ├── Metadata.csv
 ├── Albums
 ├── People
 ├── Events
 ├── Edits
 └── README
```

Open, documented formats should be preferred.

The user's memories should never be trapped inside proprietary database structures.

---

# Suggested Initial Build Milestones

## Milestone A — Safe Foundation

Phases:

0–4

Result:

> The entire library can be indexed, fingerprinted, inspected, and browsed without modification.

---

## Milestone B — Useful Photo Manager

Phases:

5–9

Result:

> Personal Photo Archive becomes useful for everyday organisation.

---

## Milestone C — Historical Reconstruction

Phases:

6–8 plus 25–26

Result:

> Incorrect historical dates can be systematically investigated and repaired at the catalogue level.

This is likely the project's first genuinely distinctive capability.

---

## Milestone D — Archive Intelligence

Phases:

10–18

Result:

> The archive begins understanding duplicates, people, events, places, and relationships.

---

## Milestone E — Memory System

Phases:

19–30

Result:

> The photo collection becomes a searchable and increasingly contextual record of personal and family history.

---

# Recommended MVP Boundary

For the first working release, stop after:

**Phase 9.**

The MVP should contain:

- safe recursive scanner
- SQLite catalogue
- SHA-256 identity
- EXIF extraction
- thumbnails
- fast image browser
- metadata inspector
- timestamp reliability
- interpreted dates
- date confidence/provenance
- timeline
- Unplaced Memories
- albums
- tags

That is already a substantial and genuinely useful application.

Face recognition, AI, editing, graph relationships, and advanced reconstruction should remain outside the MVP until the archival foundation has survived real use.

---

# Recommended Immediate Development Order

The next engineering sequence should therefore be:

```text
01. Repository skeleton
02. Archival safety contract
03. SQLite schema
04. Photo scanner
05. Metadata extractor
06. Hash/fingerprint engine
07. Thumbnail cache
08. Basic PySide6 library UI
09. Photo inspector
10. Date evidence model
11. Timestamp reliability rules
12. Timeline
13. Unplaced Memories
14. Albums/tags
15. First full 10,000-photo test
```

Only after the complete collection can pass safely through that pipeline should we begin historical reconstruction or AI analysis.

---

# Project Philosophy

Personal Photo Archive should not attempt to manufacture certainty.

Its strongest differentiator should be its willingness to represent:

- what is known
- what is observed
- what is inferred
- why it is inferred
- how confident the system is
- what the user has personally confirmed

For an archive spanning decades, **uncertainty is not corrupted data — it is part of the historical record.**

The system should preserve that distinction from its first database schema onward.