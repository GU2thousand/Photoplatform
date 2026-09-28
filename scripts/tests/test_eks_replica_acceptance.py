"""Replica harness boundary/routing tests; no AWS, kubeconfig, network or remote-quality claims."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import types
import unittest
from unittest.mock import Mock, patch

from scripts import eks_replica_acceptance as replica


ARN = "arn:aws:eks:us-east-1:012345678901:cluster/dev"
SHA = "a" * 40
PODS = [{"name": f"photoplatform-api-{index}", "uid": f"00000000-0000-4000-8000-{index:012d}",
         "phase": "Running", "ready": True, "terminating": False,
         "image": "account.ecr/api@sha256:" + "b" * 64, "imageIDs": ["containerd://sha256:" + "b" * 64]}
        for index in (1, 2)]
INITIAL = {"uid": "deployment-uid", "replicas": 2, "readyReplicas": 2, "updatedReplicas": 2,
           "generation": 1, "observedGeneration": 1, "pods": PODS}
ACCOUNTS = [{"id": 11, "name": "Fixture A", "email": "a@fixture.example", "role": "USER"},
            {"id": 12, "name": "Fixture B", "email": "b@fixture.example", "role": "USER"}]
TEAM = {"id": 9, "memberCount": 2, "members": [{"id": 11}, {"id": 12}]}
ENV = {"TEAM_A_TOKEN": "PRIVATE-A", "TEAM_B_TOKEN": "PRIVATE-B", "TEAM_ID": "9",
       "API_URL": "https://dev.example.com", "CLOUD_FRONTEND_ORIGIN": "https://frontend.example.com"}


class SocketTimeout(Exception):
    pass


class SocketClosed(Exception):
    pass


def websocket_module(create=None):
    return types.SimpleNamespace(create_connection=create or Mock(), WebSocketTimeoutException=SocketTimeout,
        WebSocketConnectionClosedException=SocketClosed, ABNF=types.SimpleNamespace(OPCODE_CLOSE=8))


def control():
    subject = Mock(arn=ARN, namespace="photoplatform-dev", namespace_uid="namespace-uid", sha=SHA)
    subject.state.return_value = copy.deepcopy(INITIAL)
    subject.json.return_value = {"metadata": {"uid": "namespace-uid", "labels": {
        "app.kubernetes.io/part-of": "photoplatform", "photoplatform.io/environment": "dev",
        "photoplatform.io/disposable": "true"}}}
    return subject


def ticket(claims=None, **changes):
    issued = int(time.time())
    payload = claims or {"iat": issued, "exp": issued + 60, "purpose": "team-socket", "userId": 11, "teamId": 9}
    payload = {**payload, **changes}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {"ticket": "HEADER." + encoded + ".SIGNATURE",
            "expiresAt": replica.dt.datetime.fromtimestamp(payload["exp"], replica.dt.timezone.utc).isoformat()}


class PodRoutingTests(unittest.TestCase):
    def test_replica_selection_requires_settled_running_distinct_uids_and_never_scales(self):
        subject = control()
        state, selected = replica.select_api_pods(subject)
        self.assertEqual([p["uid"] for p in selected], [p["uid"] for p in PODS])
        for alter in (lambda s: s.update(readyReplicas=1), lambda s: s.update(observedGeneration=0),
                      lambda s: s["pods"][1].update(uid=s["pods"][0]["uid"]),
                      lambda s: s["pods"][1].update(terminating=True)):
            broken = copy.deepcopy(INITIAL)
            alter(broken)
            subject.state.return_value = broken
            with self.assertRaises(ValueError):
                replica.select_api_pods(subject)
        subject.json.assert_not_called()

    def test_replaced_pod_or_deployment_refuses_direct_requests(self):
        subject = control()
        for change in (lambda s: s.update(uid="different"),
                       lambda s: s["pods"][0].update(uid=PODS[1]["uid"]),
                       lambda s: s["pods"][0].update(ready=False)):
            state = copy.deepcopy(INITIAL)
            change(state)
            subject.state.return_value = state
            with self.assertRaises(ValueError):
                replica.verify_pinned_pod(subject, PODS[0], INITIAL["uid"])

    def test_port_forward_targets_one_pinned_pod_on_loopback_and_kills_a_stuck_child(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("kubectl", 3), 0]
        def start(args, **kwargs):
            kwargs["stdout"].write(b"Forwarding from 127.0.0.1:31001 -> 8080\n")
            kwargs["stdout"].flush()
            return child
        with patch.object(replica, "verify_pinned_pod", return_value=INITIAL) as verify, \
                patch.object(replica.subprocess, "Popen", side_effect=start) as popen, \
                patch.object(replica.socket, "create_connection", return_value=Mock(__enter__=Mock(), __exit__=Mock())), \
                patch.object(replica.threading, "Timer") as timer:
            with replica.PodForward(control(), PODS[0], INITIAL["uid"], replica.Deadline(60)) as forward:
                evidence = forward.evidence()
                self.assertEqual(forward.base, "http://127.0.0.1:31001")
            forward.close()
        args = popen.call_args.args[0]
        self.assertEqual(args[:5], ["kubectl", "--context", ARN, "--namespace", "photoplatform-dev"])
        self.assertIn("--address=127.0.0.1", args)
        self.assertEqual(args[-2:], ["pod/photoplatform-api-1", ":8080"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(evidence["podUid"], PODS[0]["uid"])
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(0 < timer.call_args.args[0] <= 60)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertTrue(forward.log.closed)

    def test_uid_change_after_forward_start_closes_child_without_returning_target(self):
        child = Mock()
        child.poll.return_value = None
        def start(args, **kwargs):
            kwargs["stdout"].write(b"Forwarding from 127.0.0.1:31002 -> 8080\n")
            kwargs["stdout"].flush()
            return child
        with patch.object(replica, "verify_pinned_pod", side_effect=[INITIAL, ValueError("changed UID")]), \
                patch.object(replica.subprocess, "Popen", side_effect=start), \
                patch.object(replica.socket, "create_connection", return_value=Mock(__enter__=Mock(), __exit__=Mock())), \
                patch.object(replica.threading, "Timer"):
            with self.assertRaises(ValueError):
                with replica.PodForward(control(), PODS[0], INITIAL["uid"], replica.Deadline(60)):
                    self.fail("Changed UID must not become a usable API endpoint")
        child.terminate.assert_called_once()

    def test_http_client_disables_proxies_redirects_and_nonexistent_auth_ticket_route(self):
        session = Mock()
        session.request.return_value = Mock(status_code=200, json=Mock(return_value=ACCOUNTS[0]))
        forward = Mock(base="http://127.0.0.1:31001", control=control(), pod=PODS[0], deployment_uid=INITIAL["uid"])
        with patch.dict("sys.modules", {"requests": types.SimpleNamespace(Session=Mock(return_value=session))}):
            api = replica.PodAPI(forward, replica.Deadline(60))
            self.assertEqual(api.request("GET", "/api/auth/me", "PRIVATE-A"), ACCOUNTS[0])
            with self.assertRaises(ValueError):
                api.request("POST", "/api/auth/socket-ticket", "PRIVATE-A")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.request.call_args.args[:2], ("GET", "http://127.0.0.1:31001/api/auth/me"))
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.request.call_count, 1)


class FixtureAndSocketTests(unittest.TestCase):
    def test_administrator_and_nonmembers_cannot_masquerade_as_team_fixture(self):
        api = Mock()
        api.request.return_value = {**ACCOUNTS[0], "role": "ADMIN"}
        with self.assertRaises(ValueError):
            replica.profile(api, "PRIVATE-A")
        for team in ({**TEAM, "members": [{"id": 11}, {"id": 13}]},
                     {**TEAM, "members": [{"id": 11}, {"id": 11}]}, {**TEAM, "memberCount": 3}):
            with self.assertRaises(ValueError):
                replica.validate_membership(team, 9, [11, 12])
        self.assertEqual(replica.validate_membership(TEAM, 9, [11, 12]), [11, 12])

    def test_only_actual_team_route_fresh_user_team_bound_60_second_ticket_is_accepted(self):
        api = Mock()
        api.request.return_value = ticket()
        encoded, evidence = replica.socket_ticket(api, "PRIVATE-A", 9, 11)
        self.assertEqual(api.request.call_args.args, ("POST", "/api/teams/9/socket-ticket", "PRIVATE-A"))
        self.assertNotIn(encoded, json.dumps(evidence))
        self.assertEqual(evidence["lifetimeSeconds"], 60)
        for changes in ({"purpose": "access"}, {"teamId": 10}, {"userId": 12},
                        {"exp": int(time.time()) + 3600}, {"iat": int(time.time()) - 61, "exp": int(time.time()) - 1}):
            api.request.return_value = ticket(**changes)
            with self.assertRaises(ValueError):
                replica.socket_ticket(api, "PRIVATE-A", 9, 11)

    def test_socket_uses_real_ws_team_route_target_pod_and_excludes_proxy(self):
        create = Mock(return_value=Mock(status=101))
        target = Mock(forward=Mock(base="http://127.0.0.1:31002", control=control(), pod=PODS[1], deployment_uid=INITIAL["uid"]))
        with patch.dict("sys.modules", {"websocket": websocket_module(create)}):
            replica.open_socket(target, "SECRET/TICKET", 9, "https://frontend.example.com", replica.Deadline(30))
        self.assertEqual(create.call_args.args[0], "ws://127.0.0.1:31002/ws/teams/9?ticket=SECRET%2FTICKET")
        self.assertEqual(create.call_args.kwargs["http_no_proxy"], ["127.0.0.1", "localhost"])
        self.assertIsNone(create.call_args.kwargs["http_proxy_host"])
        self.assertEqual(create.call_args.kwargs["redirect_limit"], 0)

    def test_socket_redirect_response_is_never_followed_or_accepted_as_a_connection(self):
        ws = Mock(status=302)
        create = Mock(return_value=ws)
        target = Mock(forward=Mock(base="http://127.0.0.1:31002", control=control(), pod=PODS[1], deployment_uid=INITIAL["uid"]))
        with patch.dict("sys.modules", {"websocket": websocket_module(create)}):
            with self.assertRaises(RuntimeError):
                replica.open_socket(target, "PRIVATE-TICKET", 9, "https://frontend.example.com", replica.Deadline(30))
        self.assertEqual(create.call_args.kwargs["redirect_limit"], 0)
        ws.shutdown.assert_called_once()

    def test_relay_requires_exact_actor_team_event_and_never_resends_on_timeout(self):
        sender, receiver = Mock(), Mock()
        def response():
            marker = sender.send.call_args.args[0]
            return json.dumps({"type": "NOTE", "teamId": 9, "actorName": "Fixture A",
                               "message": "Fixture A: " + marker, "occurredAt": "2026-09-28T01:00:00Z"})
        sender.recv.side_effect = response
        attempts = 0
        def receive():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SocketTimeout()
            if attempts == 2:
                return json.dumps({"type": "NOTE", "message": "other"})
            return response()
        receiver.recv.side_effect = receive
        with patch.dict("sys.modules", {"websocket": websocket_module()}):
            result = replica.relay(sender, receiver, ACCOUNTS[0], 9, replica.Deadline(30))
            broken = Mock()
            broken.recv.return_value = json.dumps({"type": "NOTE", "teamId": 99, "actorName": "Fixture A",
                "message": "Fixture A: marker", "occurredAt": "timestamp"})
            with self.assertRaises(ValueError):
                replica.expect_note(broken, "marker", "Fixture A", 9, replica.Deadline(30))
        sender.send.assert_called_once()
        self.assertEqual(result["remote"]["ignoredOtherEvents"], 1)

    def test_fixture_checks_both_user_accounts_and_membership_on_each_pod(self):
        def request(method, path, token):
            return ACCOUNTS[0] if path == "/api/auth/me" and token == "PRIVATE-A" else \
                ACCOUNTS[1] if path == "/api/auth/me" else TEAM
        apis = [Mock(request=Mock(side_effect=request)), Mock(request=Mock(side_effect=request))]
        suite = replica.ReplicaSuite(control(), INITIAL, PODS, apis, Mock(), replica.Deadline(60),
                                     "https://frontend.example.com", {"cases": []}, "unused.json")
        with patch.dict(os.environ, ENV, clear=True):
            result = suite.fixture()
        self.assertEqual(result["userIds"], [11, 12])
        for api in apis:
            self.assertEqual([call.args for call in api.request.call_args_list],
                [("GET", "/api/auth/me", "PRIVATE-A"), ("GET", "/api/auth/me", "PRIVATE-B"),
                 ("GET", "/api/teams/9", "PRIVATE-A"), ("GET", "/api/teams/9", "PRIVATE-B")])


class ApiFaultBoundaryTests(unittest.TestCase):
    def test_fault_requires_opt_in_unchanged_namespace_pod_and_surviving_replica(self):
        subject = control()
        with patch.object(replica, "kubectl") as command:
            with patch.dict(os.environ, {"ALLOW_EKS_FAILURE_INJECTION": "0"}, clear=True):
                with self.assertRaises(ValueError):
                    replica.delete_pinned_api_pod(subject, PODS[0], INITIAL, replica.Deadline(30))
            with patch.dict(os.environ, {"ALLOW_EKS_FAILURE_INJECTION": "1"}, clear=True):
                subject.json.return_value["metadata"]["uid"] = "replaced"
                with self.assertRaises(ValueError):
                    replica.delete_pinned_api_pod(subject, PODS[0], INITIAL, replica.Deadline(30))
                subject = control()
                subject.state.return_value["pods"][1]["ready"] = False
                with self.assertRaises(ValueError):
                    replica.delete_pinned_api_pod(subject, PODS[0], INITIAL, replica.Deadline(30))
            command.assert_not_called()

    def test_fault_uses_server_side_uid_precondition_on_exact_selected_api_pod(self):
        with patch.dict(os.environ, {"ALLOW_EKS_FAILURE_INJECTION": "1"}), patch.object(replica, "kubectl") as command:
            result = replica.delete_pinned_api_pod(control(), PODS[0], INITIAL, replica.Deadline(30))
        self.assertEqual(command.call_args.args,
            (ARN, "photoplatform-dev", "delete", "--raw", "/api/v1/namespaces/photoplatform-dev/pods/photoplatform-api-1", "-f", "-"))
        body = command.call_args.kwargs["body"]
        self.assertEqual(body["preconditions"], {"uid": PODS[0]["uid"]})
        self.assertNotIn("gracePeriodSeconds", body)
        self.assertEqual(result["podUid"], PODS[0]["uid"])

    def test_expired_window_refuses_fault_before_guard_reads_or_delete_dispatch(self):
        subject = control()
        with patch.dict(os.environ, {"ALLOW_EKS_FAILURE_INJECTION": "1"}), patch.object(replica, "kubectl") as command:
            with self.assertRaises(TimeoutError):
                replica.delete_pinned_api_pod(subject, PODS[0], INITIAL, replica.Deadline(0))
        subject.json.assert_not_called()
        subject.state.assert_not_called()
        command.assert_not_called()

    def test_recovery_requires_real_new_uid_and_unchanged_survivor_and_deployment(self):
        subject = control()
        replacement = {**PODS[0], "name": "photoplatform-api-3", "uid": "00000000-0000-4000-8000-000000000003"}
        recovered = {**INITIAL, "pods": [replacement, PODS[1]]}
        subject.state.return_value = recovered
        selected, state = replica.wait_replacement(subject, INITIAL, PODS[0], PODS[1], replica.Deadline(30))
        self.assertEqual(selected["uid"], replacement["uid"])
        for changed in ({**recovered, "uid": "new-deployment"},
                        {**recovered, "pods": [replacement, {**PODS[1], "uid": "replaced-survivor"}]},
                        {**recovered, "replicas": 3}):
            subject.state.return_value = changed
            with self.assertRaises(ValueError):
                replica.wait_replacement(subject, INITIAL, PODS[0], PODS[1], replica.Deadline(30))

    def test_disconnect_keeps_actual_close_code_or_explicit_transport_limitation(self):
        ws = Mock()
        ws.recv_data.return_value = (8, (1012).to_bytes(2, "big"))
        with patch.dict("sys.modules", {"websocket": websocket_module()}):
            self.assertEqual(replica.observed_disconnect(ws, replica.Deadline(30)),
                             {"observation": "close_frame", "closeCode": 1012})
            ws.recv_data.side_effect = SocketClosed()
            self.assertEqual(replica.observed_disconnect(ws, replica.Deadline(30))["observation"],
                             "port_forward_transport_closed")


class PublicAndEvidenceTests(unittest.TestCase):
    def make_suite(self):
        apis = [Mock(request=Mock(return_value=ACCOUNTS[0])), Mock(request=Mock(return_value=ACCOUNTS[0]))]
        report = {"cases": []}
        return replica.ReplicaSuite(control(), INITIAL, PODS, apis, Mock(), replica.Deadline(60),
            "https://frontend.example.com", report, "unused.json")

    def test_public_distribution_requires_two_live_release_uids_and_every_header_revision(self):
        suite = self.make_suite()
        count = 0
        sessions = []
        def new_session():
            nonlocal count
            response = Mock(status_code=200, json=Mock(return_value=ACCOUNTS[0]),
                headers={"X-Photoplatform-Pod-Uid": PODS[count % 2]["uid"], "X-Photoplatform-Revision": SHA})
            count += 1
            session = Mock(get=Mock(return_value=response))
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
            sessions.append(session)
            return session
        with patch.dict(os.environ, ENV), patch.dict("sys.modules", {"requests": types.SimpleNamespace(Session=new_session)}), \
                patch.object(replica, "write_report"):
            result = suite.public_distribution()
        self.assertEqual(result["attempted"], 30)
        self.assertEqual(result["distinctPodUids"], [p["uid"] for p in PODS])
        self.assertEqual(len(sessions), 30)
        for session in sessions:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_args.kwargs["headers"]["Connection"], "close")
        self.assertNotIn("PRIVATE-A", json.dumps(suite.report))

    def test_missing_header_or_one_pod_cannot_pass_and_all_failed_samples_remain(self):
        for headers in ({}, {"X-Photoplatform-Pod-Uid": PODS[0]["uid"], "X-Photoplatform-Revision": SHA},
                        {"X-Photoplatform-Pod-Uid": PODS[1]["uid"], "X-Photoplatform-Revision": "b" * 40}):
            with self.subTest(headers=headers):
                suite = self.make_suite()
                session = Mock(get=Mock(return_value=Mock(status_code=200, json=Mock(return_value=ACCOUNTS[0]), headers=headers)))
                session.__enter__ = Mock(return_value=session)
                session.__exit__ = Mock(return_value=False)
                with patch.dict(os.environ, ENV), patch.dict("sys.modules", {"requests": types.SimpleNamespace(Session=Mock(return_value=session))}), \
                        patch.object(replica, "write_report"):
                    with self.assertRaises(AssertionError):
                        suite.public_distribution()
                self.assertEqual(len(suite.report["publicAlbSamples"]), 30)

    def test_failed_case_retains_type_and_never_credential_bearing_exception_message(self):
        suite = self.make_suite()
        def failed():
            raise RuntimeError("Bearer SECRET https://signed.example/?ticket=PRIVATE-TICKET")
        with patch.object(replica, "write_report"):
            self.assertFalse(suite.case("actual-check", failed))
        evidence = json.dumps(suite.report)
        self.assertNotIn("SECRET", evidence)
        self.assertNotIn("PRIVATE-TICKET", evidence)
        self.assertEqual(suite.report["cases"][0]["errorType"], "RuntimeError")

    def test_missing_team_fixture_is_incomplete_and_complete_subset_never_certifies_full_p6(self):
        cases = [{"name": name, "status": "PASS"} for name in (*replica.REQUIRED_CASES, replica.PUBLIC_CASE)]
        self.assertEqual(replica.selected_status(cases), "PASS")
        self.assertEqual(replica.selected_status(cases, restart_requested=True), "INCOMPLETE")
        matrix = replica.matrix_results(cases)
        self.assertEqual(next(row for row in matrix if row["scenario"] == "cross_pod_websocket_and_reconnect")["status"], "INCOMPLETE")
        self.assertTrue(any(row["status"] == "NOT_RUN" for row in matrix))
        cases[1]["status"] = "NOT_RUN"
        self.assertEqual(replica.selected_status(cases), "INCOMPLETE")

    def test_restart_switch_is_checked_before_aws_guard_or_port_forward(self):
        writes = []
        with patch.dict(os.environ, {}, clear=True), patch.object(replica, "eks_guard") as guard, \
                patch.object(replica, "PodForward") as forward, patch.object(replica, "write_report", side_effect=lambda path, data: writes.append(copy.deepcopy(data))), \
                patch("sys.argv", ["eks_replica_acceptance.py", "--restart-pod-a"]):
            self.assertEqual(replica.main(), 1)
        guard.assert_not_called()
        forward.assert_not_called()
        self.assertEqual(writes[-1]["fatalErrorType"], "ValueError")
        self.assertEqual(writes[-1]["matrixStatus"], "INCOMPLETE")

    def test_final_ready_state_cannot_certify_unrequested_pod_replacements(self):
        replica.verify_final_api_state(INITIAL, INITIAL, [], False)
        changed = copy.deepcopy(INITIAL)
        changed["pods"][0]["uid"] = "00000000-0000-4000-8000-000000000099"
        with self.assertRaises(ValueError):
            replica.verify_final_api_state(changed, INITIAL, [], False)
        restart = {"name": replica.RESTART_CASE, "status": "PASS", "evidence": {
            "fault": {"podUid": PODS[0]["uid"]}, "replacementPod": changed["pods"][0]}}
        replica.verify_final_api_state(changed, INITIAL, [restart], True)
        changed["pods"][1]["uid"] = "00000000-0000-4000-8000-000000000098"
        with self.assertRaises(ValueError):
            replica.verify_final_api_state(changed, INITIAL, [restart], True)

    def test_hard_window_interrupts_blocking_guard_work_and_restores_signal_handlers(self):
        old_term = replica.signal.getsignal(replica.signal.SIGTERM)
        old_alarm = replica.signal.getsignal(replica.signal.SIGALRM)
        window = replica.ProcessWindow(replica.Deadline(.03))
        started = time.monotonic()
        try:
            window.start()
            with self.assertRaises(replica.HarnessDeadlineExpired):
                time.sleep(1)
            with self.assertRaises(replica.HarnessTerminated):
                replica.signal.getsignal(replica.signal.SIGTERM)(replica.signal.SIGTERM, None)
        finally:
            window.close()
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(replica.signal.getsignal(replica.signal.SIGTERM), old_term)
        self.assertEqual(replica.signal.getsignal(replica.signal.SIGALRM), old_alarm)

    def test_hard_timeout_and_termination_are_never_swallowed_by_case_failure_handling(self):
        for error_type in (replica.HarnessDeadlineExpired, replica.HarnessTerminated):
            suite = self.make_suite()
            def interrupted():
                raise error_type("interrupted")
            with patch.object(replica, "write_report"), self.assertRaises(error_type):
                suite.case("interrupted", interrupted)
            self.assertEqual(suite.report["cases"][0]["status"], "FAIL")
            self.assertEqual(suite.report["cases"][0]["errorType"], error_type.__name__)

    def test_missing_team_fixture_does_not_run_socket_cases_or_get_falsely_certified(self):
        writes = []
        forward = Mock()
        forward.__enter__ = Mock(return_value=forward)
        forward.__exit__ = Mock(return_value=False)
        forward.evidence.return_value = {"listener": "127.0.0.1"}
        api = Mock(request=Mock(return_value=ACCOUNTS[0]))
        env = {"TEST_OWNER_TOKEN": "PRIVATE-OWNER", "CLOUD_FRONTEND_ORIGIN": "https://frontend.example.com"}
        with patch.dict(os.environ, env, clear=True), patch.object(replica, "eks_guard", return_value=(Mock(), control(), {})), \
                patch.object(replica, "PodForward", return_value=forward), patch.object(replica, "PodAPI", return_value=api), \
                patch.object(replica.ReplicaSuite, "public_distribution", return_value={"unitTestOnly": True}), \
                patch.object(replica, "open_socket") as open_socket, \
                patch.object(replica, "write_report", side_effect=lambda path, data: writes.append(copy.deepcopy(data))), \
                patch("sys.argv", ["eks_replica_acceptance.py"]):
            self.assertEqual(replica.main(), 2)
        open_socket.assert_not_called()
        report = writes[-1]
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["matrixStatus"], "INCOMPLETE")
        team_case = next(case for case in report["cases"] if case["name"] == replica.REQUIRED_CASES[1])
        self.assertEqual(team_case["status"], "NOT_RUN")
        self.assertIn("TEAM_A_TOKEN", team_case["reason"])
        self.assertNotIn("PRIVATE-OWNER", json.dumps(report))

    def test_live_eks_identity_failure_never_starts_forward_or_leaks_sdk_exception(self):
        writes = []
        with patch.dict(os.environ, ENV, clear=True), \
                patch.object(replica, "eks_guard", side_effect=RuntimeError("Bearer PRIVATE-A signed-url?token=secret")), \
                patch.object(replica, "PodForward") as forward, \
                patch.object(replica, "write_report", side_effect=lambda path, data: writes.append(copy.deepcopy(data))), \
                patch("sys.argv", ["eks_replica_acceptance.py"]):
            self.assertEqual(replica.main(), 1)
        forward.assert_not_called()
        self.assertEqual(writes[-1]["fatalErrorType"], "RuntimeError")
        self.assertNotIn("PRIVATE-A", json.dumps(writes[-1]))


if __name__ == "__main__":
    unittest.main()
