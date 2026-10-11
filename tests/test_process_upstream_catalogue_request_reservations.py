from __future__ import annotations

import copy
import json
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests import test_process_upstream_catalogue_candidate as candidate_tests
from tests import test_upstream_catalogue_process_workflow as workflow_tests


MODULE = candidate_tests.MODULE


NOW = "2026-10-01T10:00:00Z"
DETAIL_PK = "uddi:1ca4ede2-911d-486a-b374-e7cf8650d25c_201712281924"
CURRENT_PAGE = (
    '<input type="hidden" id="publicDataPk" value="{identity}">'
    f'<input type="hidden" id="publicDataDetailPk" value="{DETAIL_PK}">'
    '<button onclick="fn_goUrlLink(\'{identity}\')">go</button>'
)


class Interrupted(BaseException):
    """Simulates process cancellation, which must not refund a reservation."""


class RequestReservationAccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = candidate_tests.UpstreamCatalogueProcessorTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.now = NOW
        self.checkpoint_schema = json.loads(
            (candidate_tests.ROOT / "schemas/datapan.upstream-catalogue-checkpoint.v1.schema.json").read_text(),
        )

    def row(self, identity: int, *, legacy: bool = False) -> dict:
        row = self.fixture.real_link_row()
        row["id"] = str(identity)
        row["type"] = "LINK"
        row["title"] = f"Reservation fixture {identity}"
        row["source"]["url"] = f"https://www.data.go.kr/data/{identity}/openapi.do"
        row["source"]["raw"]["api_id"] = str(identity)
        row["source"]["raw"]["meta_url"] = row["source"]["url"]
        if legacy:
            row["source"]["raw"].pop("guide_url", None)
        return row

    def prepare(self, rows: list[dict]) -> None:
        self.fixture.write_real_composer_inputs([], rows, NOW)
        self.fixture.provider_index_path = self.fixture.root / "provider-index.json"
        self.fixture.provider_index_path.write_text(json.dumps({
            "adapters": [{"name": "fixture", "hosts": ["api.example.gov"]}],
        }), encoding="utf-8")

    def page(self, identity: str) -> str:
        return CURRENT_PAGE.format(identity=identity)

    def resolver_observation(self, identity: str):
        url = MODULE.DETAIL_HELPERS.CURRENT_LINK_RESOLVER_URL.format(dataset_id=identity)
        body = json.dumps({
            "status": True,
            "publicDataDetailPk": DETAIL_PK,
            "linkUrl": f"https://api.example.gov/resolved/{identity}",
        }).encode()
        return MODULE.LinkResolverObservation(
            body=body,
            request_url=url,
            effective_url=url,
            observed_at=NOW,
        )

    def claim(self, *, max_attempts: int, max_queue: int, retries: int, run_id: str = "701"):
        args = self.fixture.args(run_id=run_id, **{
            "--claim-only": None,
            "--max-attempts": max_attempts,
            "--max-queue": max_queue,
            "--retries-per-detail": retries,
        })
        code, checkpoint = MODULE.process(
            args,
            fetcher=lambda *_: self.fail("claim must reserve without an HTTP call"),
            resolver_fetcher=lambda *_: self.fail("claim must reserve without resolver I/O"),
        )
        self.assertEqual(code, 0, checkpoint.get("outcome"))
        return args, checkpoint

    def worker_args(self, run_id: str, *, max_attempts: int, max_queue: int, retries: int):
        return self.fixture.args(run_id=run_id, **{
            "--max-attempts": max_attempts,
            "--max-queue": max_queue,
            "--retries-per-detail": retries,
            "--require-durable-reservation": None,
        })

    def test_24_physical_requests_charge_page_and_resolver_retries_before_each_call(self) -> None:
        rows = [self.row(identity) for identity in range(100, 108)]
        self.prepare(rows)
        _claim_args, claim = self.claim(max_attempts=24, max_queue=8, retries=2)
        self.assertEqual(claim["request_reservation"]["reserved_attempts"], 24)
        self.assertEqual(claim["attempts_consumed"], 24)
        self.assertEqual(claim["attempts_by_id"], {str(identity): 3 for identity in range(100, 108)})

        page_calls: list[str] = []
        resolver_calls: list[str] = []
        durable_progress: list[tuple[str, int, int, int, dict[str, int], dict[str, int]]] = []

        def fetch_page(url: str, _timeout: float) -> str:
            identity = url.split("/data/")[1].split("/")[0]
            page_calls.append(identity)
            checkpoint = json.loads(self.fixture.checkpoint_path(claim).read_text())
            MODULE.verify_checkpoint(checkpoint, self.checkpoint_schema)
            durable_progress.append((
                identity, checkpoint["request_reservation"]["attempts_made"],
                len(page_calls) + len(resolver_calls), checkpoint["attempts_consumed"],
                dict(checkpoint["attempts_by_id"]),
                {row["id"]: row["attempts_reserved"] for row in checkpoint["request_reservation"]["records"]},
            ))
            return self.page(identity)

        def fail_resolver(url: str, _timeout: float):
            identity = url.rsplit("=", 1)[1]
            resolver_calls.append(identity)
            checkpoint = json.loads(self.fixture.checkpoint_path(claim).read_text())
            MODULE.verify_checkpoint(checkpoint, self.checkpoint_schema)
            durable_progress.append((
                identity, checkpoint["request_reservation"]["attempts_made"],
                len(page_calls) + len(resolver_calls), checkpoint["attempts_consumed"],
                dict(checkpoint["attempts_by_id"]),
                {row["id"]: row["attempts_reserved"] for row in checkpoint["request_reservation"]["records"]},
            ))
            raise TimeoutError("redacted fixture transport failure")

        args = self.worker_args("701", max_attempts=24, max_queue=8, retries=2)
        code, completed = MODULE.process(
            args, fetcher=fetch_page, resolver_fetcher=fail_resolver, sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(page_calls), 8)
        self.assertEqual(len(resolver_calls), 16)
        self.assertEqual(page_calls, [str(identity) for identity in range(100, 108)])
        self.assertEqual(resolver_calls, [str(identity) for identity in range(100, 108) for _ in range(2)])
        self.assertEqual(completed["request_reservation"]["attempts_made"], 24)
        self.assertEqual(completed["request_reservation"]["reserved_attempts"], 24)
        self.assertEqual(completed["attempts_consumed"], 24)
        self.assertEqual(completed["attempts_by_id"], {str(identity): 3 for identity in range(100, 108)})
        self.assertEqual([row[1] for row in durable_progress], list(range(1, 25)))
        self.assertEqual([row[2] for row in durable_progress], list(range(1, 25)))
        self.assertEqual([row[3] for row in durable_progress], [24] * 24)
        self.assertTrue(all(row[4] == {str(identity): 3 for identity in range(100, 108)} for row in durable_progress))
        self.assertTrue(all(row[5] == {str(identity): 3 for identity in range(100, 108)} for row in durable_progress))
        self.assertNotIn("redacted fixture", self.fixture.checkpoint_path(completed).read_text())

    def test_production_48_24_2_batch_reserves_eight_current_rows_and_advances_to_fresh_rows(self) -> None:
        self.prepare([self.row(identity) for identity in range(100, 148)])
        _args, claim = self.claim(max_attempts=24, max_queue=48, retries=2)
        expected_first_slice = {str(identity): 3 for identity in range(100, 108)}
        self.assertEqual(claim["request_reservation"]["reserved_attempts"], 24)
        self.assertEqual(
            {record["id"]: record["attempts_reserved"] for record in claim["request_reservation"]["records"]},
            expected_first_slice,
        )
        self.assertEqual(claim["detail_queue_cursor"], 8)

        page_calls: list[str] = []
        resolver_calls: list[str] = []

        def fetch_page(url: str, _timeout: float) -> str:
            identity = url.split("/data/")[1].split("/")[0]
            page_calls.append(identity)
            return self.page(identity)

        def fetch_resolver(url: str, _timeout: float):
            identity = url.rsplit("=", 1)[1]
            resolver_calls.append(identity)
            return self.resolver_observation(identity)

        code, completed = MODULE.process(
            self.worker_args("701", max_attempts=24, max_queue=48, retries=2),
            fetcher=fetch_page, resolver_fetcher=fetch_resolver, sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(page_calls, [str(identity) for identity in range(100, 108)])
        self.assertEqual(resolver_calls, [str(identity) for identity in range(100, 108)])
        # Each current-template row has one physical page and resolver call;
        # finalization returns its unused third reserved slot.
        self.assertEqual(completed["request_reservation"]["attempts_made"], 16)
        self.assertEqual(completed["request_reservation"]["reserved_attempts"], 16)
        self.assertEqual(completed["attempts_consumed"], 16)
        self.assertEqual(completed["attempts_by_id"], {str(identity): 2 for identity in range(100, 108)})
        self.assertEqual(completed["detail_queue_cursor"], 8)

        self.fixture.now = "2026-10-01T11:00:00Z"
        _args, next_slice = self.claim(max_attempts=24, max_queue=48, retries=2, run_id="702")
        self.assertEqual(next_slice["detail_queue_cursor"], 16)
        self.assertEqual(
            {record["id"]: record["attempts_reserved"] for record in next_slice["request_reservation"]["records"]},
            {str(identity): 3 for identity in range(108, 116)},
        )
        self.assertEqual(next_slice["attempts_by_id"], {
            **{str(identity): 2 for identity in range(100, 108)},
            **{str(identity): 3 for identity in range(108, 116)},
        })

    def test_source_epoch_paid_claim_runs_durably_at_the_cumulative_cap(self) -> None:
        rows = [self.row(identity, legacy=identity == 101) for identity in range(100, 108)]
        self.prepare(rows)
        _initial_args, initial = self.claim(max_attempts=1, max_queue=8, retries=2)
        initial_path = self.fixture.checkpoint_path(initial)
        predecessor = copy.deepcopy(initial)
        predecessor.update({
            "status": "ready", "outcome": {"reason": "synthetic_authenticated_a_parent"},
            "lease": None, "request_reservation": None,
            "attempts_consumed": 16,
            "attempts_by_id": {
                "100": 0, "101": 0, "102": 1,
                "103": 3, "104": 3, "105": 3, "106": 3, "107": 3,
            },
            "detail_retry_reset_ids": [], "detail_records": [], "detail_queue_cursor": 0,
        })
        MODULE.seal_checkpoint(predecessor)
        initial_path.write_text(json.dumps(predecessor, sort_keys=True), encoding="utf-8")
        index_path = self.fixture.state_dir / "sources/data_go_kr/index.json"
        MODULE.append_generation_index(index_path, initial_path, predecessor)
        # Seed a coherent prior retry ledger for the synthetic predecessor;
        # the initial setup claim above only creates the test checkpoint.
        index = MODULE.load_json(index_path)
        retry_state = index["detail_retry_state"]
        for row in rows:
            identity = row["id"]
            attempts = predecessor["attempts_by_id"][identity]
            if attempts:
                retry_state[identity] = {
                    "source_sha256": MODULE.source_fingerprint(row),
                    "guide_sha256": MODULE.guide_fingerprint(row),
                    "attempts": attempts,
                    "last_attempt_at": NOW,
                }
            else:
                retry_state.pop(identity, None)
        index_path.write_text(json.dumps(index, sort_keys=True), encoding="utf-8")

        eligibility = MODULE.SOURCE_ELIGIBILITY
        target_contract = {
            "schema_version": eligibility.TARGET_CONTRACT_SCHEMA_VERSION,
            "source_sha": "1" * 40,
            "source_tree_sha": "2" * 40,
            "inventory_sha256": "3" * 64,
            "contract_sha256": eligibility.target_contract_digest("3" * 64),
            "changed_classes": [eligibility.SAME_A_PROCESSING],
        }
        epoch = eligibility.build_source_epoch(
            original_observation_sha256=eligibility.object_sha256(predecessor["last_observation"]),
            target_contract_sha256=target_contract["contract_sha256"],
            predecessor_generation_id=predecessor["generation_id"],
            target_generation_id=None,
            ordinal=2,
        )
        old_inputs = predecessor["generation_inputs"]
        epoch_generation_id, epoch_inputs = MODULE.generation_identity(
            predecessor["source_id"], predecessor["source_scope"],
            old_inputs["baseline_sha256"], old_inputs["candidate_sha256"],
            old_inputs["observation_failure_sha256"], old_inputs["policy_sha256"],
            old_inputs["adapter_revision"], source_epoch=epoch,
            source_target_contract=target_contract,
        )
        successor = copy.deepcopy(predecessor)
        successor.update({
            "generation_id": epoch_generation_id,
            "generation_inputs": epoch_inputs,
            "status": "retry",
            "outcome": {"reason": "synthetic_prepaid_source_epoch_fixture"},
            "request_reservation": None,
        })
        MODULE.seal_checkpoint(successor)
        successor_path = initial_path.with_name(f"{epoch_generation_id}.json")
        successor_path.write_text(json.dumps(successor, sort_keys=True), encoding="utf-8")
        MODULE.append_generation_index(index_path, successor_path, successor)

        args = self.fixture.args(run_id="701", **{
            "--claim-only": None,
            "--max-attempts": 8,
            "--max-queue": 8,
            "--retries-per-detail": 2,
        })
        calls: list[str] = []
        with mock.patch.object(
            MODULE, "generation_identity", return_value=(epoch_generation_id, epoch_inputs),
        ):
            claim_code, claim = MODULE.process(
                args,
                fetcher=lambda url, _timeout: calls.append(url) or self.fail("claim performed HTTP I/O"),
                resolver_fetcher=lambda *_: self.fail("claim performed resolver I/O"),
            )
        self.assertEqual(claim_code, 0, claim.get("outcome"))
        self.assertEqual(claim["attempts_consumed"], 24)
        self.assertEqual(claim["request_reservation"]["reserved_attempts"], 8)
        self.assertEqual(claim["request_reservation"]["attempts_made"], 0)
        self.assertEqual(claim["attempts_by_id"], {str(identity): 3 for identity in range(100, 108)})
        self.assertEqual(
            {row["id"]: row["attempts_reserved"] for row in claim["request_reservation"]["records"]},
            {"100": 3, "101": 3, "102": 2},
        )
        self.assertEqual(calls, [])

        queue, _retained, _pending = MODULE.detail_queue(
            [], rows, {}, datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        )
        instant = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        with self.assertRaisesRegex(ValueError, "durable_request_reservation_missing"):
            MODULE.validate_durable_request_reservation(
                {**claim, "request_reservation": {**claim["request_reservation"], "owner_run_id": "702"}},
                queue, processor_run_id="701", fencing_token=claim["fencing_token"], now=instant,
            )
        with self.assertRaisesRegex(ValueError, "durable_request_reservation_expired"):
            MODULE.validate_durable_request_reservation(
                {**claim, "request_reservation": {
                    **claim["request_reservation"], "expires_at": "2026-09-30T00:00:00Z",
                }},
                queue, processor_run_id="701", fencing_token=claim["fencing_token"], now=instant,
            )
        with self.assertRaisesRegex(ValueError, "durable_request_reservation_record_mismatch"):
            MODULE.validate_durable_request_reservation(
                {**claim, "request_reservation": {
                    **claim["request_reservation"],
                    "records": [
                        {**claim["request_reservation"]["records"][0], "source_sha256": "f" * 64},
                        *claim["request_reservation"]["records"][1:],
                    ],
                }},
                queue, processor_run_id="701", fencing_token=claim["fencing_token"], now=instant,
            )
        fully_consumed = {
            **claim, "request_reservation": {
                **claim["request_reservation"], "attempts_made": 8,
                "records": [
                    {**record, "attempts_made": record["attempts_reserved"]}
                    for record in claim["request_reservation"]["records"]
                ],
            },
        }
        _reservation, paid_rows, unreserved_rows = MODULE.validate_durable_request_reservation(
            fully_consumed, queue, processor_run_id="701",
            fencing_token=claim["fencing_token"], now=instant,
        )
        self.assertEqual([row["id"] for row in paid_rows], ["100", "101", "102"])
        self.assertEqual(len(unreserved_rows), 5)

        args.claim_only = False
        args.require_durable_reservation = True
        args.max_attempts = 4
        interrupted_calls: list[str] = []

        def first_pass_page_timeout(url: str, _timeout: float):
            identity = url.split("/data/")[1].split("/")[0]
            interrupted_calls.append(identity)
            self.assertIn(identity, {"100", "101"}, "row 102 must not start before the injected crash")
            raise TimeoutError("fixture page transport failure before row 102")

        original_atomic_write_json = MODULE.atomic_write_json

        def interrupt_after_row_101_unused_slots_are_durable(path, value):
            original_atomic_write_json(path, value)
            if pathlib.Path(path) != self.fixture.checkpoint_path(claim):
                return
            reservation_value = value.get("request_reservation")
            records_value = (
                reservation_value.get("records") if isinstance(reservation_value, dict) else None
            )
            made_by_id = {
                record.get("id"): record.get("attempts_made")
                for record in records_value or [] if isinstance(record, dict)
            }
            detail_records = value.get("detail_records")
            if (
                made_by_id == {"100": 3, "101": 1, "102": 0}
                and reservation_value.get("reserved_attempts") == 8
                and [row.get("id") for row in detail_records or []] == ["100", "101"]
            ):
                raise Interrupted("simulated process loss after row 101 returned unused paid slots")

        with mock.patch.object(
            MODULE, "generation_identity", return_value=(epoch_generation_id, epoch_inputs),
        ):
            with mock.patch.object(
                MODULE, "atomic_write_json", side_effect=interrupt_after_row_101_unused_slots_are_durable,
            ):
                with self.assertRaisesRegex(Interrupted, "returned unused paid slots"):
                    MODULE.process(
                        args, fetcher=first_pass_page_timeout,
                        resolver_fetcher=lambda *_: self.fail("unexpected resolver call"),
                        sleeper=lambda _delay: None,
                    )
        self.assertEqual(interrupted_calls, ["100", "100", "100", "101"])
        partial = json.loads(self.fixture.checkpoint_path(claim).read_text(encoding="utf-8"))
        self.assertEqual(partial["attempts_consumed"], 24)
        self.assertEqual(partial["attempts_by_id"], {str(identity): 3 for identity in range(100, 108)})
        self.assertEqual(partial["request_reservation"]["reserved_attempts"], 8)
        self.assertEqual(partial["request_reservation"]["attempts_made"], 4)
        partial_by_id = {
            record["id"]: (record["attempts_reserved"], record["attempts_made"])
            for record in partial["request_reservation"]["records"]
        }
        self.assertEqual(partial_by_id, {"100": (3, 3), "101": (3, 1), "102": (2, 0)})
        self.assertEqual(
            [(row["id"], row["status"]) for row in partial["detail_records"]],
            [("100", "retry"), ("101", "retry")],
        )

        page_calls: list[str] = []

        def complete_row_101_and_exhaust_row_102(url: str, _timeout: float):
            identity = url.split("/data/")[1].split("/")[0]
            page_calls.append(identity)
            if identity == "100":
                self.fail("a fully spent row must not replay any physical request")
            if identity == "101":
                return '<a href="https://api.example.gov/op/101" onclick="fn_LinkApiRequest()">API</a>'
            if identity == "102":
                raise TimeoutError("fixture page transport failure for row 102")
            self.fail(f"unreserved row {identity} must not make a physical request")

        with mock.patch.object(
            MODULE, "generation_identity", return_value=(epoch_generation_id, epoch_inputs),
        ):
            worker_code, completed = MODULE.process(
                args, fetcher=complete_row_101_and_exhaust_row_102,
                resolver_fetcher=lambda *_: self.fail("unexpected resolver call"),
                sleeper=lambda _delay: None,
            )
        self.assertEqual(worker_code, 2)
        self.assertEqual(page_calls, ["101", "102", "102"])
        self.assertEqual(completed["attempts_consumed"], 23)
        self.assertEqual(completed["request_reservation"]["attempts_made"], 7)
        self.assertEqual(completed["request_reservation"]["reserved_attempts"], 7)
        self.assertEqual(
            {
                record["id"]: (record["attempts_reserved"], record["attempts_made"])
                for record in completed["request_reservation"]["records"]
            },
            {"100": (3, 3), "101": (2, 2), "102": (2, 2)},
        )
        self.assertEqual(
            completed["attempts_by_id"],
            {**{str(identity): 3 for identity in range(100, 101)},
             **{str(identity): 3 for identity in range(102, 108)}},
        )
        self.assertNotEqual(completed["generation_id"], predecessor["generation_id"])

    def test_processor_rejects_tampered_reservation_aggregate_before_fetch(self) -> None:
        self.prepare([self.row(2, legacy=True)])
        _claim_args, claim = self.claim(max_attempts=3, max_queue=1, retries=2)
        checkpoint_path = self.fixture.checkpoint_path(claim)
        index_path = self.fixture.state_dir / "sources/data_go_kr/index.json"
        variants = (
            (1, 0),  # Aggregate claims one consumed slot, while the record claims none.
            (0, 1),  # Record claims one consumed slot, while the aggregate claims none.
        )
        for aggregate_made, row_made in variants:
            with self.subTest(aggregate_made=aggregate_made, row_made=row_made):
                tampered = copy.deepcopy(claim)
                tampered["request_reservation"]["attempts_made"] = aggregate_made
                tampered["request_reservation"]["records"][0]["attempts_made"] = row_made
                MODULE.seal_checkpoint(tampered)
                checkpoint_path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
                MODULE.append_generation_index(index_path, checkpoint_path, tampered)
                fetches: list[str] = []
                args = self.worker_args("701", max_attempts=3, max_queue=1, retries=2)
                with self.assertRaisesRegex(ValueError, "durable_request_reservation_record_mismatch"):
                    MODULE.process(
                        args,
                        fetcher=lambda url, _timeout: fetches.append(url),
                        resolver_fetcher=lambda *_: self.fail("invalid reservation must fail before resolver"),
                    )
                self.assertEqual(fetches, [])

    def test_durable_charge_before_first_fetch_is_never_replayed_or_refunded(self) -> None:
        self.prepare([self.row(2, legacy=True)])
        _claim_args, claim = self.claim(max_attempts=3, max_queue=1, retries=2)
        args = self.worker_args("701", max_attempts=3, max_queue=1, retries=2)
        precrash_fetches: list[str] = []
        checkpoint_path = self.fixture.checkpoint_path(claim)
        original_atomic_write_json = MODULE.atomic_write_json

        def persist_then_crash_before_fetch(path, value):
            original_atomic_write_json(path, value)
            if pathlib.Path(path) != checkpoint_path:
                return
            reservation_value = value.get("request_reservation")
            records_value = (
                reservation_value.get("records") if isinstance(reservation_value, dict) else None
            )
            if (
                isinstance(records_value, list) and len(records_value) == 1
                and records_value[0].get("attempts_made") == 1
            ):
                raise Interrupted("simulated crash after atomic charge and before fetch")

        with mock.patch.object(MODULE, "atomic_write_json", side_effect=persist_then_crash_before_fetch):
            with self.assertRaisesRegex(Interrupted, "after atomic charge and before fetch"):
                MODULE.process(
                    args, fetcher=lambda url, _timeout: precrash_fetches.append(url),
                    resolver_fetcher=lambda *_: self.fail("resolver must not precede the page"),
                )
        self.assertEqual(precrash_fetches, [])
        interrupted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(interrupted["attempts_consumed"], 3)
        self.assertEqual(interrupted["request_reservation"]["attempts_made"], 1)
        self.assertEqual(interrupted["request_reservation"]["records"][0]["attempts_made"], 1)

        resumed_fetches: list[str] = []

        def resume_with_only_paid_remainder(url: str, _timeout: float) -> str:
            resumed_fetches.append(url)
            if len(resumed_fetches) == 1:
                raise TimeoutError("the uncertain pre-crash slot is not replayed")
            return '<a href="https://api.example.gov/op/2" onclick="fn_LinkApiRequest()">API</a>'

        code, completed = MODULE.process(
            args, fetcher=resume_with_only_paid_remainder,
            resolver_fetcher=lambda *_: self.fail("legacy-anchor rows must not use resolver"),
            sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 0, completed.get("outcome"))
        self.assertEqual(len(resumed_fetches), 2)
        self.assertEqual(completed["attempts_consumed"], 3)
        self.assertNotIn("2", completed["attempts_by_id"])
        self.assertEqual(completed["request_reservation"]["attempts_made"], 3)
        self.assertEqual(completed["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(completed["request_reservation"]["records"][0]["attempts_made"], 3)

    def test_partial_durable_reservation_resume_uses_only_recorded_paid_remainder(self) -> None:
        self.prepare([self.row(2, legacy=True)])
        _claim_args, claim = self.claim(max_attempts=3, max_queue=1, retries=2)
        args = self.worker_args("701", max_attempts=3, max_queue=1, retries=2)
        started_calls: list[str] = []

        def interrupted_page(url: str, _timeout: float):
            started_calls.append(url)
            raise Interrupted("simulated process loss after slot was durably charged")

        with self.assertRaises(Interrupted):
            MODULE.process(
                args, fetcher=interrupted_page,
                resolver_fetcher=lambda *_: self.fail("resolver must not precede the page"),
            )
        interrupted = json.loads(self.fixture.checkpoint_path(claim).read_text(encoding="utf-8"))
        self.assertEqual(interrupted["attempts_consumed"], 3)
        self.assertEqual(interrupted["request_reservation"]["attempts_made"], 1)
        self.assertEqual(interrupted["request_reservation"]["records"][0]["attempts_made"], 1)

        page_calls: list[str] = []

        def fetch_page(url: str, _timeout: float) -> str:
            page_calls.append(url)
            if len(page_calls) == 1:
                raise TimeoutError("fixture retry within the paid remainder")
            return '<a href="https://api.example.gov/op/2" onclick="fn_LinkApiRequest()">API</a>'

        code, resumed = MODULE.process(
            args, fetcher=fetch_page,
            resolver_fetcher=lambda *_: self.fail("legacy-anchor rows must not use resolver"),
            sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 0, resumed.get("outcome"))
        self.assertEqual(len(started_calls), 1)
        self.assertEqual(len(page_calls), 2)
        self.assertEqual(resumed["attempts_consumed"], 3)
        self.assertEqual(resumed["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(resumed["request_reservation"]["attempts_made"], 3)
        self.assertEqual(resumed["request_reservation"]["records"][0]["attempts_made"], 3)

        queue, _retained, _pending = MODULE.detail_queue(
            [], [self.row(2)], {}, datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        )
        instant = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        old_partial = copy.deepcopy(claim)
        old_partial["request_reservation"]["attempts_made"] = 1
        old_partial["request_reservation"]["records"][0].pop("attempts_made")
        with self.assertRaisesRegex(ValueError, "durable_request_reservation_attempt_history_unavailable"):
            MODULE.validate_durable_request_reservation(
                old_partial, queue, processor_run_id="701", fencing_token=claim["fencing_token"], now=instant,
            )

    def test_production_48_row_allocator_skips_exhausted_prefix_and_uses_odd_residual_capacity(self) -> None:
        queue = [
            {"id": str(identity), "source_sha256": f"{identity:064x}", "guide_sha256": None}
            for identity in range(100, 148)
        ]
        checkpoint = {
            "generation_id": "a" * 64,
            "fencing_token": 1,
            "lease": {"expires_at": "2026-10-01T10:45:00Z"},
            "attempts_by_id": {"100": 3, "101": 3, "102": 3, "103": 2, "104": 1, "105": 3},
            "attempts_consumed": 14,
        }
        now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
        reservation, reserved, unqueued = MODULE.reserve_requests(
            checkpoint, queue, cursor=0, processor_run_id="fixture-queue-48",
            max_attempts=24, max_queue=48, retries_per_detail=2, now=now,
        )
        expected = {"103": 1, "104": 2, **{str(identity): 3 for identity in range(106, 113)}}
        self.assertEqual(reservation["attempt_budget"], 24)
        self.assertEqual(reservation["reserved_attempts"], 24)
        self.assertEqual(
            {record["id"]: record["attempts_reserved"] for record in reservation["records"]}, expected,
        )
        self.assertEqual([row["id"] for row in reserved], list(expected))
        self.assertEqual(checkpoint["detail_queue_cursor"], 13)
        self.assertEqual(checkpoint["attempts_consumed"], 38)
        self.assertNotIn("100", {row["id"] for row in reserved})
        self.assertNotIn("101", {row["id"] for row in reserved})
        self.assertNotIn("102", {row["id"] for row in reserved})
        self.assertNotIn("105", {row["id"] for row in reserved})
        self.assertEqual(len(unqueued), 39)

    def test_mixed_legacy_and_current_rows_refund_only_unused_reserved_slots(self) -> None:
        legacy, current = self.row(2, legacy=True), self.row(3)
        self.prepare([legacy, current])
        self.claim(max_attempts=6, max_queue=2, retries=2)
        page_calls: list[str] = []
        resolver_calls: list[str] = []

        def fetch_page(url: str, _timeout: float) -> str:
            identity = url.split("/data/")[1].split("/")[0]
            page_calls.append(identity)
            if identity == "2":
                return f'<a href="https://api.example.gov/op/{identity}" onclick="fn_LinkApiRequest()">API</a>'
            return self.page(identity)

        def fetch_resolver(url: str, _timeout: float):
            identity = url.rsplit("=", 1)[1]
            resolver_calls.append(identity)
            return self.resolver_observation(identity)

        code, completed = MODULE.process(
            self.worker_args("701", max_attempts=6, max_queue=2, retries=2),
            fetcher=fetch_page, resolver_fetcher=fetch_resolver, sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(page_calls, ["2", "3"])
        self.assertEqual(resolver_calls, ["3"])
        # Six were pre-reserved; one legacy page plus the current page/resolver
        # used three physical slots. The other three were refunded at normal exit.
        self.assertEqual(completed["attempts_consumed"], 3)
        self.assertEqual(completed["attempts_by_id"], {"3": 2})
        self.assertEqual(completed["request_reservation"]["attempts_made"], 3)
        self.assertEqual(completed["request_reservation"]["reserved_attempts"], 3)
        self.assertEqual(
            {record["id"]: record["attempts_reserved"] for record in completed["request_reservation"]["records"]},
            {"2": 1, "3": 2},
        )
        evidence = json.loads((self.fixture.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual([row["api_key"]["id"] for row in evidence["worker_outcomes"]], ["3"])
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"]["code"], "resolved_link_operation_contract_unproven")

    def test_fair_cursor_moves_to_fresh_identities_after_a_slice_exhausts_one(self) -> None:
        self.prepare([self.row(identity) for identity in (10, 20, 30, 40)])
        _args, first_reserved = self.claim(max_attempts=3, max_queue=2, retries=2)
        self.assertEqual(first_reserved["detail_queue_cursor"], 1)
        self.assertEqual(first_reserved["attempts_by_id"], {"10": 3})
        self.assertEqual(
            {row["id"]: row["attempts_reserved"] for row in first_reserved["request_reservation"]["records"]},
            {"10": 3},
        )
        first_calls: list[str] = []

        def fail_first_slice(url: str, _timeout: float) -> str:
            identity = url.split("/data/")[1].split("/")[0]
            first_calls.append(identity)
            raise TimeoutError("first-slice failure")

        first_code, first = MODULE.process(
            self.worker_args("701", max_attempts=3, max_queue=2, retries=2),
            fetcher=fail_first_slice,
            resolver_fetcher=lambda *_: self.fail("legacy-anchor rows must not use resolver"),
            sleeper=lambda _delay: None,
        )
        self.assertIn(first_code, (2, 3))
        self.assertEqual(first_calls, ["10", "10", "10"])
        self.assertEqual(first["attempts_consumed"], 3)
        self.assertEqual(first["attempts_by_id"], {"10": 3})
        self.assertEqual(first["request_reservation"]["attempts_made"], 3)

        self.fixture.now = "2026-10-01T10:50:00Z"
        _args, second = self.claim(max_attempts=3, max_queue=2, retries=2, run_id="702")
        self.assertEqual(second["detail_queue_cursor"], 2)
        self.assertEqual(second["attempts_by_id"], {"10": 3, "20": 3})
        self.assertEqual(
            {row["id"]: row["attempts_reserved"] for row in second["request_reservation"]["records"]},
            {"20": 3},
        )

        self.fixture.now = "2026-10-01T11:40:00Z"
        _args, third = self.claim(max_attempts=3, max_queue=2, retries=2, run_id="703")
        self.assertEqual(third["detail_queue_cursor"], 3)
        self.assertEqual(third["attempts_by_id"], {"10": 3, "20": 3, "30": 3})
        self.assertEqual(
            {row["id"]: row["attempts_reserved"] for row in third["request_reservation"]["records"]},
            {"30": 3},
        )

    def test_zero_budget_for_resolver_never_makes_unreserved_second_request(self) -> None:
        self.prepare([self.row(2)])
        self.claim(max_attempts=1, max_queue=1, retries=0)
        pages: list[str] = []
        resolver: list[str] = []

        def fetch_page(url: str, _timeout: float) -> str:
            pages.append(url)
            return self.page("2")

        code, completed = MODULE.process(
            self.worker_args("701", max_attempts=1, max_queue=1, retries=0),
            fetcher=fetch_page,
            resolver_fetcher=lambda url, _timeout: resolver.append(url),
            sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(pages), 1)
        self.assertEqual(resolver, [])
        self.assertEqual(completed["request_reservation"]["attempts_made"], 1)
        self.assertEqual(completed["attempts_consumed"], 1)
        evidence = json.loads((self.fixture.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"], {
            "code": "insufficient_budget_for_link_resolver", "phase": "resolver",
        })

    def test_page_retry_and_resolver_retry_reuse_the_page_and_persist_each_charge(self) -> None:
        self.prepare([self.row(2), self.row(3)])
        self.claim(max_attempts=6, max_queue=2, retries=2)
        pages: list[str] = []
        resolvers: list[str] = []
        durable_counts: list[tuple[int, int, dict[str, int]]] = []

        def fetch_page(url: str, _timeout: float) -> str:
            identity = url.split("/data/")[1].split("/")[0]
            pages.append(identity)
            # Read the generation from the source checkpoint without depending
            # on a private output artifact path.
            checkpoint_path = next((self.fixture.state_dir / "sources/data_go_kr/generations").glob("*.json"))
            checkpoint = json.loads(checkpoint_path.read_text())
            durable_counts.append((
                checkpoint["request_reservation"]["attempts_made"],
                checkpoint["attempts_consumed"], dict(checkpoint["attempts_by_id"]),
            ))
            if identity == "2" and pages.count("2") == 1:
                raise TimeoutError("page retry fixture")
            return self.page(identity)

        def fetch_resolver(url: str, _timeout: float):
            identity = url.rsplit("=", 1)[1]
            resolvers.append(identity)
            checkpoint_path = next((self.fixture.state_dir / "sources/data_go_kr/generations").glob("*.json"))
            checkpoint = json.loads(checkpoint_path.read_text())
            durable_counts.append((
                checkpoint["request_reservation"]["attempts_made"],
                checkpoint["attempts_consumed"], dict(checkpoint["attempts_by_id"]),
            ))
            if identity == "3" and resolvers.count("3") == 1:
                raise TimeoutError("resolver retry fixture")
            return self.resolver_observation(identity)

        args = self.worker_args("701", max_attempts=6, max_queue=2, retries=2)
        code, completed = MODULE.process(args, fetcher=fetch_page, resolver_fetcher=fetch_resolver, sleeper=lambda _delay: None)
        self.assertEqual(code, 2)
        self.assertEqual(pages, ["2", "2", "3"])
        self.assertEqual(resolvers, ["2", "3", "3"])
        self.assertEqual([item[0] for item in durable_counts], [1, 2, 3, 4, 5, 6])
        self.assertEqual([item[1] for item in durable_counts], [6] * 6)
        self.assertTrue(all(item[2] == {"2": 3, "3": 3} for item in durable_counts))
        self.assertEqual(completed["request_reservation"]["attempts_made"], 6)
        self.assertEqual(completed["attempts_consumed"], 6)

    def test_resolver_timeout_replaces_prior_page_timeout_phase(self) -> None:
        self.prepare([self.row(2)])
        self.claim(max_attempts=3, max_queue=1, retries=2)
        pages: list[str] = []
        resolvers: list[str] = []

        def fetch_page(url: str, _timeout: float) -> str:
            pages.append(url)
            if len(pages) == 1:
                raise TimeoutError("page timeout detail must not persist")
            return self.page("2")

        def fetch_resolver(url: str, _timeout: float):
            resolvers.append(url)
            raise TimeoutError("resolver timeout detail must not persist")

        code, completed = MODULE.process(
            self.worker_args("701", max_attempts=3, max_queue=1, retries=2),
            fetcher=fetch_page, resolver_fetcher=fetch_resolver, sleeper=lambda _delay: None,
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(resolvers), 1)
        self.assertEqual(completed["request_reservation"]["attempts_made"], 3)
        self.assertEqual(completed["attempts_consumed"], 3)
        expected = {"code": "timeout", "phase": "resolver"}
        self.assertEqual(completed["detail_records"][-1]["failure_diagnostic"], expected)
        self.assertEqual(completed["detail_records"][-1]["source_sha256"], MODULE.source_fingerprint(self.row(2)))
        self.assertEqual(completed["detail_records"][-1]["guide_sha256"], MODULE.guide_fingerprint(self.row(2)))
        evidence = json.loads((self.fixture.output_dir / "upstream-catalogue-enrichment-evidence.json").read_text())
        self.assertEqual(evidence["worker_outcomes"][0]["failure_diagnostic"], expected)
        index_value = json.loads((self.fixture.state_dir / "sources/data_go_kr/index.json").read_text())
        retry = index_value["detail_retry_state"][MODULE.record_id(self.row(2))]
        self.assertEqual(retry["failure_diagnostic"], expected)
        serialized = json.dumps(completed) + json.dumps(evidence) + json.dumps(index_value)
        self.assertNotIn("timeout detail", serialized)

    def test_interruption_at_page_resolver_and_post_response_keeps_precharged_slots(self) -> None:
        cases = (("before", 1, 0, 0), ("between", 1, 1, 0), ("after", 2, 1, 1))
        for mode, expected_made, expected_pages, expected_resolvers in cases:
            with self.subTest(mode=mode):
                self.setUp()
                self.prepare([self.row(2)])
                self.claim(max_attempts=3, max_queue=1, retries=2)
                checkpoint_path = next((self.fixture.state_dir / "sources/data_go_kr/generations").glob("*.json"))
                page_calls: list[str] = []
                resolver_calls: list[str] = []

                def page_fetch(url: str, _timeout: float) -> str:
                    page_calls.append(url)
                    return self.page("2")

                def resolver_fetch(url: str, _timeout: float):
                    resolver_calls.append(url)
                    return self.resolver_observation("2")

                args = self.worker_args("701", max_attempts=3, max_queue=1, retries=2)
                if mode == "before":
                    # The reservation is checkpointed before URL construction
                    # and before the page transport callback starts.
                    original_url = MODULE.safe_public_page_url
                    url_calls = 0

                    def interrupt_before_page_transport(identity: str) -> str:
                        nonlocal url_calls
                        url_calls += 1
                        if url_calls == 2:
                            raise Interrupted()
                        return original_url(identity)

                    with mock.patch.object(MODULE, "safe_public_page_url", side_effect=interrupt_before_page_transport):
                        with self.assertRaises(Interrupted):
                            MODULE.process(args, fetcher=page_fetch, resolver_fetcher=resolver_fetch, sleeper=lambda _delay: None)
                else:
                    original_write = MODULE.atomic_write_json
                    writes = 0
                    stop_before_write = 3 if mode == "between" else 4

                    def interrupt_at_boundary(path: pathlib.Path, value: dict) -> None:
                        nonlocal writes
                        writes += 1
                        # Writes 1 and 2 persist worker setup and page charge.
                        # Stop before resolver dispatch, or before finalizing
                        # after both calls, depending on the chosen window.
                        if writes == stop_before_write:
                            raise Interrupted()
                        original_write(path, value)

                    with mock.patch.object(MODULE, "atomic_write_json", side_effect=interrupt_at_boundary):
                        with self.assertRaises(Interrupted):
                            MODULE.process(args, fetcher=page_fetch, resolver_fetcher=resolver_fetch, sleeper=lambda _delay: None)

                durable = json.loads(checkpoint_path.read_text())
                self.assertEqual(len(page_calls), expected_pages)
                self.assertEqual(len(resolver_calls), expected_resolvers)
                self.assertEqual(durable["request_reservation"]["attempts_made"], expected_made)
                self.assertEqual(durable["request_reservation"]["reserved_attempts"], 3)
                self.assertEqual(durable["attempts_consumed"], 3)
                self.assertEqual(durable["attempts_by_id"], {"2": 3})

    def test_state_branch_cas_conflict_keeps_the_claimed_identity_budget_exhausted_on_replay(self) -> None:
        self.prepare([self.row(2)])
        branch = workflow_tests.StateBranchWorkflowFixture("runTest")
        branch.setUp()
        self.addCleanup(branch.tearDown)
        original_state_dir = self.fixture.state_dir
        self.fixture.state_dir = branch.state / workflow_tests.STATE_ROOT

        try:
            old_sha, _ = branch.prepare()
            _claim_args, claim = self.claim(max_attempts=1, max_queue=1, retries=0)
            claim_push = branch.push(branch.state, old_sha)
            claim_sha = claim_push["new_sha"]
            self.assertTrue(claim_sha)

            # A concurrent writer advances the durable state branch after
            # the claim was committed but before this worker's state update.
            workflow_tests.git(branch.root, "clone", str(branch.remote), str(branch.competitor))
            workflow_tests.git(branch.competitor, "checkout", "--track", "-b", workflow_tests.BRANCH,
                               f"origin/{workflow_tests.BRANCH}")
            workflow_tests.git(branch.competitor, "config", "user.name", "fixture")
            workflow_tests.git(branch.competitor, "config", "user.email", "fixture@example.test")
            unrelated = branch.competitor / workflow_tests.STATE_ROOT / "quarantine" / f"{'f' * 64}.json"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text('{"concurrent":true}\n', encoding="utf-8")
            workflow_tests.git(branch.competitor, "add", workflow_tests.STATE_ROOT)
            workflow_tests.git(branch.competitor, "commit", "-m", "concurrent state writer")
            workflow_tests.git(branch.competitor, "push", "origin", f"HEAD:refs/heads/{workflow_tests.BRANCH}")
            winner_sha = workflow_tests.git(branch.competitor, "rev-parse", "HEAD").stdout.strip()

            page_calls: list[str] = []
            args = self.worker_args("701", max_attempts=1, max_queue=1, retries=0)
            code, local_worker = MODULE.process(
                args,
                fetcher=lambda url, _timeout: (page_calls.append(url) or self.page("2")),
                resolver_fetcher=lambda *_: self.fail("worker has no reserved resolver slot"),
                sleeper=lambda _delay: None,
            )
            self.assertEqual(code, 2)
            self.assertEqual(len(page_calls), 1)
            self.assertEqual(local_worker["request_reservation"]["attempts_made"], 1)
            self.assertEqual(local_worker["attempts_consumed"], 1)

            cas_result, _cas_payload = branch.run_state(
                "commit-push", "--worktree", str(branch.state), "--branch", workflow_tests.BRANCH,
                "--repository", workflow_tests.REPOSITORY, "--expected-old-sha", claim_sha,
                "--message", "stale worker state", ok=False,
            )
            self.assertNotEqual(cas_result.returncode, 0)
            self.assertIn("compare_and_swap_conflict", cas_result.stderr)
            self.assertEqual(
                workflow_tests.git(branch.competitor, "ls-remote", "origin", f"refs/heads/{workflow_tests.BRANCH}").stdout.split()[0],
                winner_sha,
            )

            # The worker's post-call checkpoint did not win its CAS. A fresh
            # owner rereads the committed reservation and cannot reclaim the
            # identity's precharged slot or issue free recovery traffic.
            replay = branch.root / "replay-state"
            fresh_sha, _ = branch.prepare(replay)
            self.assertEqual(fresh_sha, winner_sha)
            self.fixture.state_dir = replay / workflow_tests.STATE_ROOT
            self.fixture.now = "2026-10-01T11:00:00Z"
            replay_args = self.fixture.args(run_id="702", **{
                "--claim-only": None,
                "--max-attempts": 1,
                "--max-queue": 1,
                "--retries-per-detail": 0,
            })
            replay_code, replayed = MODULE.process(
                replay_args,
                fetcher=lambda *_: self.fail("exhausted replay must not refetch the page"),
                resolver_fetcher=lambda *_: self.fail("exhausted replay must not fetch the resolver"),
            )
            self.assertEqual(replay_code, 0, replayed.get("outcome"))
            self.assertEqual(replayed["attempts_by_id"], {"2": 1})
            self.assertEqual(replayed["attempts_consumed"], 1)
            self.assertEqual(replayed["request_reservation"]["reserved_attempts"], 0)
            self.assertEqual(replayed["request_reservation"]["records"], [])
        finally:
            self.fixture.state_dir = original_state_dir

    def test_seoul_declaration_state_branch_cas_conflict_keeps_both_reserved_calls_spent(self) -> None:
        subject = json.loads(
            (candidate_tests.ROOT / "data/data-go-kr.registry.json").read_text(encoding="utf-8"),
        )
        candidate = next(row for row in subject if row["id"] == "15056854")
        now = "2026-10-06T10:00:00Z"
        self.fixture.now = now
        self.fixture.write_real_composer_inputs([], [candidate], now)
        self.fixture.provider_index_path.write_bytes(
            (candidate_tests.ROOT / "data/provider-index.json").read_bytes(),
        )
        declaration = MODULE.SEOUL_DECLARATION
        detail_id = candidate["source"]["raw"]["id"]
        page_body = (
            "<button type=\"button\" onclick=\"fn_goUrlLink('15056854')\">Open</button>"
            "<input type=\"hidden\" id=\"publicDataPk\" value=\"15056854\">"
            f"<input type=\"hidden\" id=\"publicDataDetailPk\" value=\"{detail_id}\">"
        ).encode("utf-8")
        resolver_body = json.dumps({
            "publicDataDetailPk": detail_id,
            "linkUrl": declaration.DECLARATION["subject"]["source_metadata_target"],
            "status": True,
        }, separators=(",", ":")).encode("utf-8")

        branch = workflow_tests.StateBranchWorkflowFixture("runTest")
        branch.setUp()
        self.addCleanup(branch.tearDown)
        original_state_dir = self.fixture.state_dir
        self.fixture.state_dir = branch.state / workflow_tests.STATE_ROOT
        try:
            old_sha, _ = branch.prepare()
            claim_args = self.fixture.args(run_id="701", **{
                "--claim-only": None,
                "--max-attempts": 2,
                "--max-queue": 1,
                "--retries-per-detail": 1,
            })
            claim_code, claim = MODULE.process(
                claim_args,
                fetcher=lambda *_: self.fail("claim must reserve without a page request"),
                resolver_fetcher=lambda *_: self.fail("claim must reserve without resolver I/O"),
            )
            self.assertEqual(claim_code, 0, claim)
            self.assertEqual(claim["attempts_by_id"], {"15056854": 2})
            self.assertEqual(claim["request_reservation"]["reserved_attempts"], 2)
            claim_push = branch.push(branch.state, old_sha)
            claim_sha = claim_push["new_sha"]

            workflow_tests.git(branch.root, "clone", str(branch.remote), str(branch.competitor))
            workflow_tests.git(branch.competitor, "checkout", "--track", "-b", workflow_tests.BRANCH,
                               f"origin/{workflow_tests.BRANCH}")
            workflow_tests.git(branch.competitor, "config", "user.name", "fixture")
            workflow_tests.git(branch.competitor, "config", "user.email", "fixture@example.test")
            unrelated = branch.competitor / workflow_tests.STATE_ROOT / "quarantine" / f"{'f' * 64}.json"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text('{"concurrent":true}\n', encoding="utf-8")
            workflow_tests.git(branch.competitor, "add", workflow_tests.STATE_ROOT)
            workflow_tests.git(branch.competitor, "commit", "-m", "concurrent state writer")
            workflow_tests.git(branch.competitor, "push", "origin", f"HEAD:refs/heads/{workflow_tests.BRANCH}")
            winner_sha = workflow_tests.git(branch.competitor, "rev-parse", "HEAD").stdout.strip()

            physical_calls: list[str] = []

            def fetch_page(url: str, _timeout: float):
                self.assertEqual(url, declaration.PAGE_URL)
                physical_calls.append("page")
                return MODULE.DetailPageObservation(
                    body=page_body.decode("utf-8"), page_url=url, effective_url=url,
                    page_sha256=MODULE.sha256_bytes(page_body), observed_at=now, page_bytes=page_body,
                )

            def fetch_resolver(url: str, _timeout: float):
                self.assertEqual(url, declaration.RESOLVER_URL)
                physical_calls.append("resolver")
                return MODULE.LinkResolverObservation(
                    body=resolver_body, request_url=url, effective_url=url, observed_at=now,
                )

            worker_args = self.fixture.args(run_id="701", **{
                "--max-attempts": 2,
                "--max-queue": 1,
                "--retries-per-detail": 1,
                "--require-durable-reservation": None,
            })
            worker_code, worker = MODULE.process(
                worker_args, fetcher=fetch_page, resolver_fetcher=fetch_resolver, sleeper=lambda _delay: None,
            )
            self.assertEqual(worker_code, 0, worker)
            self.assertEqual(physical_calls, ["page", "resolver"])
            self.assertEqual(worker["request_reservation"]["attempts_made"], 2)
            self.assertEqual(worker["attempts_consumed"], 2)

            stale_push, _ = branch.run_state(
                "commit-push", "--worktree", str(branch.state), "--branch", workflow_tests.BRANCH,
                "--repository", workflow_tests.REPOSITORY, "--expected-old-sha", claim_sha,
                "--message", "stale Seoul worker result", ok=False,
            )
            self.assertNotEqual(stale_push.returncode, 0)
            self.assertIn("compare_and_swap_conflict", stale_push.stderr)
            remote_sha = workflow_tests.git(
                branch.competitor, "ls-remote", "origin", f"refs/heads/{workflow_tests.BRANCH}",
            ).stdout.split()[0]
            self.assertEqual(remote_sha, winner_sha)

            replay_state = branch.root / "replay-state"
            fresh_sha, _ = branch.prepare(replay_state)
            self.assertEqual(fresh_sha, winner_sha)
            self.fixture.state_dir = replay_state / workflow_tests.STATE_ROOT
            self.fixture.now = "2026-10-06T11:00:00Z"
            replay_args = self.fixture.args(run_id="702", **{
                "--claim-only": None,
                "--max-attempts": 2,
                "--max-queue": 1,
                "--retries-per-detail": 1,
            })
            replay_code, replay = MODULE.process(
                replay_args,
                fetcher=lambda *_: self.fail("replayed declaration must not refetch the page"),
                resolver_fetcher=lambda *_: self.fail("replayed declaration must not refetch the resolver"),
            )
            self.assertEqual(replay_code, 0, replay)
            self.assertEqual(replay["attempts_by_id"], {"15056854": 2})
            self.assertEqual(replay["attempts_consumed"], 2)
            self.assertEqual(replay["request_reservation"]["reserved_attempts"], 0)
            self.assertEqual(replay["request_reservation"]["records"], [])
        finally:
            self.fixture.state_dir = original_state_dir


if __name__ == "__main__":
    unittest.main()
