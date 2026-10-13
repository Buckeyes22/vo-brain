"""`_has_quantity` accepts the size forms real pages use and still rejects naked numbers."""

import pytest

from doclayer import verification
from doclayer.resolver import _has_quantity


@pytest.mark.parametrize(
    "text, aspect",
    [
        ("The pool holds 931.5G in total.", "storage"),
        ("Memory is 16Gi per node.", "memory"),
        ("A 16Gi RAM node.", "memory"),
        ("Capacity 1.2 TB.", "storage"),
        ("About 512 mb, 2 PiB or 3.5KiB.", "storage"),
        ("A 750G disk.", "storage"),
        ("16Gi of memory", "memory"),
        ("a 750G SSD", "storage"),
    ],
)
def test_shorthand_size_units(text, aspect):
    assert _has_quantity(text, aspect)


@pytest.mark.parametrize(
    "text",
    [
        "We have 3k storage nodes",
        "a 5G modem on the storage host",
        "We have 3 k storage nodes",
    ],
)
def test_attached_single_letter_needs_the_facets_own_size_words_not_a_noun(text):
    assert not _has_quantity(text, "storage")


@pytest.mark.parametrize(
    "text",
    [
        "We have 3 k storage nodes",
        "RAID 5 p storage",
        "Kubernetes 1.30 P storage",
        "item 3\nP storage",
        "About 3 k.",
        "storage is 5 M",
    ],
)
def test_single_letter_unit_must_be_attached_to_the_number(text):
    assert not _has_quantity(text, "storage")


def test_percent_is_a_quantity_only_beside_a_size_word():
    assert _has_quantity("The volume is 65% full.", "storage")
    assert _has_quantity("usage 65%", "storage")
    assert not _has_quantity("CPU load 95% on the host", "memory")
    assert not _has_quantity("65% of the time", "storage")
    assert not _has_quantity("Retries happen at 65% of the time.", "timeout")


@pytest.mark.parametrize(
    "text",
    [
        "Free space is 115,984,068,608.",
        "used 333824475136 on the volume",
        "The size is 2048 here.",
        "size 2048",
        "The size was 1999 here.",
        "avail 16374161408",
        "count 12345 storage",
        "free: 115,984,068,608",
    ],
)
def test_large_integers_need_a_size_word_beside_them(text):
    assert _has_quantity(text, "storage")


@pytest.mark.parametrize(
    "text",
    [
        "The service listens on port 8080.",
        "Limit 40 for the queue.",
        "Issue 12345 was closed.",
        "Dated 2026-10-06, size unknown.",
        "There are 40 items.",
        "The disk was used in 2024.",
        "free ports 8080, 8443 on the storage host",
        "ports: 8080 and 8443, size unknown",
        "PR 4096 used the storage driver",
        "issue #4096 free of charge",
        "The job used 3600 seconds of storage time",
        "CPU usage 95% on the host",
        "Allowed from 10.0.0.0/8, 4096 users",
        "free port 8080 on the storage host",
        "host 10.0.0.5:9000 size",
        "size of 10.0.0.5 is unknown",
        "commit deadbeef1234 size",
        "version 1.2.3 free",
        "size is far away and the number is then given as 12345",
    ],
)
def test_bare_numbers_years_ports_addresses_and_far_context_are_rejected(text):
    assert not _has_quantity(text, "storage")


def test_quota_has_its_own_units_and_rejects_loose_shorthand():
    assert _has_quantity("Quota is 50 requests per day.", "quota")
    assert _has_quantity("Quota 3m tokens.", "quota")
    assert _has_quantity("Quota is 40 RPM.", "quota")
    assert not _has_quantity("quota is 3 m", "quota")
    assert not _has_quantity("Quota is 40.", "quota")


def test_table_cell_under_a_unit_header_is_a_quantity():
    table = "| Mount | Free (bytes) |\n|---|---|\n| /data | 42 |\n"
    assert _has_quantity(table, "storage")
    assert _has_quantity("| Mount | Used % |\n| --- | ---: |\n| /data | 89 |\n", "storage")
    assert _has_quantity("| Mount | Size GB |\n|---|---|\n| /data | 7 |\n", "storage")


