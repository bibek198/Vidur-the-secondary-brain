from src.normalize.common import clean_handle, clean_status, kep_id_from_ref, load_yaml
from src.normalize.keps import parse_kep
from src.realtime.events import Event


def test_clean_status_handles_typos_and_comments():
    assert clean_status("imlpemented") == "implemented"
    assert clean_status("implementable # provisional|implementable|implemented") == "implementable"
    assert clean_status("implemented (alpha)") == "implemented"
    assert clean_status("superseded") == "replaced"
    assert clean_status("provisional|implementable|implemented|deferred|rejected|withdrawn|replaced") is None


def test_clean_handle_variants_and_bots():
    assert clean_handle('"@SergeyKanzhelev"') == "sergeykanzhelev"
    assert clean_handle("@mrunalp      # overall") == "mrunalp"
    assert clean_handle("Jane Doe (@jdoe)") == "jdoe"
    assert clean_handle("k8s-ci-robot") is None
    assert clean_handle(None) is None


def test_kep_refs():
    assert kep_id_from_ref("/keps/sig-node/4438-sidecar-restart-termination") == "KEP-4438"
    assert kep_id_from_ref("KEP-753") == "KEP-753"
    assert kep_id_from_ref("n/a") is None


def test_yaml_keeps_dates_as_strings():
    assert load_yaml("creation-date: 2018-05-14")["creation-date"] == "2018-05-14"
    assert load_yaml("creation-date: 2024-13-01")["creation-date"] == "2024-13-01"  # invalid month, no crash


def test_parse_kep_minimal():
    rec = parse_kep({"kep-number": 753, "title": "Sidecar Containers", "owning-sig": "sig-node",
                     "status": "implemented", "authors": ["@A"], "approvers": ["@B"],
                     "see-also": ["/keps/sig-node/4438-sidecar-restart-termination"],
                     "replaces": ["n/a"], "milestone": {"alpha": "v1.28", "beta": None},
                     "feature-gates": [{"name": "SidecarContainers", "components": ["kubelet"]}]},
                    "keps/sig-node/753-sidecar-containers")
    assert rec["id"] == "KEP-753" and rec["status"] == "implemented"
    assert rec["authors"] == ["a"] and rec["approvers"] == ["b"]
    assert rec["see_also"] == ["KEP-4438"] and rec["replaces"] == []
    assert rec["milestones"] == {"alpha": "v1.28"}
    assert rec["feature_gates"][0]["components"] == ["kubelet"]


def test_event_id_is_deterministic_and_ignores_observed_at():
    a = Event("git", "kep_status_changed", "KEP-1", "2024-01-01T00:00:00Z", {"to": "implemented"}, "2024-01-02T00:00:00Z")
    b = Event("git", "kep_status_changed", "KEP-1", "2024-01-01T00:00:00Z", {"to": "implemented"}, "2026-10-16T00:00:00Z")
    assert a.event_id == b.event_id
