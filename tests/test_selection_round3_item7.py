"""Round-3 outcome retry regression."""
import unittest
from unittest import mock

from mcp_server import server


class PendingAttestationRetryTests(unittest.TestCase):
    def test_retry_uses_verdict_at_evidence_not_late_immediate_verdict(self):
        entry = {"id": "attested-entry", "outcome_op_pending": True,
                 "source_phrase_id": "phrase", "answer_notes": [{"pitch": 60}],
                 "immediate_verdict": "keep", "late": True,
                 "attestation": {"fact": "absent",
                                 "attested_at": "2026-09-25T10:00:00+00:00"}}
        with mock.patch.object(server, "_outcome_op_applied", return_value=False), \
                mock.patch.object(server, "_apply_outcome_rating_ops",
                                  return_value=[]) as apply, \
                mock.patch("ledger.set_outcome_op_pending"):
            result, _ = server._close_outcome_ratings({}, {entry["id"]: entry}, {})
        retried = apply.call_args.args[0]
        self.assertIsNone(retried["immediate_verdict"])
        # a late KEEP attested absent is abandoned (demote), exactly as at confirm time
        self.assertEqual(retried["terminal_state"], "abandoned")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