def test_table_without_a_unit_header_is_rejected():
    assert not _has_quantity("| Mount | Note |\n|---|---|\n| /data | 42 |\n", "storage")
    assert not _has_quantity("| Mount | Size |\n|---|---|\n| /data | 7 |\n", "storage")


def test_unit_header_cells_reject_versions_addresses_and_single_letters():
    assert not _has_quantity("| Mount | Free (bytes) |\n|---|---|\n| /data | 1.2.3 |\n", "storage")
    assert not _has_quantity("| Mount | Free (bytes) |\n|---|---|\n| /data | 10.0.0.5 |\n", "storage")
    for header in ("T", "K", "P", "M"):
        assert not _has_quantity(f"| Mount | {header} |\n|---|---|\n| /data | 5 |\n", "storage")


def test_neighbour_facet_guard_survives_pipes_elsewhere_in_the_text():
    mixed = "| a | b |\nmemory is 64 GB; storage is not documented"
    assert not _has_quantity(mixed, "storage")
    assert not _has_quantity("| App1 | RAM 64 GB; storage not measured |", "storage")
    assert _has_quantity("| App1 | storage 64 GB; RAM not measured |", "storage")
    assert _has_quantity("| a | b |\nmemory is 64 GB; storage is 2 TB", "storage")


def test_timeouts_do_not_accept_size_shorthand():
    assert not _has_quantity("The timeout is 30G.", "timeout")


# Shaped from the quotes the model cited for gap-cef30b744240e55b027c (storage facet).
CEF_ROWS = [
    "Gpu1 `/` and `/data` (one LV `/dev/mapper/ubuntu--vg-ubuntu--lv`, ext4, counted once) | `nvme0n1` `PCIe SSD` 931.5G, `ROTA=0` | 333,824,475,136 | 65% | yes, by 101,824,475,136",
    "App1 `/data` (`/dev/sdd1`, ext4) | virtual `QEMU HARDDISK` 953.9G, `ROTA=0` | 115,984,068,608 | 89% | no",
    "App1 `/mnt/fast` (`/dev/sdb1`) | virtual `QEMU HARDDISK` 750G, `ROTA=0` | 16,374,161,408, then 23,492,034,560 minutes later | 98% | no; shared PostgreSQL storage (`app-db`), excluded",
]


@pytest.mark.parametrize("row", CEF_ROWS)
def test_live_storage_rows_carry_a_quantity(row):
    assert _has_quantity(row, "storage")


def test_live_storage_reply_is_no_longer_rejected_for_quantity():
    path = "wiki/fleet-fast-storage-observations.md"
    body = "# Storage\n\n" + "\n\n".join(CEF_ROWS) + "\n"
    reply = dict(
        answers=True,
        reason="r",
        facets=[
            dict(
                facet="storage",
                answered=True,
                scope_preserved=True,
                evidence=[dict(path=path, quote=row) for row in CEF_ROWS],
            )
        ],
    )
    why = []
    out = verification._validate_semantic(
        reply,
        ["storage"],
        [],
        [dict(path=path, body=body, page_metadata={})],
        question="what documented SSD/NVMe capacity exists?",
        why=why,
    )
    assert out is not None and out["answers"] is True and why == []


def test_percent_is_for_size_aspects_and_never_under_a_cpu_column():
    assert _has_quantity("Quota is 80% used.", "quota")
    assert not _has_quantity("The timeout is 80% used.", "timeout")
    assert not _has_quantity("| Host | CPU % |\n|---|---|\n| x | 95 |\n", "memory")
    assert not _has_quantity("| Host | CPU % |\n|---|---|\n| x | 95 |\n", "storage")
    assert _has_quantity("| Host | Disk used % |\n|---|---|\n| x | 95 |\n", "storage")


def test_cidr_suffixes_are_not_quantities():
    assert not _has_quantity("| Net | Size (GB) |\n|---|---|\n| lan | 10.0.0.0/8 |\n", "storage")
    assert not _has_quantity("Route 192.0.2.0/24, size unknown", "storage")


def test_a_year_is_a_quantity_only_with_a_size_word_right_beside_it():
    assert _has_quantity("size 2048", "storage")
    assert _has_quantity("capacity is 2048", "storage")
    assert not _has_quantity("The disk was used in 2024.", "storage")
    assert not _has_quantity("Measured 2026 on the disk", "storage")


