import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_contract  # noqa: E402
import validate_spec  # noqa: E402

SPEC = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))


class ContractFileTests(unittest.TestCase):
    """The contract as it is in the repository, and the mistakes the validator must catch in it."""

    def broken(self, change) -> list[str]:
        spec = copy.deepcopy(SPEC)
        change(spec)
        return validate_spec.problems_in(spec)

    def test_the_contract_is_consistent(self):
        self.assertEqual(validate_spec.problems_in(SPEC), [])

    def test_a_reference_to_a_schema_that_does_not_exist_is_caught(self):
        def change(spec):
            spec["paths"]["/api/dashboard"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
                "$ref": "#/components/schemas/Tablero"}
        self.assertTrue(any("points to nothing" in problem for problem in self.broken(change)))

    def test_a_path_parameter_nobody_declared_is_caught(self):
        def change(spec):
            spec["paths"]["/api/dashboard/{cropId}"]["get"]["parameters"] = []
        self.assertIn("GET /api/dashboard/{cropId}: path parameter 'cropId' is not declared", self.broken(change))

    def test_a_protected_route_without_its_401_is_caught(self):
        def change(spec):
            del spec["paths"]["/api/fincas"]["get"]["responses"]["401"]
        self.assertIn("GET /api/fincas: needs a token but does not document the 401", self.broken(change))

    def test_a_repeated_operation_id_is_caught(self):
        def change(spec):
            spec["paths"]["/api/alerts"]["get"]["operationId"] = "dashboard"
        self.assertTrue(any("is also used by" in problem for problem in self.broken(change)))

    def test_a_required_field_missing_from_its_schema_is_caught(self):
        def change(spec):
            spec["components"]["schemas"]["Sensor"]["required"].append("battery")
        self.assertTrue(any("'battery' is not among its properties" in problem for problem in self.broken(change)))

    def test_a_body_without_its_400_and_an_identifier_without_its_404_are_caught(self):
        def change(spec):
            del spec["paths"]["/api/riego"]["post"]["responses"]["400"]
            del spec["paths"]["/api/alerts/{alertId}/acknowledge"]["patch"]["responses"]["404"]
        problems = self.broken(change)
        self.assertIn("POST /api/riego: takes a body but does not document the 400", problems)
        self.assertIn("PATCH /api/alerts/{alertId}/acknowledge: takes an identifier but does not document the 404",
                      problems)


class AnswerCheckTests(unittest.TestCase):
    """How check_contract.py compares an answer of the backend with its schema."""

    PREDICTION = {"idPrediction": 3, "idCrop": 1, "type": "IRRIGATION", "confidence": 0.95,
                  "recommendation": "Riega ahora.", "modelUsed": "agrosense-riego-reglas-1.0",
                  "createdAt": "2026-10-11T10:51:19.308"}

    def check(self, value, schema_name):
        return check_contract.mismatches(value, SPEC["components"]["schemas"][schema_name])

    def test_an_answer_that_fits_has_no_mismatches(self):
        self.assertEqual(self.check(self.PREDICTION, "Prediction"), [])
        self.assertEqual(self.check(dict(self.PREDICTION, confidence=None, recommendation=None), "Prediction"), [])

    def test_missing_wrong_and_unknown_fields_are_reported(self):
        answer = dict(self.PREDICTION, idPrediction="3", extra=True)
        del answer["createdAt"]
        self.assertEqual(self.check(answer, "Prediction"),
                         ["body.createdAt is missing", "body.idPrediction is not a integer",
                          "body.extra is not in the contract"])

    def test_true_is_not_a_number_and_a_value_outside_its_list_is_reported(self):
        self.assertEqual(self.check(dict(self.PREDICTION, idCrop=True), "Prediction"), ["body.idCrop is not a integer"])
        sensor = {"idSensor": 1, "idCrop": 1, "sensorCode": "AS-001", "sensorType": "BAROMETER", "active": True}
        self.assertTrue(self.check(sensor, "Sensor")[0].startswith("body.sensorType = 'BAROMETER' is not one of"))

    def test_lists_are_checked_item_by_item_and_against_their_limit(self):
        dashboard = {"totalCrops": 1, "totalSensors": 1, "pendingAlerts": 0, "activeAlerts": [],
                     "latestReadings": [{"idReading": 1}]}
        self.assertIn("body.latestReadings[0].idSensor is missing", self.check(dashboard, "Dashboard"))
        dashboard["latestReadings"] = []
        dashboard["activeAlerts"] = [None] * 6
        self.assertEqual(self.check(dashboard, "Dashboard"), ["body.activeAlerts has more than 5 items"])

    def test_null_is_only_accepted_where_the_contract_allows_it(self):
        self.assertEqual(self.check(dict(self.PREDICTION, idCrop=None), "Prediction"), ["body.idCrop is null"])
        self.assertEqual(check_contract.mismatches(None, {"type": "object"}), ["body is null"])


if __name__ == "__main__":
    unittest.main()
