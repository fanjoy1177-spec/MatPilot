"""Merged Service/CLI/MCP contracts with synthetic fixtures, not accuracy evidence."""
import asyncio
import contextlib
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
from tests import test_recommendation_v2_integration as evidence_fixtures


class ComplexityRankingIntegrationTests(unittest.TestCase):
    collection = mixed_fixtures.MixedValenceIntegrationTests.collection
    fixture_job = triage_fixtures.TriageIntegrationTests.fixture_job
    registered = triage_fixtures.TriageIntegrationTests.registered

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(Settings(data_dir=self.root / 'jobs', backend='demo'))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def structured(result):
        value = result[1] if isinstance(result, tuple) else result
        if isinstance(value, dict):
            return value
        return json.loads((result[0] if isinstance(result, tuple) else result)[0].text)

    def test_default_preserved_and_explicit_complexity_keeps_pool_and_budget(self):
        self.assertEqual(Settings().automatic_ranking_policy, 'ionic_mixed_valence_v1')
        job_id = self.fixture_job()
        old = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True,
                                                selection_policy='ionic_mixed_valence_v1')
        ranked = self.service.recommend_candidates(job_id=job_id, limit=3, include_all_records=True,
                                                   selection_policy='ionic_complexity_v3')
        self.assertEqual(old['stage_counts'], ranked['stage_counts'])
        self.assertEqual(old['selected_count'], ranked['selected_count'])
        for before, after in zip(old['records'], ranked['records']):
            self.assertEqual(before['relaxation_priority'], after['relaxation_priority'])
            self.assertEqual(before['structural_representative_qualified'], after['structural_representative_qualified'])
        self.assertFalse(ranked['selection_policy_resolution']['property_prediction_performed'])
        self.assertTrue(ranked['selection_policy_resolution']['policy_choice_informed_by_development_labels'])
        self.assertFalse(ranked['selection_policy_resolution']['fitted_model'])
        self.assertEqual(self.service.recommend_candidates(job_id=job_id, limit=3)['selected_candidate_ids'], old['selected_candidate_ids'])

    def test_configured_auto_matches_explicit_and_capabilities_disclose_both_features(self):
        service, config = self.collection('Fe3O4')
        payload = json.loads(config.read_text(encoding='utf-8'))
        payload['automatic_ranking_policy'] = 'ionic_complexity_v3'
        config.write_text(json.dumps(payload), encoding='utf-8')
        service = Service(Settings.load(str(config)))
        caps = service.get_capabilities()
        self.assertEqual(caps['candidate_recommendation']['automatic_ranking_policy'], 'ionic_complexity_v3')
        for policy in ('triage_v2', 'evidence_v1', 'structural_v2', 'ionic_mixed_valence_v2', 'ionic_ranked_v2', 'ionic_complexity_v3'):
            self.assertIn(policy, caps['candidate_recommendation']['selection_policies'])
        self.assertIn('property_prediction', caps)
        one = service.recommend_candidates(collection_id='fixture', include_all_records=True)
        two = service.recommend_candidates(collection_id='fixture', include_all_records=True, selection_policy='ionic_complexity_v3')
        self.assertEqual(one['effective_selection_policy'], 'ionic_complexity_v3')
        self.assertEqual(one['records'], two['records'])
        self.assertEqual(one['all_candidate_audit_sha256'], two['all_candidate_audit_sha256'])
        self.assertIsNone(one['records'][0]['ranking_evidence']['uncalibrated_score'])

    def test_unsupported_system_falls_back_without_changing_gate(self):
        service, _ = self.collection('FeNi')
        service.settings.automatic_ranking_policy = 'ionic_complexity_v3'
        result = service.recommend_candidates(collection_id='fixture')
        self.assertEqual(result['effective_selection_policy'], 'structural_v1')
        self.assertTrue(result['selection_policy_resolution']['fallback_applied'])
        self.assertFalse(result['selection_policy_resolution']['property_prediction_performed'])
        with self.assertRaisesRegex(ValueError, 'does not support'):
            service.recommend_candidates(collection_id='fixture', selection_policy='ionic_complexity_v3')

    def test_cli_and_mcp_expose_same_complexity_result_without_predicting_properties(self):
        service, config = self.collection('Fe3O4')
        output = io.StringIO()
        with patch('sys.argv', ['matpilot', '--config', str(config), 'recommend-collection', 'fixture',
                               '--selection-policy', 'ionic_complexity_v3', '--include-all-records']), contextlib.redirect_stdout(output):
            main()
        cli = json.loads(output.getvalue())

        async def exercise():
            app = make_server(service.settings)
            tools = {t.name: t for t in await app.list_tools()}
            self.assertIn('get_candidate_evidence', tools)
            self.assertIn('prepare_property_prediction', tools)
            self.assertEqual(tools['recommend_candidates'].inputSchema['properties']['selection_policy']['default'], 'auto')
            result = self.structured(await app.call_tool('recommend_candidates', {'collection_id': 'fixture',
                'selection_policy': 'ionic_complexity_v3', 'include_all_records': True}))
            self.assertEqual(result['records'], cli['records'])
            self.assertEqual(result['all_candidate_audit_sha256'], cli['all_candidate_audit_sha256'])
            self.assertFalse(result['selection_policy_resolution']['property_prediction_performed'])
        asyncio.run(exercise())
        self.assertEqual(service.store.jobs(), [])

    def test_report_discloses_heuristic_and_preserves_existing_reports(self):
        job_id = self.fixture_job()
        prior = self.service.build_report(job_id, recommendation_limit=3, recommendation_policy='ionic_mixed_valence_v1')
        frozen = {prior[key]: Path(prior[key]).read_bytes() for key in ('report_path', 'evidence_path')}
        result = self.service.build_report(job_id, recommendation_limit=3, recommendation_policy='ionic_complexity_v3')
        text = Path(result['report_path']).read_text(encoding='utf-8')
        for expected in ('约化整数化学式', '没有拟合模型', '规则选择参考过开发标签', 'ranking_evidence'):
            self.assertIn(expected, text)
        evidence = json.loads(Path(result['evidence_path']).read_text(encoding='utf-8'))
        self.assertFalse(evidence['recommendation']['selection_policy_resolution']['property_prediction_performed'])
        for path, content in frozen.items():
            self.assertEqual(Path(path).read_bytes(), content)

    def test_explicit_evidence_and_material_class_precede_configured_heuristic(self):
        fixture = evidence_fixtures.RecommendationV2IntegrationTests()
        fixture.setUp()
        try:
            rows = [fixture.record('A'), fixture.record('B')]
            metrics = [fixture.metric(rows[0], .8), fixture.metric(rows[1], .1)]
            service, _, _ = fixture.register(rows, metrics=metrics)
            service.settings.automatic_ranking_policy = 'ionic_complexity_v3'
            ranked = service.recommend_candidates(collection_id='fixture', objective=fixture.objective(),
                metric_selections=[fixture.selection(m) for m in metrics], include_all_records=True)
            self.assertEqual(ranked['requested_selection_policy'], 'auto')
            self.assertEqual(ranked['effective_selection_policy'], 'evidence_v1')
            self.assertEqual(ranked['selected_candidate_ids'], ['B'])
            self.assertNotIn('priority_ranking', ranked)
            declared = service.recommend_candidates(collection_id='fixture', material_class='unknown')
            self.assertEqual(declared['effective_selection_policy'], 'structural_v2')
            with self.assertRaisesRegex(ValueError, 'do not accept'):
                service.recommend_candidates(collection_id='fixture', selection_policy='ionic_complexity_v3',
                    material_class='ionic')
        finally:
            fixture.tearDown()
