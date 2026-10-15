from kubesentinel.diagnostics.rules.node import NodeConditionRule
from kubesentinel.incidents.health import has_failure_signal, is_confirmed_healthy
from kubesentinel.models.diagnosis import Confidence, ReasonCode
from kubesentinel.observer.inspector import node_evidence, resource_ref_from_node
from tests.unit.factories import make_node, node_condition


def test_not_ready_node_fires_high_confidence():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "False", "KubeletNotReady")]))
    hypotheses = NodeConditionRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].reason_code == ReasonCode.NODE_UNSCHEDULABLE
    assert hypotheses[0].confidence == Confidence.HIGH


def test_ready_node_produces_no_hypotheses():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "True")]))
    assert NodeConditionRule().evaluate(evidence) == []


def test_memory_pressure_fires_high_confidence():
    evidence = node_evidence(
        make_node(conditions=[node_condition("Ready", "True"), node_condition("MemoryPressure", "True")])
    )
    hypotheses = NodeConditionRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert "MemoryPressure" in hypotheses[0].explanation


def test_cordoned_node_fires_medium_confidence():
    evidence = node_evidence(make_node(unschedulable=True))
    hypotheses = NodeConditionRule().evaluate(evidence)
    assert len(hypotheses) == 1
    assert hypotheses[0].confidence == Confidence.MEDIUM


def test_not_ready_is_a_failure_signal():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "False")]))
    assert has_failure_signal(evidence) is True


def test_ready_node_is_not_a_failure_signal():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "True")]))
    assert has_failure_signal(evidence) is False


def test_cordoning_alone_is_not_a_failure_signal():
    # Cordoning is routine maintenance far more often than it's a problem -- must
    # not open an incident on its own.
    evidence = node_evidence(make_node(unschedulable=True, conditions=[node_condition("Ready", "True")]))
    assert has_failure_signal(evidence) is False


def test_ready_node_without_pressure_is_confirmed_healthy():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "True")]))
    assert is_confirmed_healthy(evidence) is True


def test_not_ready_node_is_not_confirmed_healthy():
    evidence = node_evidence(make_node(conditions=[node_condition("Ready", "False")]))
    assert is_confirmed_healthy(evidence) is False


def test_cleared_pressure_is_confirmed_healthy_using_latest_observation():
    # An earlier pressure spike that has since cleared must not keep this open forever.
    first = node_evidence(make_node(conditions=[node_condition("Ready", "True"), node_condition("MemoryPressure", "True")]))
    second = node_evidence(make_node(conditions=[node_condition("Ready", "True"), node_condition("MemoryPressure", "False")]))
    assert is_confirmed_healthy(first + second) is True


def test_resource_ref_from_node():
    ref = resource_ref_from_node(make_node(name="node-1", uid="abc"))
    assert ref.kind == "Node"
    assert ref.name == "node-1"
    assert ref.uid == "abc"
