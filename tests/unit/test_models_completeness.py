from kubesentinel.models.completeness import EvidenceCompleteness, EvidenceCompletenessMixin


class TestEvidenceCompletenessMixin:
    def test_defaults_to_complete_with_no_warnings(self):
        instance = EvidenceCompletenessMixin()
        assert instance.warnings == []
        assert instance.evidence_completeness == EvidenceCompleteness.COMPLETE

    def test_accepts_warnings_and_an_explicit_completeness(self):
        instance = EvidenceCompletenessMixin(
            warnings=["could not reach X"], evidence_completeness=EvidenceCompleteness.DEGRADED
        )
        assert instance.warnings == ["could not reach X"]
        assert instance.evidence_completeness == EvidenceCompleteness.DEGRADED

    def test_every_state_is_a_real_distinct_value(self):
        # Four states, matching MASTER_ARCHITECTURE.md's own named three
        # ("degraded / unknown / not applicable") plus the implicit
        # positive case.
        assert {
            EvidenceCompleteness.COMPLETE,
            EvidenceCompleteness.DEGRADED,
            EvidenceCompleteness.UNKNOWN,
            EvidenceCompleteness.NOT_APPLICABLE,
        } == set(EvidenceCompleteness)

    def test_subclass_can_add_its_own_fields_alongside_the_shared_ones(self):
        class _ExampleRunResult(EvidenceCompletenessMixin):
            workload_count: int

        instance = _ExampleRunResult(workload_count=3)
        assert instance.workload_count == 3
        assert instance.evidence_completeness == EvidenceCompleteness.COMPLETE
