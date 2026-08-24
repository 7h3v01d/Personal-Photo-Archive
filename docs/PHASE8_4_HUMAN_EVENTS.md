# Phase 8.4 — Human Event Identity

Phase 8.3 clusters are derived browsing context. Phase 8.4 introduces a separate,
durable human interpretation: a named Event.

## Authority boundary

Naming a cluster does **not** make the cluster algorithm authoritative and does
not alter dates. The event snapshots only the cluster's authoritative seed
members at the moment the user names it. Range/tentative contextual photos are
excluded unless a later explicit human-membership workflow adds them.

An Event has:

- stable UUID identity;
- owning library;
- human-authored name and optional note;
- start/end span copied from the source cluster at creation time;
- source cluster key for provenance;
- explicit durable photo-membership snapshot.

If chronology later changes, the human event survives and its membership does
not silently drift. Current Timeline lane state is still shown per member, so an
event photo can later become range/unplaced without the Event laundering that
chronology back into authority.

## UI

Timeline now includes an **Events** scale. In **Clusters**, use **Name this
cluster…** to create an Event. Existing cluster-linked events can be renamed.
The Events scale remains available even if the original provisional cluster
later disappears.

## Safety

Event creation/rename performs no EXIF writes, no photo writes, no anchor or
reconstruction changes, and no date inference. Cross-library event membership
is rejected both in the API and by a database trigger.
