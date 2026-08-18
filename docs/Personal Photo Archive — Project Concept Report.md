# Personal Photo Archive
## Project Concept Report

### Working Concept

**Personal Photo Archive** is a local-first digital photography management and preservation platform designed to organise, protect, reconstruct, edit, search, and continuously grow a personal photographic collection spanning decades.

The initial collection contains approximately **10,000 digital photographs covering around 24 years**.

While recent photographs generally contain reliable EXIF metadata, many older digital-camera photographs contain incomplete or incorrect date/time information due to early cameras losing their clocks when batteries were depleted or removed.

The project therefore cannot treat embedded metadata as unquestioned truth.

Instead, Personal Photo Archive will preserve original photographic evidence while building a separate, auditable interpretation layer around every image.

---

# 1. Project Vision

The project should evolve from a conventional photo organiser into a **personal photographic memory archive**.

Its purpose is not simply to answer:

> Where is this JPEG?

It should eventually answer questions such as:

- When was this photograph probably taken?
- Who is in it?
- Where was it taken?
- What event does it belong to?
- Which photographs were taken immediately before and after it?
- Is its camera timestamp trustworthy?
- Is this file an original, duplicate, resized copy, or edited derivative?
- Are there other photographs from the same event?
- Has this photograph been edited?
- What evidence supports our interpretation of its date?
- Which memories remain unidentified or chronologically unresolved?

The system should become **more knowledgeable about the collection over time** without modifying or corrupting the historical originals.

---

# 2. Core Design Principle

## Originals Are Evidence

Original photographs and their original metadata should be treated as immutable archival evidence.

The application should therefore distinguish between:

1. **Original file**
2. **Original embedded metadata**
3. **Observed filesystem information**
4. **User-confirmed information**
5. **System-derived information**
6. **AI-assisted suggestions**
7. **Non-destructive edits**
8. **Exported derivatives**

Corrections should normally be stored in the catalogue rather than silently written over the source photograph.

This makes every interpretation reversible and auditable.

---

# 3. The Historical Metadata Problem

Older cameras frequently suffered from unreliable clocks.

A photograph may contain:

`01 January 2001 00:02:17`

while the photograph may actually have been taken several years later.

Traditional photo applications frequently assume:

`EXIF DateTimeOriginal = factual capture date`

Personal Photo Archive should instead represent historical information using:

- value
- source
- evidence
- confidence
- precision
- provenance

For example:

**Observed EXIF date:**  
01 January 2001, 00:02

**Interpreted capture date:**  
Approximately 25 December 2004

**Precision:**  
Day

**Confidence:**  
0.91

**Evidence:**
- sequential filenames
- neighbouring photographs
- folder name
- camera identity
- known Christmas event
- user confirmation

The original EXIF remains untouched.

---

# 4. Temporal Confidence Model

Dates should not require artificial precision.

Supported date representations should include:

- exact timestamp
- known date
- known month
- known year
- approximate date
- date range
- unknown

Example:

`Christmas 2004`

is more historically honest than inventing:

`25/12/2004 12:00:00`

when the exact timestamp is unknown.

Every interpreted date should additionally record its origin.

Possible sources include:

- EXIF
- filesystem timestamp
- filename sequence
- original folder structure
- manually confirmed
- event-derived
- neighbouring image sequence
- GPS evidence
- import history
- camera session
- AI-assisted inference

---

# 5. Photographic Identity

Each photograph should have a permanent internal identity independent of filename or location.

This allows:

`IMG_0034.JPG`

to later become:

`Christmas Morning 2004 - 034.JPG`

without breaking the catalogue.

A permanent photo identifier also allows the application to track:

- file movements
- renames
- duplicates
- edits
- metadata interpretations
- albums
- people
- locations
- events
- exports

---

# 6. Library Model

The project should eventually support two storage modes.

## Referenced Library

Files remain in their current directories.

The archive indexes and monitors them without relocating them.

Advantages:

- easy initial adoption
- no major file migration
- compatible with existing backups

## Managed Library

Original photographs are imported into an archive-controlled structure.

Advantages:

- predictable organisation
- stronger integrity guarantees
- easier backup
- protection against accidental modification

The first implementation should favour **referenced-library indexing**, with managed-library support added once the catalogue has proven trustworthy.

---

# 7. Immutable Original + Interpretation Layers

Conceptually, every photograph should be represented as:

```text
Memory / Narrative
        ↓
Events / Albums / People / Places
        ↓
Interpreted Metadata
        ↓
Non-Destructive Editing
        ↓
Original Metadata
        ↓
Original File
```

Higher layers may change.

The original remains immutable.

---

# 8. Catalogue Database

SQLite is suitable for the initial and likely long-term desktop implementation.

Core entities should eventually include:

- Photos
- Files
- Cameras
- Metadata observations
- Metadata interpretations
- Date evidence
- People
- Faces
- Places
- Events
- Albums
- Tags
- Edits
- Duplicates
- Derivatives
- Import sessions
- User confirmations
- Analysis jobs
- Integrity records

The database should be designed around provenance rather than simply storing one final answer.

---

# 9. Duplicate Intelligence

Duplicate management should distinguish several fundamentally different cases.

## Exact Duplicate

Identical file content.

Detected cryptographically using hashes such as SHA-256.

## Visual Duplicate

Same photograph stored using different compression or dimensions.

## Derivative

A resized, edited, cropped, colour-adjusted, or otherwise modified version of another photograph.

Instead of immediately deleting duplicates, the system should construct a **lineage relationship**.

