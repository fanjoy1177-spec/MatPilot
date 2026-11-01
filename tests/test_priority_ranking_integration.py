"""Actual Service/CLI/MCP paths for a fixed learned ranker, using CPU fixtures.

These synthetic CIFs validate interface behavior only, not material performance.
"""
import asyncio
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matpilot.cli import main
from matpilot.config import Settings
from matpilot.server import make_server
from matpilot.service import Service
from tests import test_mixed_valence_integration as mixed_fixtures
from tests import test_triage_integration as triage_fixtures


class PriorityRankingIntegrationTests(unittest.TestCase):
    collection = mixed_fixtures.MixedValenceIntegrationTests.collection
    fixture_job = triage_fixtures.TriageIntegrationTests.fixture_job
    registered = triage_fixtures.TriageIntegrationTests.registered

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root / "jobs", backend="demo"))

    def tearDown(self):
        self.temp.cleanup()

    def configured_service(self, config, value):
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["automatic_ranking_policy"] = value
        config.write_text(json.dumps(payload), encoding="utf-8")
        return Service(Settings.load(str(config)))

    @staticmethod
    def structured(result):
        value = result[1] if isinstance(result, tuple) else result
        if isinstance(value, dict):
            return value
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_configuration_default_validation_load_and_snapshot(self):
        self.assertEqual(Settings().automatic_ranking_policy, "ionic_mixed_valence_v1")
        for invalid in ("auto", "structural_v1", "unregistered", None, True, 1, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "automatic_ranking_policy"):
                    Settings(data_dir=self.root, automatic_ranking_policy=invalid)
                config = self.root / "invalid-config.json"
                target = self.root / "not-created"
                config.write_text(json.dumps({"data_dir": str(target), "automatic_ranking_policy": invalid}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "automatic_ranking_policy"):
                    Settings.load(str(config))
                self.assertFalse(target.exists())
        original = Settings(data_dir=self.root, automatic_ranking_policy="ionic_ranked_v2")
        self.assertEqual(Settings(**original.snapshot()).automatic_ranking_policy, "ionic_ranked_v2")

    def test_default_auto_is_unchanged_and_explicit_ranker_keeps_the_same_gate(self):
        job_id = self.fixture_job()
        old = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True,
                                                selection_policy="ionic_mixed_valence_v1")
        automatic = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True)
        ranked = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True,
                                                   selection_policy="ionic_ranked_v2")
        self.assertEqual(automatic["effective_selection_policy"], "ionic_mixed_valence_v1")
        self.assertEqual(automatic["records"], old["records"])
        self.assertEqual(automatic["all_candidate_audit_sha256"], old["all_candidate_audit_sha256"])
        self.assertEqual(ranked["stage_counts"], old["stage_counts"])
        self.assertEqual(ranked["relaxation_triage_policy"], old["relaxation_triage_policy"])
        old_rows = {r["candidate_id"]: r for r in old["records"]}
        for row in ranked["records"]:
            previous = old_rows[row["candidate_id"]]
            self.assertEqual(row["relaxation_priority"], previous["relaxation_priority"])
            self.assertEqual(row["structural_representative_qualified"], previous["structural_representative_qualified"])
            if row["selected"]:
                self.assertEqual(row["relaxation_priority"]["tier"], "priority_relaxation")
        resolution = ranked["selection_policy_resolution"]
        self.assertTrue(resolution["training_property_labels_used"])
        self.assertFalse(resolution["inference_property_labels_used"])
        self.assertFalse(resolution["calibrated_stability_probability"])
        self.assertTrue(ranked["priority_ranking"]["performance_labels_used_for_training"])
        self.assertFalse(ranked["priority_ranking"]["runtime_property_labels_used"])
        # Running v2 must not mutate the reproducible explicit dev11 route.
        after = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True,
                                                  selection_policy="ionic_mixed_valence_v1")
        self.assertEqual(old, after)

    def test_operator_config_controls_auto_and_capability_without_changing_explicit_replay(self):
        service, config = self.collection("Fe3O4")
        default = service.get_capabilities()["candidate_recommendation"]
        self.assertEqual(default["automatic_ranking_policy"], "ionic_mixed_valence_v1")
        service = self.configured_service(config, "ionic_ranked_v2")
        cap = service.get_capabilities()["candidate_recommendation"]
        self.assertEqual(cap["automatic_ranking_policy"], "ionic_ranked_v2")
        self.assertEqual(cap["selection_policies"]["auto"]["ionic_policy"], "ionic_ranked_v2")
        self.assertIn("ionic_ranked_v2", cap["selection_policies"])
        automatic = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True)
        explicit = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                               selection_policy="ionic_ranked_v2")
        self.assertEqual(automatic["effective_selection_policy"], "ionic_ranked_v2")
        self.assertEqual(automatic["records"], explicit["records"])
        self.assertEqual(automatic["all_candidate_audit_sha256"], explicit["all_candidate_audit_sha256"])
        score = automatic["records"][0]["ranking_evidence"]["uncalibrated_score"]
        self.assertIsInstance(score, (int, float))
        self.assertTrue(automatic["selection_policy_resolution"]["property_prediction_performed"])
        self.assertEqual(automatic["selection_policy_resolution"]["prediction_scope"], "uncalibrated_low_hull_proxy_relative_score")
        old = service.recommend_candidates(collection_id="fixture", selection_policy="ionic_mixed_valence_v1")
        self.assertNotIn("priority_ranking", old)
        self.assertFalse(old["selection_policy_resolution"]["property_prediction_performed"])
        self.assertEqual(service.store.jobs(), [])

    def test_unsupported_ionic_system_still_falls_back_and_explicit_ranker_rejects_it(self):
        service, config = self.collection("FeNi")
        service = self.configured_service(config, "ionic_ranked_v2")
        automatic = service.recommend_candidates(collection_id="fixture", include_all_records=True)
        self.assertEqual(automatic["effective_selection_policy"], "structural_v1")
        self.assertTrue(automatic["selection_policy_resolution"]["fallback_applied"])
        self.assertFalse(automatic["selection_policy_resolution"]["chemical_feasibility_policy_invoked"])
        self.assertFalse(automatic["selection_policy_resolution"]["property_prediction_performed"])
        self.assertNotIn("priority_ranking", automatic)
        with self.assertRaisesRegex(ValueError, "does not support"):
            service.recommend_candidates(collection_id="fixture", selection_policy="ionic_ranked_v2")

    def test_supported_but_untrained_anion_preserves_dev11_order_with_ranking_warning(self):
        service, _ = self.collection("NaCl")
        old = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                           selection_policy="ionic_mixed_valence_v1")
        ranked = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                              selection_policy="ionic_ranked_v2")
        self.assertEqual(ranked["selected_candidate_ids"], old["selected_candidate_ids"])
        self.assertEqual(ranked["stage_counts"], old["stage_counts"])
        self.assertEqual(ranked["records"][0]["relaxation_priority"], old["records"][0]["relaxation_priority"])
        self.assertTrue(ranked["priority_ranking"]["fallback_reasons"])
        self.assertFalse(ranked["selection_policy_resolution"]["property_prediction_performed"])
        self.assertEqual(ranked["effective_selection_policy"], "ionic_ranked_v2")

    def test_caller_preference_and_bad_sha_cannot_promote_unqualified_candidates(self):
        service, _ = self.collection("FeNiO")
        registration = service.settings.candidate_collections["fixture"]
        path = Path(registration["manifest_path"])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(priority_candidate_ids=["fixture"], priority_selection_basis="explicit operator review preference")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        registration["manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        ranked = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                              selection_policy="ionic_ranked_v2")
        self.assertEqual(ranked["selected_count"], 0)
        self.assertEqual(ranked["stage_counts"]["shortfall_count"], 1)
        self.assertEqual(ranked["records"][0]["relaxation_priority"]["tier"], "manual_review")
        manifest["records"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        registration["manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        invalid = service.recommend_candidates(collection_id="fixture", limit=1, include_all_records=True,
                                               selection_policy="ionic_ranked_v2")
        self.assertEqual(invalid["selected_count"], 0)
        self.assertEqual(invalid["excluded_reason_counts"], {"artifact_sha256_mismatch": 1})
        self.assertIsNone(invalid["records"][0].get("ranking_evidence", {}).get("uncalibrated_score"))

    def test_cli_and_real_fastmcp_use_configured_auto_and_explicit_policy_consistently(self):
        service, config = self.collection("Fe3O4")
        service = self.configured_service(config, "ionic_ranked_v2")
        output = io.StringIO()
        with patch("sys.argv", ["matpilot", "--config", str(config), "recommend-collection", "fixture",
                               "--limit", "1", "--selection-policy", "ionic_ranked_v2", "--include-all-records"]), contextlib.redirect_stdout(output):
            main()
        cli = json.loads(output.getvalue())
        self.assertEqual(cli["effective_selection_policy"], "ionic_ranked_v2")

        async def exercise():
            app = make_server(service.settings)
            tool = next(t for t in await app.list_tools() if t.name == "recommend_candidates")
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertEqual(tool.inputSchema["properties"]["selection_policy"]["default"], "auto")
            caps = self.structured(await app.call_tool("get_capabilities", {}))
            self.assertEqual(caps["candidate_recommendation"]["automatic_ranking_policy"], "ionic_ranked_v2")
            native = self.structured(await app.call_tool("recommend_candidates", {
                "collection_id": "fixture", "limit": 1, "include_all_records": True}))
            self.assertEqual(native["effective_selection_policy"], "ionic_ranked_v2")
            self.assertEqual(native["records"], cli["records"])
            self.assertEqual(native["all_candidate_audit_sha256"], cli["all_candidate_audit_sha256"])
        asyncio.run(exercise())
        self.assertEqual(service.store.jobs(), [])

    def test_report_cli_and_mcp_include_scores_warnings_and_keep_other_artifacts(self):
        job_id = self.fixture_job()
        ordinary = self.service.build_report(job_id)
        mixed = self.service.build_report(job_id, recommendation_limit=3, recommendation_policy="ionic_mixed_valence_v1")
        legacy = self.service.build_report(job_id, recommendation_limit=3, recommendation_policy="fe_ni_o_relaxation_v1")
        previous = {entry[key]: Path(entry[key]).read_bytes() for entry in (ordinary, mixed, legacy)
                    for key in ("report_path", "evidence_path")}
        config = self.root / "ranked-report-config.json"
        payload = self.service.settings.snapshot()
        payload["automatic_ranking_policy"] = "ionic_ranked_v2"
        config.write_text(json.dumps(payload), encoding="utf-8")
        output = io.StringIO()
        with patch("sys.argv", ["matpilot", "--config", str(config), "report", job_id,
                               "--recommendation-limit", "3", "--recommendation-policy", "auto"]), contextlib.redirect_stdout(output):
            main()
        cli = json.loads(output.getvalue())
        self.assertIn("ionic_ranked_v2", Path(cli["report_path"]).parts)
        evidence = json.loads(Path(cli["evidence_path"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["recommendation"]["effective_selection_policy"], "ionic_ranked_v2")
        self.assertTrue(evidence["recommendation"]["include_all_records"])
        markdown = Path(cli["report_path"]).read_text(encoding="utf-8")
        for expected in ("训练使用历史", "相对分数", "不是经过校准", "覆盖结果", "回退或警告", "ranking_evidence", "电荷可行性"):
            self.assertIn(expected, markdown)
        self.assertNotIn("本策略不产生物性评分", markdown)
        for path, content in previous.items():
            self.assertEqual(Path(path).read_bytes(), content)

        async def exercise():
            app = make_server(Settings.load(str(config)))
            native = self.structured(await app.call_tool("build_report", {"job_id": job_id,
                "recommendation_limit": 3, "recommendation_policy": "ionic_ranked_v2"}))
            self.assertEqual(native["evidence_path"], cli["evidence_path"])
            actual = json.loads(Path(native["evidence_path"]).read_text(encoding="utf-8"))
            self.assertEqual(actual["recommendation"]["all_candidate_audit_sha256"],
                             evidence["recommendation"]["all_candidate_audit_sha256"])
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