DU_TABLE = """Top consumers on `/` (`sudo -n du -xh --max-depth=1`, same filesystem only) [source: live check App1 2026-10-05]:

| Path | Size | Notes |
|---|---|---|
| `/home` | 33G | `/home/user/github` 12G, `.local` 4.7G, `git` 4.6G, `.config` 1.5G, `.nvm` 1.4G, `go` 1.4G, `.rustup` 1.3G |
| `/usr` | 21G | |
| `/opt` | 10G | `/opt/stacks` 8.2G, `/opt/docker` 889M; a second `du` run reported `/opt` as 11G |
| `/var` | 6.0G | `/var/lib` 4.2G (`snapd` 3.6G), `/var/log` 1.7G, `/var/cache` 174M |
| `/root` | 992M | |
| `/tmp` | 882M | |
"""

SHARD_TABLE = """Observed on SHARD9 on 2026-10-05 with `du -sh` and `find / -maxdepth 4 -type d -name 'gate-shards*'` [source: live check shard9 2026-10-05]:

| Path | Size | Notes |
| --- | --- | --- |
| `/home/user/gate-shards-final-head` | 308K | helper scripts, `evidence-sha256.txt`, `unit-results.json`, `cleanup.txt`; last modified 2026-09-27 12:52 |
| `/home/user/gate-shards-fix` | 55M | restore logs, schema/toc dumps, `resume2/` (35M); last modified 2026-09-27 10:28 |
| `/home/user/gate-shards-evidence-a1b2c3d4e` | 884K | independent copies of three completed-run artifacts |
| `/home/user/gate-shards-path-attempt-artifacts` | 1.8M | interrupted-run artifacts moved out of the checkout |
"""


def test_live_du_and_shard_tables_carry_storage_sizes_and_not_memory_sizes():
    assert _has_quantity(DU_TABLE, "storage") and _has_quantity(SHARD_TABLE, "storage")
    # table size words are per facet: a Size/Path table is not a memory table
    assert not _has_quantity(DU_TABLE, "memory") and not _has_quantity(SHARD_TABLE, "memory")


def test_a_memory_table_counts_for_memory_only():
    ram = "| Host | RAM |\n|---|---|\n| x | 64G |\n"
    assert _has_quantity(ram, "memory")
    assert not _has_quantity(ram, "storage")
    assert _has_quantity("## Heap\n\n| Service | Limit |\n|---|---|\n| jvm | 2G |\n", "memory")
    assert not _has_quantity("| Host | Disk |\n|---|---|\n| x | 64G |\n", "memory")


@pytest.mark.parametrize(
    "row",
    [
        "| `/home` | 33G | `/home/user/github` 12G, `.local` 4.7G |",
        "| `/usr` | 21G | |",
        "| `/root` | 992M | |",
        "| `/home/user/gate-shards-final-head` | 308K | helper scripts |",
        "| `/home/user/gate-shards-path-attempt-artifacts` | 1.8M | interrupted-run artifacts |",
    ],
)
def test_a_quoted_row_without_its_header_still_carries_a_size(row):
    assert _has_quantity(row, "storage")


def test_the_nearest_heading_gives_a_table_its_size_context():
    assert _has_quantity("## Storage\n\n| Path | Notes |\n|---|---|\n| `/data` | 33G of it is cache |\n", "storage")
    # the nearest heading, not the nearest non-empty line
    assert _has_quantity(
        "## Storage\n\nMeasured with du on prod.\n\n| Path | Notes |\n|---|---|\n| `/data` | 33G of it is cache |\n",
        "storage",
    )
    assert not _has_quantity("## Prices\n\n| Plan | Notes |\n|---|---|\n| basic | 33G promo |\n", "storage")
    assert not _has_quantity("## Prices\n\n| Plan | Notes |\n|---|---|\n| basic | 33G |\n", "storage")
    assert not _has_quantity("| Model | Price ($/1M tokens) |\n|---|---|\n| astra | $1M |\n", "storage")


