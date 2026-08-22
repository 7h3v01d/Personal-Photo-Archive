-- Reconstruction evidence binding (schema v12).
--
-- Revision binding (v11) answers "did the bytes change?". It does NOT answer
-- "did the evidence used to reconstruct those bytes change?" — e.g. a newer,
-- more-specific human anchor supersedes the old one while the bytes are
-- identical. A confirmation is a decision about a specific EVIDENCE STATE, not
-- just a byte sequence.
--
-- We record a deterministic fingerprint of the canonical evidence payload
-- (engine version, per-frame reliability, sequence, reset-group membership and
-- device-identity strength, resolved anchor/GPS values, and — for offset
-- propagation — the same for the group's members). Freshness then requires BOTH
-- the same current revision AND the same evidence fingerprint. The fingerprint
-- is frozen at the moment of decision; if today's evidence differs, the decision
-- is evidence-stale and no longer authoritative until re-reviewed.

ALTER TABLE reconstructions ADD COLUMN evidence_fingerprint TEXT;
