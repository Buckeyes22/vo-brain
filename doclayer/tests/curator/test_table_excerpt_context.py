from doclayer.curator.curate import support_excerpt
from doclayer.curator.state import Evidence

HEADER = "| Node | CUDA | CPU |\n|---|---|---|\n"
ROWS = "".join(f"| node-{n} | {n * 3} | {n % 5} |\n" for n in range(1, 200))
SOURCE = "# Parity\n\n## Results\n\n" + HEADER + ROWS


def evidence(quote):
    return Evidence(source_id="s", label="parity", locator="## Results", modality="data", text=quote, quote=quote)


def test_truncated_table_excerpt_keeps_header_row():
    quote = "| node-150 | 450 | 0 |"
    excerpt = support_excerpt(evidence(quote), SOURCE, budget=600)
    assert quote in excerpt
    assert HEADER in excerpt
    assert len(excerpt) <= 600


def test_small_table_excerpt_has_single_header():
    small = "# Parity\n\n## Results\n\n" + HEADER + "| node-1 | 3 | 1 |\n"
    excerpt = support_excerpt(evidence("| node-1 | 3 | 1 |"), small, budget=2000)
    assert excerpt.count("| Node | CUDA | CPU |") == 1


RULE_ROW = "|---|---|---|\n"
HEADER_ROW = "| Node | CUDA | CPU |"


def rows_after_rule(excerpt):
    return excerpt.split(RULE_ROW, 1)[1].splitlines()


def test_truncated_table_excerpt_has_single_header_and_whole_rows():
    quote = "| node-150 | 450 | 0 |"
    excerpt = support_excerpt(evidence(quote), SOURCE, budget=600)
    assert excerpt.count(HEADER_ROW) == 1
    assert all(line.startswith("|") for line in rows_after_rule(excerpt)[:-1])
    assert rows_after_rule(excerpt)[0].startswith("|")


def test_small_budget_excerpt_has_no_cut_row_under_header():
    quote = "| node-140 | 420 | 0 |"
    excerpt = support_excerpt(evidence(quote), SOURCE, budget=300)
    assert quote in excerpt
    assert RULE_ROW + "0 | 420 | 0 |" not in excerpt
    assert excerpt.count(HEADER_ROW) == 1
    assert rows_after_rule(excerpt)[0].startswith("|")
    assert len(excerpt) <= 300


def test_truncated_window_with_header_already_inside_keeps_one_header():
    quote = "| node-2 | 6 | 2 |"
    excerpt = support_excerpt(evidence(quote), SOURCE, budget=600)
    assert quote in excerpt
    assert excerpt.count(HEADER_ROW) == 1
    assert len(excerpt) <= 600
