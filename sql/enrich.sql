-- Derives the technical meaning of each assignment from RSM's own licence type
-- designation plus the ITU band the frequency falls in.
--
-- A bare frequency tells a reader nothing. What matters is what the assignment
-- is FOR: a bi-directional microwave link, a satellite earth station, a simplex
-- land mobile channel, a repeater. RSM already encodes that in `licenceType`, so
-- this groups their 55 designations into services rather than inventing a
-- taxonomy, and reads the duplex arrangement out of the same string.
--
-- Maintenance: `service` must never be 'Unclassified'. The pipeline counts
-- unclassified rows on every run and reports them in the job summary, so a new
-- RSM licence type shows up as a warning instead of silently landing in a
-- catch-all bucket. When one appears, add it to the CASE below.
--
-- Ordering matters in both CASE expressions:
--   * 'Fixed Television Outside Broadcast …' must be tested before 'Fixed%'.
--   * 'General user licence (Spectrum)' must be tested before '%(spectrum)%'
--     so both general user licences group together.
--   * '… - Mobile Transmit' must be tested before '%repeater%', because on a
--     repeater pair that row describes the mobile side specifically.

CREATE OR REPLACE VIEW enriched AS
SELECT
    *,
    TRY_CAST(frequency AS DOUBLE) AS freq_mhz,
    CASE
        WHEN licenceType ILIKE '%amateur%'                       THEN 'Amateur'
        WHEN licenceType ILIKE 'aero%'                           THEN 'Aeronautical'
        WHEN licenceType ILIKE 'maritime%'                       THEN 'Maritime'
        WHEN licenceType ILIKE 'meteorolog%'                     THEN 'Meteorological'
        WHEN licenceType ILIKE 'satellite%'
          OR licenceType ILIKE 'space operations%'               THEN 'Satellite & Space'
        WHEN licenceType ILIKE '%radar%'
          OR licenceType ILIKE 'radiodet%'
          OR licenceType ILIKE 'two frequency%'                  THEN 'Radiodetermination'
        WHEN licenceType ILIKE 'telemetry%'                      THEN 'Telemetry & Telecommand'
        WHEN licenceType ILIKE 'paging%'                         THEN 'Paging'
        WHEN licenceType ILIKE 'fixed television outside%'       THEN 'Outside Broadcast'
        WHEN licenceType IN ('VHF FM', 'MF AM', 'UHF TV')
          OR licenceType ILIKE 'hf am%'                          THEN 'Broadcasting'
        WHEN licenceType ILIKE 'fixed%'                          THEN 'Fixed Link'
        WHEN licenceType ILIKE 'land%'
          OR licenceType ILIKE 'prs%'                            THEN 'Land Mobile'
        WHEN licenceType ILIKE 'general user licence%'           THEN 'General User Licence'
        WHEN licenceType ILIKE '%(spectrum)%'
          OR licenceType ILIKE 'managed spectrum park'           THEN 'Spectrum Licence'
        ELSE 'Unclassified'
    END AS service,
    CASE
        WHEN licenceType ILIKE '%mobile transmit%'  THEN 'Mobile Transmit'
        WHEN licenceType ILIKE '%bi-directional%'   THEN 'Bi-directional'
        WHEN licenceType ILIKE '%uni-directional%'  THEN 'Uni-directional'
        WHEN licenceType ILIKE '%simplex%'          THEN 'Simplex'
        WHEN licenceType ILIKE '%repeater%'
          OR licenceType ILIKE '%digipeater%'       THEN 'Repeater'
        WHEN licenceType ILIKE '%base%'             THEN 'Base Station'
        WHEN licenceType ILIKE '%beacon%'           THEN 'Beacon'
        ELSE 'Unspecified'
    END AS link_mode,
    -- Standard ITU band designations, so the buckets mean the same thing here
    -- as they do in any other spectrum reference.
    CASE
        WHEN TRY_CAST(frequency AS DOUBLE) IS NULL  THEN 'Unknown'
        WHEN TRY_CAST(frequency AS DOUBLE) < 0.03   THEN 'VLF (<30 kHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 0.3    THEN 'LF (30-300 kHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 3.0    THEN 'MF (300 kHz-3 MHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 30.0   THEN 'HF (3-30 MHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 300.0  THEN 'VHF (30-300 MHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 3000.0 THEN 'UHF (300 MHz-3 GHz)'
        WHEN TRY_CAST(frequency AS DOUBLE) < 30000.0 THEN 'SHF (3-30 GHz)'
        ELSE                                             'EHF (>30 GHz)'
    END AS band,
    -- The '#' suffix on a channel marks the second leg of a paired assignment.
    -- Measured against the current register: 'Land Mobile - Mobile Transmit'
    -- rows are 6,206 hash to 9 plain, while 'Land Repeater' rows are 10,146
    -- plain to 3 hash — so the hash side is the mobile/return leg and the plain
    -- side is the base. Fixed bi-directional types split roughly 50/50, being
    -- the two ends of one link.
    --
    -- This is an empirical read of the naming convention, not a citation.
    -- RSM's PIB 38 is the authority on what the convention formally means, and
    -- on which leg is the uplink; check it before labelling these as up/down.
    CASE
        WHEN channel LIKE '%#' THEN 'Return leg (#)'
        ELSE 'Primary leg'
    END AS pair_leg,
    CASE
        WHEN TRY_CAST(frequency AS DOUBLE) IS NULL  THEN 99
        WHEN TRY_CAST(frequency AS DOUBLE) < 0.03   THEN 1
        WHEN TRY_CAST(frequency AS DOUBLE) < 0.3    THEN 2
        WHEN TRY_CAST(frequency AS DOUBLE) < 3.0    THEN 3
        WHEN TRY_CAST(frequency AS DOUBLE) < 30.0   THEN 4
        WHEN TRY_CAST(frequency AS DOUBLE) < 300.0  THEN 5
        WHEN TRY_CAST(frequency AS DOUBLE) < 3000.0 THEN 6
        WHEN TRY_CAST(frequency AS DOUBLE) < 30000.0 THEN 7
        ELSE                                             8
    END AS band_order
FROM licences;
