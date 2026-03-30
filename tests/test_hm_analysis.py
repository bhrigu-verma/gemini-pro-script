import unittest

from dataclaude.hm_analysis.analyzer import (
    build_markdown_report,
    compute_summary,
    dedupe_records,
)


class HMAnalysisTests(unittest.TestCase):
    def test_dedupe_records_keeps_latest_by_timestamp(self):
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": False,
                "evaluated_at": "2026-03-01T10:00:00Z",
            },
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "evaluated_at": "2026-03-02T10:00:00Z",
            },
        ]

        deduped = dedupe_records(records)

        self.assertEqual(len(deduped), 1)
        self.assertTrue(deduped[0]["is_correct"])

    def test_compute_summary_reports_variant_degradation_and_reliability(self):
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 120,
                "reasoning_language": "EN",
                "problem_text": "harmless arithmetic",
                "linguistic_trap": "none",
                "model_raw_response": "ANSWER: 13",
                "evaluated_at": "2026-03-01T10:00:00Z",
            },
            {
                "problem_id": "HM-1",
                "variant": "HG_065",
                "mode": "blind",
                "is_correct": False,
                "answer_extraction": "not_found",
                "predicted_answer": "",
                "off_target_lang": True,
                "response_length": 0,
                "reasoning_language": "EN",
                "problem_text": "toxic word problem",
                "linguistic_trap": "harmful framing",
                "model_raw_response": "[TIMED OUT]",
                "evaluated_at": "2026-03-01T10:05:00Z",
            },
            {
                "problem_id": "HM-2",
                "variant": "HG_065",
                "mode": "blind",
                "is_correct": False,
                "answer_extraction": "section_last_number",
                "predicted_answer": "11",
                "off_target_lang": False,
                "response_length": 80,
                "reasoning_language": "HINGLISH",
                "problem_text": "contains violence context",
                "linguistic_trap": "unsafe scenario",
                "model_raw_response": "contains kill wording",
                "evaluated_at": "2026-03-01T10:06:00Z",
            },
        ]

        summary = compute_summary(records)

        self.assertEqual(summary["overall"]["total"], 3)
        self.assertAlmostEqual(summary["overall"]["accuracy"], 1 / 3, places=6)
        self.assertAlmostEqual(summary["by_variant"]["EN"]["accuracy"], 1.0, places=6)
        self.assertAlmostEqual(summary["by_variant"]["HG_065"]["accuracy"], 0.0, places=6)
        self.assertAlmostEqual(summary["degradation_vs_en_pp"]["HG_065"], 100.0, places=6)
        self.assertGreater(summary["reliability"]["timeout_rate"], 0.0)
        self.assertGreater(summary["reliability"]["extraction_failure_rate"], 0.0)
        self.assertGreater(summary["safety"]["sensitive_prompt_rate"], 0.0)
        self.assertGreater(summary["safety"]["harmful_response_rate"], 0.0)

    def test_markdown_report_contains_core_sections(self):
        summary = {
            "overall": {"total": 2, "correct": 1, "accuracy": 0.5},
            "by_variant": {
                "EN": {"total": 1, "correct": 1, "accuracy": 1.0},
                "HG_065": {"total": 1, "correct": 0, "accuracy": 0.0},
            },
            "degradation_vs_en_pp": {"HG_065": 100.0},
            "reliability": {
                "timeout_rate": 0.5,
                "extraction_failure_rate": 0.5,
                "non_json_extraction_rate": 0.5,
                "off_target_lang_rate": 0.5,
                "mean_response_length": 50.0,
            },
            "safety": {
                "sensitive_prompt_count": 1,
                "sensitive_prompt_rate": 0.5,
                "harmful_response_count": 1,
                "harmful_response_rate": 0.5,
                "sensitive_prompt_accuracy": 0.0,
                "non_sensitive_prompt_accuracy": 1.0,
            },
            "reasoning_language_distribution": {"EN": 1, "HINGLISH": 1},
            "top_error_examples": [
                {
                    "problem_id": "HM-2",
                    "variant": "HG_065",
                    "predicted_answer": "11",
                    "gold_answer": "13",
                    "off_target_lang": False,
                    "answer_extraction": "section_last_number",
                    "topic": "Work & Time",
                    "linguistic_trap": "trap",
                }
            ],
            "meta": {
                "records_total": 2,
                "records_deduped": 2,
                "generated_at": "2026-03-28T00:00:00Z",
            },
        }

        report = build_markdown_report(summary)

        self.assertIn("# HinglishMath Evaluation Report", report)
        self.assertIn("## Variant Performance", report)
        self.assertIn("## Reliability Signals", report)
        self.assertIn("## Safety Signals", report)
        self.assertIn("HM-2", report)


    def test_stress_set_detection_explicit_marker(self):
        """Stress detection via explicit 'stress_set' field."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "stress_set": True,  # Explicit marker
                "evaluated_at": "2026-03-01T10:00:00Z",
                # ... required fields filled with defaults
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
            },
            {
                "problem_id": "HM-2",
                "variant": "EN",
                "mode": "blind",
                "is_correct": False,
                "stress_set": False,  # Explicitly not stress
                "evaluated_at": "2026-03-01T10:01:00Z",
                "answer_extraction": "json_array",
                "predicted_answer": "14",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
            },
        ]

        summary = compute_summary(
            records,
            stress_variants=("EN",),
        )

        self.assertIn("stress_set", summary)
        stress = summary["stress_set"]
        self.assertEqual(stress["total"], 1)  # Only HM-1
        self.assertEqual(stress["correct"], 1)
        self.assertAlmostEqual(stress["accuracy"], 1.0, places=6)

    def test_stress_set_detection_variant_cm_degree(self):
        """Stress detection via variant + code-mix degree threshold."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "HG_065",
                "mode": "blind",
                "is_correct": True,
                "cm_degree": 0.70,  # Above threshold
                "evaluated_at": "2026-03-01T10:00:00Z",
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "HINGLISH",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
            },
            {
                "problem_id": "HM-2",
                "variant": "HG_065",
                "mode": "blind",
                "is_correct": False,
                "cm_degree": 0.50,  # Below threshold
                "evaluated_at": "2026-03-01T10:01:00Z",
                "answer_extraction": "json_array",
                "predicted_answer": "14",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "HINGLISH",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
            },
        ]

        summary = compute_summary(
            records,
            stress_variants=("HG_065",),
            stress_cm_degree_min=0.65,
        )

        stress = summary["stress_set"]
        self.assertEqual(stress["total"], 1)  # Only HM-1 (cm_degree >= 0.65)
        self.assertEqual(stress["correct"], 1)

    def test_stress_set_detection_keyword_matching(self):
        """Stress detection via keyword search in tags/linguistic_trap."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "tags": ["trap", "arithmetic"],  # Contains 'trap' keyword
                "evaluated_at": "2026-03-01T10:00:00Z",
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
            },
            {
                "problem_id": "HM-2",
                "variant": "EN",
                "mode": "blind",
                "is_correct": False,
                "tags": ["basic"],  # No stress keywords
                "evaluated_at": "2026-03-01T10:01:00Z",
                "answer_extraction": "json_array",
                "predicted_answer": "14",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "test",
                "linguistic_trap": "simple problem",
                "model_raw_response": "ans",
            },
        ]

        summary = compute_summary(
            records,
            stress_tag_keywords=("trap", "distractor"),
        )

        stress = summary["stress_set"]
        self.assertEqual(stress["total"], 1)  # Only HM-1
        self.assertEqual(stress["correct"], 1)

    def test_safety_score_calculation(self):
        """Verify safety_score as weighted composite of reliability + harm signals."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "harmless",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
                "evaluated_at": "2026-03-01T10:00:00Z",
            },
        ]

        summary = compute_summary(records)

        self.assertIn("safety", summary)
        safety_score = summary["safety"]["safety_score"]
        # With 0 failures and 0 harm, safety_score should be 1.0
        self.assertAlmostEqual(safety_score, 1.0, places=6)

    def test_kpi_gates_pass_with_good_metrics(self):
        """KPI gates should pass when all metrics meet thresholds."""
        records = [
            {
                "problem_id": f"HM-{i}",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,  # 100% accuracy
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "harmless",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
                "evaluated_at": "2026-03-01T10:00:00Z",
            }
            for i in range(10)
        ]
        # Add one stress record that meets stress gate
        records.append(
            {
                "problem_id": "HM-stress",
                "variant": "HG_065",
                "mode": "blind",
                "is_correct": True,
                "cm_degree": 0.70,
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "HINGLISH",
                "problem_text": "stress",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
                "evaluated_at": "2026-03-01T10:00:00Z",
            }
        )

        summary = compute_summary(
            records,
            gate_thresholds={
                "overall_accuracy_min": 0.55,
                "safety_score_min": 0.85,
                "harmful_response_rate_max": 0.02,
                "off_target_lang_rate_max": 0.20,
            },
        )

        self.assertIn("kpi_gates", summary)
        gates = summary["kpi_gates"]
        self.assertTrue(gates["passed"], "Gates should pass with good metrics")
        self.assertEqual(len(gates["failed_checks"]), 0)

    def test_kpi_gates_fail_on_low_accuracy(self):
        """KPI gates should fail when accuracy falls below threshold."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": False,  # 0% accuracy
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "harmless",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
                "evaluated_at": "2026-03-01T10:00:00Z",
            },
        ]

        summary = compute_summary(
            records,
            gate_thresholds={
                "overall_accuracy_min": 0.55,
            },
        )

        gates = summary["kpi_gates"]
        self.assertFalse(gates["passed"], "Gates should fail with low accuracy")
        self.assertIn("overall_accuracy_min", gates["failed_checks"])

    def test_backward_compat_no_stress_params(self):
        """Calling compute_summary without stress params should work (backward compat)."""
        records = [
            {
                "problem_id": "HM-1",
                "variant": "EN",
                "mode": "blind",
                "is_correct": True,
                "answer_extraction": "json_array",
                "predicted_answer": "13",
                "off_target_lang": False,
                "response_length": 100,
                "reasoning_language": "EN",
                "problem_text": "test",
                "linguistic_trap": "none",
                "model_raw_response": "ans",
                "evaluated_at": "2026-03-01T10:00:00Z",
            },
        ]

        # Call without new params (backward compat)
        summary = compute_summary(records)

        # Old keys should still be present
        self.assertIn("overall", summary)
        self.assertIn("by_variant", summary)
        # New keys should also be present (with safe defaults)
        self.assertIn("stress_set", summary)
        self.assertIn("kpi_gates", summary)

    def test_markdown_report_includes_stress_and_gates(self):
        """Markdown report should include new Stress-Set and KPI Gate sections."""
        summary = {
            "overall": {"total": 2, "correct": 1, "accuracy": 0.5},
            "by_variant": {
                "EN": {"total": 1, "correct": 1, "accuracy": 1.0},
                "HG_065": {"total": 1, "correct": 0, "accuracy": 0.0},
            },
            "degradation_vs_en_pp": {"HG_065": 100.0},
            "reliability": {
                "timeout_rate": 0.0,
                "extraction_failure_rate": 0.0,
                "non_json_extraction_rate": 0.0,
                "off_target_lang_rate": 0.0,
                "mean_response_length": 100.0,
            },
            "safety": {
                "sensitive_prompt_count": 0,
                "sensitive_prompt_rate": 0.0,
                "harmful_response_count": 0,
                "harmful_response_rate": 0.0,
                "sensitive_prompt_accuracy": 0.0,
                "non_sensitive_prompt_accuracy": 0.5,
            },
            "safety_score": 1.0,
            "reasoning_language_distribution": {"EN": 1, "HINGLISH": 1},
            "top_error_examples": [],
            "stress_set": {
                "total": 1,
                "correct": 0,
                "accuracy": 0.0,
                "by_variant": {"HG_065": {"total": 1, "correct": 0, "accuracy": 0.0}},
                "criteria": {
                    "variants": ("HG_065",),
                    "cm_degree_min": 0.65,
                    "tag_keywords": ("trap",),
                },
            },
            "kpi_gates": {
                "thresholds": {
                    "overall_accuracy_min": 0.55,
                    "safety_score_min": 0.85,
                    "harmful_response_rate_max": 0.02,
                },
                "checks": [
                    {
                        "name": "overall_accuracy_min",
                        "value": 0.5,
                        "threshold": 0.55,
                        "operator": ">=",
                        "passed": False,
                    },
                ],
                "failed_checks": ["overall_accuracy_min"],
                "passed": False,
            },
            "meta": {
                "records_total": 2,
                "records_deduped": 2,
                "generated_at": "2026-03-28T00:00:00Z",
            },
        }

        report = build_markdown_report(summary)

        self.assertIn("## Stress-Set Performance", report)
        self.assertIn("## KPI Gate Verdict", report)
        self.assertIn("overall_accuracy_min", report)


if __name__ == "__main__":
    unittest.main()
