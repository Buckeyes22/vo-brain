from doclayer.coverage import Catalog, SubjectEntry
from doclayer.routing import classify, routing_text


def test_pasted_report_is_not_new_host_documentation_request():
    c = Catalog(status="ok", revision="a" * 40, policy_version=1)
    c.subjects["doclayer"] = SubjectEntry(slug="doclayer", title="Doclayer", kind="service")
    prompt = "glm finished with this output: What's running on doclayer and worker-1?\nDocs for unknown-lib."
    result = classify(prompt, c, ["worker 1"])
    assert not result.wants_lookup
    assert not result.gap_capture_eligible


def test_fenced_and_quoted_questions_do_not_leak_subjects():
    text = routing_text(
        "Please review this code.\n```text\nwhat is running on worker-1?\n```\n> docs for a missing library"
    )
    assert "worker-1" not in text
    assert "missing library" not in text


def test_real_request_before_attached_code_is_preserved():
    assert "running on worker-1" in routing_text("What is running on worker-1?\n```text\nignored\n```")