@pytest.mark.parametrize(
    "table",
    [
        "| Network | Generation |\n|---|---|\n| modem | 5G |\n",
        "| Model | Context |\n|---|---|\n| astra | 1M |\n",
        "| Team | Users |\n|---|---|\n| ops | 3k |\n",
        "| Host | Year |\n|---|---|\n| x | 5G |\n",
        "| 5G |",
        "| modem | 5G |",
    ],
)
def test_the_cell_only_rule_needs_a_size_header_heading_path_or_size_word(table):
    assert not _has_quantity(table, "storage")


def test_a_header_less_row_counts_only_with_a_real_filesystem_path():
    assert _has_quantity("| `/home` | 33G |", "storage")
    assert _has_quantity("| `/a/b` | 33G |", "storage")
    assert _has_quantity("| /mnt/fast | 1.2T |", "storage")
    for row in (
        "| disk | 33G |",
        "| used | 3k |",
        "| plan | $10 /mo | 5G |",
        "| `/s` | 5G |",
        "| `/mo` | 5G |",
        "| Teams | count | 3k |",
        "| Users | 3k | count |",
        "| blocks | 3k |",
    ):
        assert not _has_quantity(row, "storage"), row


def test_path_rows_quoted_alone_do_not_count_for_memory():
    for row in ("| `/usr` | 21G | |", "| `/root` | 992M | |", "| `/home/user/gate-shards-fix` | 55M | restore logs |"):
        assert _has_quantity(row, "storage") and not _has_quantity(row, "memory"), row
    assert _has_quantity("| heap | 2G |", "memory")
    assert _has_quantity("| RSS | 8.9G |", "memory")
    assert not _has_quantity("| heap | 2G |", "storage")


@pytest.mark.parametrize(
    "table",
    [
        "| Host | RAM used |\n|---|---|\n| x | 5G |\n",
        "| Host | Mem free |\n|---|---|\n| x | 5G |\n",
        "## RAM\n\n| Host | Notes |\n|---|---|\n| x | 64G of it |\n",
    ],
)
def test_a_memory_table_or_heading_does_not_count_for_storage(table):
    assert not _has_quantity(table, "storage")


@pytest.mark.parametrize(
    "table",
    [
        "| Path | Size on disk |\n|---|---|\n| /data | 5G |\n",
        "## df\n\n| Mount | Notes |\n|---|---|\n| /data | 5G of it |\n",
        "## Volume\n\n| Host | Size |\n|---|---|\n| x | 5G |\n",
        "| Host | Size |\n|---|---|\n| /mnt/fast | 5G |\n",
    ],
)
def test_a_storage_table_or_heading_does_not_count_for_memory(table):
    assert not _has_quantity(table, "memory")


@pytest.mark.parametrize(
    "table",
    [
        "## Network usage\n\n| Host | Notes |\n|---|---|\n| uplink | 5G |\n",
        "| Plan | Free tier |\n|---|---|\n| basic | 5G modem |\n",
        "| Plan | Free tier |\n|---|---|\n| basic | 3k nodes |\n",
    ],
)
def test_network_and_tier_headings_never_give_size_context(table):
    assert not _has_quantity(table, "storage")


def test_a_heading_inside_a_code_fence_is_not_the_nearest_heading():
    table = "## Storage\n\n```\n# comment\n```\n\n| Plan | Notes |\n|---|---|\n| basic | 33G of it |\n"
    assert _has_quantity(table, "storage")
    assert _has_quantity(
        "## Storage\n\n~~~sh\n# comment\n~~~\n\n| Plan | Notes |\n|---|---|\n| basic | 33G of it |\n", "storage"
    )
    assert not _has_quantity(
        "## Prices\n\n```\n# disk\n```\n\n| Plan | Notes |\n|---|---|\n| basic | 33G of it |\n", "storage"
    )


@pytest.mark.parametrize(
    "text",
    [
        "free 5G plan",
        "a total of 3k nodes",
        "We offer a free 5G plan",
        "free 5G modem",
        "total 3k nodes",
        "Total: 3k users",
        "a 5G free plan",
        "5G free tier",
        "3k total users",
    ],
)
def test_free_and_total_noun_check_applies_in_both_orders(text):
    assert not _has_quantity(text, "storage")


@pytest.mark.parametrize(
    "text", ["Size of /data: 2048", "Count of blocks: 2048", "Size as reported: 2048", "Free blocks: 2048"]
)
def test_as_of_is_one_date_phrase_so_other_of_labels_are_quantities(text):
    assert _has_quantity(text, "storage")