Example:

```text
Original
 ├─ exact backup copy
 ├─ resized email copy
 ├─ colour-corrected version
 └─ social-media export
```

This prevents accidental destruction of the best surviving copy.

---

# 10. Timeline

The timeline should become a central navigation interface.

The user should be able to navigate:

`24 years → year → month → event → day → photo sequence`

Photographs whose dates are uncertain should appear separately as:

**Unplaced Memories**

As historical information is reconstructed, those photographs can progressively move into the correct position.

This transforms historical cleanup into an incremental discovery process.

---

# 11. Event Model

Events should be first-class entities.

Examples:

- Christmas 2004
- Family holiday
- Birthday party
- School event
- New house
- Wedding
- Weekend trip

An event may contain:

- title
- date or range
- location
- people
- photographs
- notes
- confidence
- evidence

One photograph may belong to several overlapping collections without duplication.

---

# 12. Albums

Albums should be virtual.

A single photograph might simultaneously belong to:

- 2005
- Christmas
- Family
- Brisbane
- Dad
- Favourite Photos
- Canon PowerShot

Albums therefore become database relationships rather than physical file copies.

---

# 13. People and Face Recognition

Later phases can introduce local facial detection and recognition.

The system should:

1. detect faces
2. group visually similar faces
3. propose identities
4. request human confirmation
5. learn from confirmation

Recognition should remain assistive rather than authoritative.

The user should always control identity confirmation.

---

# 14. Age-Aware Recognition

Because the archive covers decades, people's appearance changes significantly.

The architecture should eventually allow a person's identity to contain multiple representative age ranges.

Example:

```text
Person
 ├─ childhood
 ├─ adolescence
 ├─ young adult
 ├─ middle age
 └─ later life
```

This should provide stronger historical face matching than relying on a single modern representation.

---

# 15. Non-Destructive Editing

Photo editing should never require destructive modification of the archival original.

Supported operations can eventually include:

- rotate
- crop
- straighten
- exposure
- contrast
- brightness
- saturation
- white balance
- sharpening
- noise reduction
- red-eye correction
- restoration tools

Editing instructions should be stored separately.

The rendered result becomes:

`Original + Editing Recipe`

A flattened copy is produced only when exported.

---

# 16. Search

Traditional search should support:

- filename
- date
- camera
- folder
- tags
- rating
- event
- person
- location
- confidence level
- metadata source

Later natural-language search could support:

> Photos of Dad at Christmas before 2010.

> Show photographs of Kayla at the beach.

> Find photographs taken using the old Canon whose dates may be wrong.

> Show unresolved photographs probably belonging to 2003.

Deterministic catalogue search should remain the foundation.

AI should enhance rather than replace it.

---

# 17. Import Pipeline

Every newly discovered photograph should move through a controlled ingestion pipeline.

```text
Discover
   ↓
Fingerprint
   ↓
Duplicate Check
   ↓
Metadata Extraction
   ↓
Thumbnail Generation
   ↓
Database Registration
   ↓
Date Reliability Assessment
   ↓
Optional Analysis
   ↓
Timeline / Event Assignment
```

Every import session should be logged.

---

# 18. Integrity

Because the archive contains irreplaceable personal history, integrity is more important than convenience.

The project should eventually maintain:

- SHA-256 hashes
- original file size
- original metadata snapshot
- first-seen date
- last-known location
- corruption checks
- missing-file detection
- duplicate detection
- database backups

The software should never silently delete a photograph.

---

# 19. Backup Philosophy

The application is not itself a backup.

It should instead understand and help verify backup state.

Future functionality could identify:

- photographs with no known backup
- catalogue backups
- disconnected archive locations
- hash mismatches
- corrupted files
- missing originals

A future **Archive Health** panel could present the integrity of the entire collection.

---

# 20. Privacy

The project should remain local-first.

Photographs, faces, identities, events, and personal metadata should not require cloud processing.

Cloud AI may eventually be supported as an explicit optional capability, but the archive must remain completely functional without it.

---

# 21. AI Philosophy

AI should function as an analyst and assistant.

It may propose:

- likely people
- likely events
- likely date ranges
- descriptions
- related photographs
- location hints

It should not silently rewrite historical information.

The ideal workflow is:

`Observe → Infer → Explain → Propose → Human Confirm → Learn`

---

# 22. Initial Project Boundary

The first version should **not** attempt to become:

- Photoshop
- Google Photos
- Lightroom
- a cloud sharing service
- a social network
- an AI image generator

The initial objective is much simpler:

> Safely catalogue approximately 10,000 existing photographs without modifying the originals and establish a trusted foundation on which increasingly intelligent organisation can be built.

---

# 23. Strategic Development Order

The natural project progression is:

**Preserve → Catalogue → Understand → Reconstruct → Organise → Edit → Recognise → Remember**

This order deliberately places trust and preservation ahead of advanced intelligence.

---

# 24. Long-Term Vision

At maturity, Personal Photo Archive should behave less like an image browser and more like an interactive family memory system.

Opening an old photograph should reveal not merely pixels but its historical context:

- when it was taken
- how certain that date is
- who appears in it
- where it occurred
- what happened around it
- which event it belongs to
- what photographs came immediately before and after
- whether other versions exist
- what information was manually confirmed
- what information was inferred

Over time, the archive becomes an increasingly accurate map of decades of personal history.

The project's most important principle remains simple:

> **Never destroy evidence in order to make the archive appear more certain than it really is.**