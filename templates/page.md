---
schema_version: 1
privacy: internal
source_origin: operator
source_lineage:
  - source:describe-where-this-came-from
tags:
  - area
  - topic
updated: 2026-06-30
concept_id: c-16752823
address: c-000011
title: page
---

# Page title

> Copy this template for new pages. The `privacy:` line is mandatory and enforced by the
> memory broker — omit it and the page is treated as `sensitive` (hidden from authoring and
> code-runner profiles). Pick the **lowest** taint that is still truthful:
> `public` (shareable) · `internal` (homelab-internal, the default) · `sensitive`
> (operator-only) · `credential` (references a secret — stays strictly local; never paste the
> secret itself).

## Section

One topic per section so retrieval can read just the relevant slice. Link related pages with
`[[wikilinks]]`; dangling links are fine (they mark "write this later").