@pytest.mark.parametrize("text", ["As of: 2024 free", "Updated as of: 2024 on the disk", "Last used: 2024"])
def test_a_date_phrase_label_makes_a_colon_year_a_year(text):
    assert not _has_quantity(text, "storage")


@pytest.mark.parametrize(
    "text",
    [
        "port 22, 512 GB free",
        "Storage issues 2 TB",
        "issue 12 used 2 TiB of disk",
        "ports 22 and 80 and 512 GB free",
    ],
)
def test_the_port_and_issue_scrubs_never_remove_a_number_with_a_unit(text):
    assert _has_quantity(text, "storage")


@pytest.mark.parametrize(
    "text",
    [
        "Last used: 2024",
        "Updated: 2024 on the disk",
        "Since: 1999, free",
        "used 3600 s of storage",
        "a 5G free plan",
        "We run 3k nodes in total",
    ],
)
def test_remaining_false_positives_are_rejected(text):
    assert not _has_quantity(text, "storage")


def test_an_attached_letter_needs_a_storage_noun_within_three_tokens():
    assert _has_quantity("a 750G disk", "storage")
    assert _has_quantity("16Gi of RAM", "memory")
    assert _has_quantity("5G used", "storage")
    assert _has_quantity("volume of 5G", "storage")
    assert not _has_quantity("a 5G free plan", "storage")
    assert not _has_quantity("We run 3k nodes in total", "storage")


@pytest.mark.parametrize(
    "text",
    [
        "374G free",
        "120G free",
        "`/data` has 120G free",
        "120G free on /data",
        "These directories total about 58M and SHARD9 root has 374G free (`df -h` 2026-10-05).",
        "total about 58M",
        "free: 33G",
    ],
)
def test_free_and_total_count_beside_an_attached_unit_unless_a_noun_follows(text):
    assert _has_quantity(text, "storage")


@pytest.mark.parametrize("text", ["Size: 2048", "Free blocks: 2048", "Used: 1999 blocks", "size 2048"])
def test_a_year_range_number_after_a_size_label_is_a_quantity(text):
    assert _has_quantity(text, "storage")


# -- table context is per column ---------------------------------------------------------------- #

INVENTORY = "| Host | Disk | RAM |\n|---|---|---|\n| x | 931.5G | 64G |\n"


def test_a_mixed_disk_and_ram_table_passes_each_facet_from_its_own_column():
    assert _has_quantity(INVENTORY, "storage")
    assert _has_quantity(INVENTORY, "memory")
    # a value only in the other resource's column never satisfies the facet
    assert not _has_quantity("| Host | Disk | RAM |\n|---|---|---|\n| x | n/a | 64G |\n", "storage")
    assert _has_quantity("| Host | Disk | RAM |\n|---|---|---|\n| x | n/a | 64G |\n", "memory")
    assert not _has_quantity("| Host | Disk | RAM |\n|---|---|---|\n| x | 931.5G | n/a |\n", "memory")


@pytest.mark.parametrize(
    "table",
    [
        "| Tier | Size |\n|---|---|\n| hot | 2T |\n",
        "| Storage tier | Size |\n|---|---|\n| hot | 2T |\n",
        "| Host | Storage tier |\n|---|---|\n| x | 2T |\n",
        "| Host | Network storage |\n|---|---|\n| x | 2T |\n",
        "| Path | Size | Notes |\n|---|---|---|\n| /x | 33G | RAM backed |\n",
        "| Path | Size | Notes |\n|---|---|---|\n| /x | 33G | network volumes |\n",
        "| Path | Size | Notes |\n|---|---|---|\n| /x | 33G | swap file |\n",
        "| Mount | Used |\n|---|---|\n| network share | 2T |\n| /data | 1T |\n",
        "## Network\n\n| Host | Size |\n|---|---|\n| uplink | 5G |\n",
        "## Swap\n\n| Host | Size |\n|---|---|\n| x | 8G |\n",
    ],
)
def test_other_columns_cells_and_headings_never_cancel_a_columns_own_context(table):
    assert _has_quantity(table, "storage")


def test_memory_context_survives_storage_words_in_other_cells():
    assert _has_quantity("## Memory\n\n| Host | Notes |\n|---|---|\n| x | 64G, /data on disk |\n", "memory")
    assert _has_quantity("| Host | Notes | RAM |\n|---|---|---|\n| x | /mnt/fast | 64G |\n", "memory")


@pytest.mark.parametrize(
    "table, aspect",
    [
        ("| Host | RAM used |\n|---|---|\n| x | 5G |\n", "storage"),  # own header names the other resource
        ("| Host | Free tier |\n|---|---|\n| x | 5G |\n", "storage"),  # own header names a tier
        ("| Host | Network usage |\n|---|---|\n| x | 5G |\n", "storage"),
        ("| Host | Disk used |\n|---|---|\n| x | 5G |\n", "memory"),
        ("## Network usage\n\n| Host | Notes |\n|---|---|\n| uplink | 5G |\n", "storage"),  # heading gives the context
        ("## RAM used\n\n| Host | Notes |\n|---|---|\n| x | 5G |\n", "storage"),
    ],
)
def test_exclusion_words_cancel_only_in_the_label_that_gave_the_context(table, aspect):
    assert not _has_quantity(table, aspect)


def test_the_free_h_shape_takes_its_context_from_the_row_label_and_the_column_headers():
    free_h = "| | total | used | free |\n|---|---|---|---|\n| Mem: | 62Gi | 10Gi | 40Gi |\n| Swap: | 8Gi | 0B | 8Gi |\n"
    # the Mem:/Swap: row labels are memory context and cancel storage
    assert _has_quantity(free_h, "memory")
    assert not _has_quantity(free_h, "storage")
    df_h = "| Filesystem | Size | Used | Avail |\n|---|---|---|---|\n| /dev/sda1 | 931G | 600G | 331G |\n"
    assert _has_quantity(df_h, "storage")
    assert not _has_quantity(df_h, "memory")  # a /dev/... row never counts for memory
    assert not _has_quantity("| Mount | Size |\n|---|---|\n| /mnt/fast | 5G |\n", "memory")


def test_a_row_label_cancels_the_other_resource_but_other_cells_still_never_do():
    both = "| Host | Used |\n|---|---|\n| Mem: | 10Gi |\n| /data | 2T |\n"
    assert _has_quantity(both, "storage")  # the /data row counts; the Mem: row does not
    assert not _has_quantity("| Host | Used |\n|---|---|\n| Mem: | 10Gi |\n", "storage")
    assert not _has_quantity("| Host | Used |\n|---|---|\n| /dev/sda1 | 10Gi |\n", "memory")
    # a RAM note in another cell of a disk row does not cancel the disk row
    assert _has_quantity("| Mount | Used | Notes |\n|---|---|---|\n| /data | 2T | RAM backed |\n", "storage")
    assert _has_quantity("| Label | Used |\n|---|---|\n| Swap: | 8Gi |\n", "memory")


@pytest.mark.parametrize(
    "table, aspect",
    [
        ("## Storage\n\n| Host | RAM |\n|---|---|\n| x | 64G |\n", "storage"),
        ("## Host sizes\n\n| Host | Disk | RAM |\n|---|---|---|\n| x | n/a | 64G |\n", "storage"),
        ("## Disk and memory\n\n| Host | Disk | RAM |\n|---|---|---|\n| x | n/a | 64G |\n", "storage"),
        ("## Memory\n\n| Host | Disk |\n|---|---|\n| x | 931.5G |\n", "memory"),
        ("## Capacity\n\n| Host | Swap |\n|---|---|\n| x | 8G |\n", "storage"),
        ("## Usage\n\n| Host | Network |\n|---|---|\n| x | 5G |\n", "storage"),
    ],
)
def test_a_column_naming_the_other_resource_never_takes_context_from_the_heading(table, aspect):
    assert not _has_quantity(table, aspect)


def test_the_heading_still_gives_context_to_columns_that_name_neither_resource():
    assert _has_quantity("## Storage\n\n| Host | Notes |\n|---|---|\n| x | 64G of it |\n", "storage")
    assert _has_quantity("## Storage\n\n| Host | Storage tier |\n|---|---|\n| x | 64G |\n", "storage")
