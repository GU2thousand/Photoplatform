#!/usr/bin/env python3
"""Guarded real EKS dev multi-Pod auth, team relay and reconnect acceptance.

Requires two already Ready API replicas; never scales or provisions resources.
TEAM_A_TOKEN and TEAM_B_TOKEN must identify distinct USER accounts already in
TEAM_ID. TEST_OWNER_TOKEN can run JWT checks when the team fixture is missing,
but missing team cases remain NOT_RUN and the command exits 2 (incomplete).

--restart-pod-a additionally requires ALLOW_EKS_FAILURE_INJECTION=1, deletes only
the selected API Pod with a server-side UID precondition, then checks a fresh
ticket and relay on its verified replacement. Socket interoperability uses
loopback-only, bounded direct Pod port-forwards. Thirty separate public HTTPS
connections additionally require valid JWT responses from at least two actual
Pod UIDs, using opt-in dev instance headers. These observations do not certify
ingress/browser WebSocket reconnect or the complete P6 acceptance matrix.
"""
import argparse
import base64
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import required, secure_origin, write_report
from scripts.eks_acceptance import MATRIX
from scripts.eks_common import eks_guard, kubectl, safe_name


REQUIRED_CASES = ("valid_jwt_on_distinct_api_pods", "team_fixture_membership_on_both_api_pods",
    "ticket_from_pod_a_accepted_on_pod_b", "ticket_from_pod_b_accepted_on_pod_a",
    "cross_pod_team_notes_bidirectional", "fresh_ticket_client_reconnect_to_other_pod")
RESTART_CASE = "api_pod_restart_fresh_ticket_reconnect"
PUBLIC_CASE = "public_alb_jwt_distribution"


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Expected an actual positive integer identity")
    return value


def select_api_pods(control):
    state = control.state("api")
    pods = sorted((p for p in state["pods"] if p["ready"] and not p["terminating"]
                   and p["phase"] == "Running"), key=lambda p: p["name"])
    if (state["replicas"] < 2 or state["readyReplicas"] != state["replicas"] or
            state["updatedReplicas"] != state["replicas"] or
            state["observedGeneration"] < state["generation"] or len(pods) != state["replicas"]):
        raise ValueError("Two settled verified Ready API replicas are required; this harness never scales")
    if len({p["name"] for p in pods}) != len(pods) or len({p["uid"] for p in pods}) != len(pods):
        raise ValueError("API Pod names and UIDs must be distinct")
    for pod in pods:
        safe_name(pod["name"])
        uuid.UUID(pod["uid"])
    return state, pods[:2]


def verify_pinned_pod(control, pod, deployment_uid):
    state = control.state("api")  # Rechecks revision, owner chain and runnable image digest.
    matches = [p for p in state["pods"] if p["name"] == pod["name"] and p["uid"] == pod["uid"]]
    if (state["uid"] != deployment_uid or len(matches) != 1 or not matches[0]["ready"]
            or matches[0]["terminating"] or matches[0]["phase"] != "Running"):
        raise ValueError("Pinned API Deployment/Pod UID changed or the Pod is not Ready")
    return state


class Deadline:
    def __init__(self, seconds):
        self.ends = time.monotonic() + seconds

    def remaining(self, maximum=None):
        remaining = self.ends - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("EKS replica acceptance deadline exceeded")
        return min(remaining, maximum) if maximum is not None else remaining


class HarnessTerminated(BaseException):
    """Raise on SIGTERM so ExitStack closes kubectl children before process exit."""


class HarnessDeadlineExpired(BaseException):
    """Cannot be swallowed by a per-case or SDK Exception catch/retry loop."""


class ProcessWindow:
    """Unix CLI hard deadline also covers AWS and fixed-timeout kubectl guard calls."""
    def __init__(self, deadline):
        self.deadline, self.previous = deadline, None

    @staticmethod
    def terminated(*_):
        raise HarnessTerminated("EKS acceptance process received termination")

    @staticmethod
    def expired(*_):
        raise HarnessDeadlineExpired("EKS acceptance hard deadline exceeded")

    def start(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValueError("EKS replica CLI must run on the main thread for bounded cleanup")
        self.previous = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGALRM),
                         signal.getitimer(signal.ITIMER_REAL), time.monotonic())
        signal.signal(signal.SIGTERM, self.terminated)
        signal.signal(signal.SIGALRM, self.expired)
        signal.setitimer(signal.ITIMER_REAL, self.deadline.remaining())

    def close(self):
        if self.previous is None:
            return
        term_handler, alarm_handler, timer, started = self.previous
        self.previous = None
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGTERM, term_handler)
        signal.signal(signal.SIGALRM, alarm_handler)
        if timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(.001, timer[0] - (time.monotonic() - started)), timer[1])


class PodForward:
    """A bounded child process; kubectl logs remain transient and never enter evidence."""
    def __init__(self, control, pod, deployment_uid, deadline):
        self.control, self.pod = control, dict(pod)
        self.deployment_uid, self.deadline = deployment_uid, deadline
        self.process = self.log = self.timer = None
        self.port = None
        self.lock = threading.RLock()
        self.closed = False

    def __enter__(self):
        try:
            verify_pinned_pod(self.control, self.pod, self.deployment_uid)
            self.log = tempfile.TemporaryFile(mode="w+b")
            self.process = subprocess.Popen(["kubectl", "--context", self.control.arn,
                "--namespace", self.control.namespace, "--request-timeout=30s", "port-forward",
                "--address=127.0.0.1", "--pod-running-timeout=30s", "pod/" + safe_name(self.pod["name"]),
                ":8080"], stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
            self.timer = threading.Timer(self.deadline.remaining(), self.close)
            self.timer.daemon = True
            self.timer.start()
            startup_end = time.monotonic() + self.deadline.remaining(35)
            while time.monotonic() < startup_end:
                if self.process.poll() is not None:
                    raise RuntimeError("Scoped API Pod port-forward exited")
                with self.lock:
                    if self.closed:
                        raise TimeoutError("API Pod port-forward lifetime expired")
                    # Parent reads must not move the child writer's shared file
                    # offset. pread preserves kubectl's stdout position.
                    output = os.pread(self.log.fileno(), 8192, 0)
                match = re.search(rb"Forwarding from 127\.0\.0\.1:([0-9]+) -> 8080", output)
                if match:
                    self.port = int(match.group(1))
                    if not 1 <= self.port <= 65535:
                        raise ValueError("Unexpected loopback port-forward listener")
                    with socket.create_connection(("127.0.0.1", self.port), timeout=self.deadline.remaining(1)):
                        pass
                    verify_pinned_pod(self.control, self.pod, self.deployment_uid)
                    return self
                time.sleep(min(.1, self.deadline.remaining()))
            raise TimeoutError("Scoped API Pod port-forward did not start")
        except BaseException:
            self.close()
            raise

    @property
    def base(self):
        if self.port is None or self.closed:
            raise ValueError("API Pod port-forward is not active")
        return f"http://127.0.0.1:{self.port}"

    def evidence(self):
        return {"podName": self.pod["name"], "podUid": self.pod["uid"],
                "listener": "127.0.0.1", "localPort": self.port, "targetPort": 8080,
                "routing": "direct pod/name; no Service or ALB load balancing",
                "lifetimeBoundedByHarnessDeadline": True}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.timer:
                self.timer.cancel()
            try:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
            finally:
                if self.log:
                    self.log.close()

    def __exit__(self, *_):
        self.close()


class PodAPI:
    def __init__(self, forward, deadline):
        import requests
        self.forward, self.deadline = forward, deadline
        self.session = requests.Session()
        self.session.trust_env = False  # A caller's HTTP proxy must never receive fixture tokens.

    def request(self, method, path, token):
        if not re.fullmatch(r"/api/(?:auth/me|teams/[1-9][0-9]*(?:/socket-ticket|/images)?)", path):
            raise ValueError("Unsupported replica acceptance API route")
        verify_pinned_pod(self.forward.control, self.forward.pod, self.forward.deployment_uid)
        response = self.session.request(method, self.forward.base + path,
            headers={"Authorization": "Bearer " + token}, timeout=self.deadline.remaining(15),
            allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected HTTP status {response.status_code}")
        return response.json()

    def close(self):
        self.session.close()


def profile(api, token):
    data = api.request("GET", "/api/auth/me", token)
    if not isinstance(data, dict) or data.get("role") != "USER" or not isinstance(data.get("name"), str):
        raise ValueError("Fixture must be an actual USER account with a profile")
    positive_id(data.get("id"))
    return data


def validate_membership(data, team_id, user_ids):
    if not isinstance(data, dict) or data.get("id") != team_id or not isinstance(data.get("members"), list):
        raise ValueError("Unexpected team membership response")
    ids = [positive_id(member.get("id")) for member in data["members"] if isinstance(member, dict)]
    if (len(ids) != len(data["members"]) or len(set(ids)) != len(ids) or
            type(data.get("memberCount")) is not int or data["memberCount"] != len(ids) or
            not set(user_ids).issubset(ids)):
        raise ValueError("Both fixture USER accounts must be actual distinct team members")
    return sorted(ids)


def socket_ticket(api, token, team_id, user_id):
    before = time.time()
    data = api.request("POST", f"/api/teams/{team_id}/socket-ticket", token)
    after = time.time()
    if not isinstance(data, dict) or not isinstance(data.get("ticket"), str):
        raise ValueError("Socket ticket response missing an actual ticket")
    try:
        pieces = data["ticket"].split(".")
        if len(pieces) != 3:
            raise ValueError("Malformed socket ticket")
        claims = json.loads(base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4)))
        expires_at = dt.datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            raise ValueError("Ticket expiration must have a timezone")
        issued, expires = claims.get("iat"), claims.get("exp")
        if (type(issued) is not int or type(expires) is not int or expires - issued != 60 or
                not before - 5 <= issued <= after + 5 or expires <= after or
                expires_at.timestamp() != expires or claims.get("purpose") != "team-socket" or
                claims.get("teamId") != team_id or claims.get("userId") != user_id):
            raise ValueError("Ticket claims do not match the fresh 60-second team/user contract")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Invalid fresh team socket ticket contract") from exc
    # Decoding checks only shape/lifetime. An observed authorized NOTE from the
    # target Pod is required before the harness considers its signature accepted.
    return data["ticket"], {"issuedAtEpoch": issued, "expiresAtEpoch": expires,
                            "lifetimeSeconds": 60, "signatureEvidence": "authorized target-Pod NOTE required"}


def open_socket(target, ticket, team_id, origin, deadline):
    import websocket
    verify_pinned_pod(target.forward.control, target.forward.pod, target.forward.deployment_uid)
    target_url = target.forward.base.replace("http://", "ws://", 1)
    ws = websocket.create_connection(target_url + f"/ws/teams/{team_id}?ticket=" + quote(ticket, safe=""),
        timeout=deadline.remaining(15), origin=origin, http_proxy_host=None, http_proxy_port=None,
        http_no_proxy=["127.0.0.1", "localhost"], redirect_limit=0)
    if ws.status != 101:
        with contextlib.suppress(Exception):
            ws.shutdown()
        raise RuntimeError("Direct Pod socket handshake must upgrade without redirects")
    return ws


def expect_note(ws, marker, actor_name, team_id, deadline, max_wait=15):
    import websocket
    ends = time.monotonic() + deadline.remaining(max_wait)
    ignored = 0
    while time.monotonic() < ends:
        ws.settimeout(min(1, deadline.remaining(), max(.01, ends - time.monotonic())))
        try:
            message = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        if not message:
            raise RuntimeError("Socket closed before an authorized matching NOTE")
        event = json.loads(message)
        if not isinstance(event, dict):
            raise ValueError("Unexpected team socket event")
        if event.get("message") == actor_name + ": " + marker:
            if (event.get("type") != "NOTE" or event.get("teamId") != team_id or
                    event.get("actorName") != actor_name or not event.get("occurredAt")):
                raise ValueError("Matching team NOTE has an incorrect event identity")
            return {"eventType": "NOTE", "teamId": team_id, "marker": marker,
                    "receivedAtEpoch": time.time(), "ignoredOtherEvents": ignored}
        ignored += 1
        if ignored > 256:
            raise RuntimeError("Team fixture is too busy for bounded relay acceptance")
    raise TimeoutError("Authorized NOTE did not reach the selected API Pod")


def relay(sender, receiver, actor, team_id, deadline):
    marker = "eks-replica-" + uuid.uuid4().hex
    sender.send(marker)  # Exactly once. Retry would hide dropped events and create duplicate notes.
    remote = expect_note(receiver, marker, actor["name"], team_id, deadline)
    local = expect_note(sender, marker, actor["name"], team_id, deadline)
    return {"sentAtMostOnce": True, "senderUserId": actor["id"], "remote": remote, "localEcho": local}


def delete_pinned_api_pod(control, pod, initial, deadline):
    if os.getenv("ALLOW_EKS_FAILURE_INJECTION") != "1":
        raise ValueError("API restart requires ALLOW_EKS_FAILURE_INJECTION=1")
    deadline.remaining()
    namespace = control.json("get", "namespace", control.namespace, "-o", "json")
    labels = namespace["metadata"].get("labels", {})
    if (namespace["metadata"].get("uid") != control.namespace_uid or
            namespace["metadata"].get("deletionTimestamp") or
            labels.get("photoplatform.io/environment") != "dev" or
            labels.get("photoplatform.io/disposable") != "true" or
            labels.get("app.kubernetes.io/part-of") != "photoplatform"):
        raise ValueError("Pinned disposable dev namespace was replaced or changed")
    current = verify_pinned_pod(control, pod, initial["uid"])
    ready = [p for p in current["pods"] if p["ready"] and not p["terminating"]]
    if current["replicas"] != initial["replicas"] or len(ready) != current["replicas"] or len(ready) < 2:
        raise ValueError("API fault requires unchanged replica count and a surviving Ready replica")
    safe_name(pod["name"])
    uuid.UUID(pod["uid"])
    body = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": pod["uid"]},
            "propagationPolicy": "Background"}
    # Guards can consume their bounded network time. Never dispatch a late fault
    # after the accepted run window, even if all identity checks succeeded.
    deadline.remaining()
    kubectl(control.arn, control.namespace, "delete", "--raw",
        f"/api/v1/namespaces/{control.namespace}/pods/{pod['name']}", "-f", "-", body=body)
    return {"podName": pod["name"], "podUid": pod["uid"], "deleteOptions": body,
            "semantics": "Single API Pod default-grace deletion; not SIGKILL, rollout or node drain"}


def wait_replacement(control, initial, deleted, survivor, deadline):
    original_uids = {p["uid"] for p in initial["pods"]}
    while True:
        deadline.remaining()
        state = control.state("api")
        if state["uid"] != initial["uid"] or state["replicas"] != initial["replicas"]:
            raise ValueError("Deployment identity or replica count changed during the selected API fault")
        if not any(p["uid"] == survivor["uid"] and p["name"] == survivor["name"]
                   and p["ready"] and not p["terminating"] for p in state["pods"]):
            raise ValueError("The selected surviving API replica changed or became unready")
        active = [p for p in state["pods"] if p["ready"] and not p["terminating"] and p["phase"] == "Running"]
        replacements = [p for p in active if p["uid"] not in original_uids]
        if len(replacements) > 1:
            raise ValueError("More than the single selected API Pod changed during the fault")
        if (len(replacements) == 1 and not any(p["uid"] == deleted["uid"] for p in state["pods"])
                and len(active) == state["replicas"] and state["readyReplicas"] == state["replicas"]
                and state["updatedReplicas"] == state["replicas"]
                and state["observedGeneration"] >= state["generation"]):
            return replacements[0], state
        time.sleep(deadline.remaining(2))


def observed_disconnect(ws, deadline):
    import websocket
    ends = time.monotonic() + deadline.remaining(45)
    while time.monotonic() < ends:
        ws.settimeout(min(1, deadline.remaining(), max(.01, ends - time.monotonic())))
        try:
            opcode, data = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                code = int.from_bytes(data[:2], "big") if len(data) >= 2 else None
                return {"observation": "close_frame", "closeCode": code}
        except websocket.WebSocketTimeoutException:
            continue
        except (websocket.WebSocketConnectionClosedException, OSError):
            return {"observation": "port_forward_transport_closed", "closeCode": None}
    raise TimeoutError("Selected API socket did not disconnect after Pod deletion")


class ReplicaSuite:
    def __init__(self, control, initial, pods, apis, stack, deadline, origin, report, output):
        self.control, self.initial, self.pods, self.apis = control, initial, pods, apis
        self.stack, self.deadline, self.origin = stack, deadline, origin
        self.report, self.output = report, output
        self.accounts, self.team_id, self.tokens = None, None, None

    def case(self, name, function=None, reason=None):
        started = time.monotonic()
        row = {"name": name, "status": "NOT_RUN"}
        self.report["cases"].append(row)
        if reason:
            row["reason"] = reason
        else:
            try:
                self.deadline.remaining()
                row["evidence"] = function()
                row["status"] = "PASS"
            except (HarnessTerminated, HarnessDeadlineExpired) as exc:
                row.update(status="FAIL", errorType=type(exc).__name__, durationSeconds=time.monotonic() - started)
                raise  # Main must unwind ExitStack, not continue to another case.
            except Exception as exc:
                row.update(status="FAIL", errorType=type(exc).__name__)
        row["durationSeconds"] = time.monotonic() - started
        write_report(self.output, self.report)
        return row["status"] == "PASS"

    def jwt(self):
        token = os.getenv("TEAM_A_TOKEN") or required("TEST_OWNER_TOKEN")
        identities = [profile(api, token) for api in self.apis]
        if identities[0] != identities[1]:
            raise ValueError("The same JWT did not return the same account on both selected API Pods")
        return {"userId": identities[0]["id"], "role": identities[0]["role"],
                "podUids": [p["uid"] for p in self.pods], "authorizedRoute": "/api/auth/me"}

    def public_distribution(self):
        import requests
        token = os.getenv("TEAM_A_TOKEN") or required("TEST_OWNER_TOKEN")
        expected_identity = profile(self.apis[0], token)
        origin = secure_origin(required("API_URL"))
        eligible = {p["uid"] for p in self.initial["pods"] if p["ready"] and not p["terminating"]}
        samples = self.report["publicAlbSamples"] = []
        seen = set()
        for attempt in range(1, 31):
            sample = {"attempt": attempt, "statusCode": None, "podUid": None, "revisionMatches": False,
                      "identityMatches": False, "podUidInGuardedRelease": False}
            samples.append(sample)
            try:
                # New session/connection per request; no sticky-session cookie or
                # ambient HTTP proxy can hide actual ALB target distribution.
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(origin + "/api/auth/me", headers={"Authorization": "Bearer " + token,
                        "Connection": "close"}, allow_redirects=False, timeout=self.deadline.remaining(15))
                    sample["statusCode"] = response.status_code
                    raw_uid = response.headers.get("X-Photoplatform-Pod-Uid", "")
                    try:
                        sample["podUid"] = str(uuid.UUID(raw_uid))
                    except (ValueError, AttributeError):
                        pass
                    sample["revisionMatches"] = response.headers.get("X-Photoplatform-Revision") == self.control.sha
                    sample["podUidInGuardedRelease"] = sample["podUid"] in eligible
                    if response.status_code == 200:
                        sample["identityMatches"] = response.json() == expected_identity
                    if all(sample[key] for key in ("revisionMatches", "identityMatches", "podUidInGuardedRelease")):
                        seen.add(sample["podUid"])
            except Exception as exc:
                sample["errorType"] = type(exc).__name__
            write_report(self.output, self.report)
        if (len(seen) < 2 or any(sample["statusCode"] != 200 or not all(sample[key] for key in
                ("revisionMatches", "identityMatches", "podUidInGuardedRelease")) for sample in samples)):
            raise AssertionError("All public ALB samples must authorize the same JWT and identify at least two guarded API Pod UIDs")
        current = self.control.state("api")
        if current["uid"] != self.initial["uid"] or not seen.issubset(
                {p["uid"] for p in current["pods"] if p["ready"] and not p["terminating"]}):
            raise ValueError("Observed public ALB targets changed before distribution confirmation")
        return {"attempted": len(samples), "successful": len(samples), "distinctPodUids": sorted(seen),
                "requestHeader": "Connection: close", "cookieReuse": False, "authorizedRoute": "/api/auth/me",
                "instanceHeaders": ["X-Photoplatform-Pod-Uid", "X-Photoplatform-Revision"],
                "requiresDevOnlyInstanceIdentity": True}

    def fixture(self):
        self.tokens = [required("TEAM_A_TOKEN"), required("TEAM_B_TOKEN")]
        raw_id = required("TEAM_ID")
        if not re.fullmatch(r"[1-9][0-9]*", raw_id):
            raise ValueError("TEAM_ID must identify an existing positive-integer team")
        self.team_id = int(raw_id)
        accounts = [[profile(api, token) for token in self.tokens] for api in self.apis]
        if accounts[0] != accounts[1] or accounts[0][0]["id"] == accounts[0][1]["id"]:
            raise ValueError("Team fixture must identify the same two distinct USER accounts on both API Pods")
        self.accounts = accounts[0]
        user_ids = [account["id"] for account in self.accounts]
        member_sets = [validate_membership(api.request("GET", f"/api/teams/{self.team_id}", token),
                                          self.team_id, user_ids) for api in self.apis for token in self.tokens]
        if any(members != member_sets[0] for members in member_sets):
            raise ValueError("Team membership changed or differs across the selected accounts/Pods")
        return {"teamId": self.team_id, "userIds": user_ids, "membershipVerifiedOnPodUids": [p["uid"] for p in self.pods],
                "membershipRoute": f"/api/teams/{self.team_id}", "administratorBypass": False}

    @contextlib.contextmanager
    def connected(self, user, issuer, target):
        ticket, metadata = socket_ticket(issuer, self.tokens[user], self.team_id, self.accounts[user]["id"])
        ws = open_socket(target, ticket, self.team_id, self.origin, self.deadline)
        try:
            yield ws, metadata
        finally:
            try:
                with contextlib.suppress(Exception):
                    ws.close(timeout=min(2, max(.1, self.deadline.ends - time.monotonic())))
            finally:
                # A received Close marks websocket-client disconnected before
                # close(), which can otherwise leave its descriptor open.
                with contextlib.suppress(Exception):
                    ws.shutdown()

    def cross_ticket(self, issuer_index):
        issuer, target = self.apis[issuer_index], self.apis[1 - issuer_index]
        with self.connected(issuer_index, issuer, target) as (ws, metadata):
            marker = "eks-ticket-" + uuid.uuid4().hex
            ws.send(marker)
            note = expect_note(ws, marker, self.accounts[issuer_index]["name"], self.team_id, self.deadline)
        return {"issuerPodUid": self.pods[issuer_index]["uid"], "targetPodUid": self.pods[1 - issuer_index]["uid"],
                "ticket": metadata, "authorizedNote": note}

    def bidirectional(self):
        with self.connected(0, self.apis[0], self.apis[0]) as (a, _), \
                self.connected(1, self.apis[1], self.apis[1]) as (b, _):
            directions = [relay(a, b, self.accounts[0], self.team_id, self.deadline),
                          relay(b, a, self.accounts[1], self.team_id, self.deadline)]
        return {"podUids": [p["uid"] for p in self.pods], "teamId": self.team_id, "directions": directions,
                "transport": "Actual team NOTE text messages through the deployed Rabbit fanout relay"}

    def reconnect(self):
        with self.connected(0, self.apis[0], self.apis[0]) as (old, old_ticket):
            marker = "eks-before-reconnect-" + uuid.uuid4().hex
            old.send(marker)
            expect_note(old, marker, self.accounts[0]["name"], self.team_id, self.deadline)
        # JWT tickets issued within the same second can be byte-identical. Obtain
        # the new ticket in a later second, rather than falsely calling it distinct.
        wait = old_ticket["issuedAtEpoch"] + 1.1 - time.time()
        if wait > 0:
            time.sleep(min(wait, self.deadline.remaining()))
        with self.connected(0, self.apis[1], self.apis[1]) as (fresh, metadata):
            if metadata["issuedAtEpoch"] <= old_ticket["issuedAtEpoch"]:
                raise ValueError("Reconnect did not obtain a freshly issued socket ticket")
            marker = "eks-reconnected-" + uuid.uuid4().hex
            fresh.send(marker)
            note = expect_note(fresh, marker, self.accounts[0]["name"], self.team_id, self.deadline)
            images = self.apis[1].request("GET", f"/api/teams/{self.team_id}/images", self.tokens[0])
            if not isinstance(images, list):
                raise ValueError("Reconnect committed team-library REST refresh failed")
        return {"previousPodUid": self.pods[0]["uid"], "reconnectedPodUid": self.pods[1]["uid"],
                "freshTicket": metadata, "authorizedNote": note, "restRefreshStatus": 200,
                "semantics": "Explicit client disconnect/reconnect; no server failure in this case"}

    def restart(self):
        with self.connected(0, self.apis[0], self.apis[0]) as (old, _), \
                self.connected(1, self.apis[1], self.apis[1]) as (surviving_socket, _):
            relay(old, surviving_socket, self.accounts[0], self.team_id, self.deadline)
            self.report["plannedApiPodFault"] = {"podName": self.pods[0]["name"], "podUid": self.pods[0]["uid"],
                                                  "deploymentUid": self.initial["uid"]}
            write_report(self.output, self.report)
            fault = delete_pinned_api_pod(self.control, self.pods[0], self.initial, self.deadline)
            self.report["issuedApiPodFault"] = fault
            write_report(self.output, self.report)
            disconnected = observed_disconnect(old, self.deadline)
            replacement, state = wait_replacement(self.control, self.initial, self.pods[0], self.pods[1], self.deadline)
            forward = self.stack.enter_context(PodForward(self.control, replacement, self.initial["uid"], self.deadline))
            self.report["routing"].append(forward.evidence())
            replacement_api = PodAPI(forward, self.deadline)
            self.stack.callback(replacement_api.close)
            for api in (self.apis[1], replacement_api):
                if profile(api, self.tokens[0]) != self.accounts[0]:
                    raise ValueError("Original JWT failed on surviving/replacement API Pod")
            validate_membership(replacement_api.request("GET", f"/api/teams/{self.team_id}", self.tokens[0]),
                                self.team_id, [account["id"] for account in self.accounts])
            with self.connected(0, self.apis[1], replacement_api) as (fresh, metadata):
                delivered = relay(fresh, surviving_socket, self.accounts[0], self.team_id, self.deadline)
                images = replacement_api.request("GET", f"/api/teams/{self.team_id}/images", self.tokens[0])
                if not isinstance(images, list):
                    raise ValueError("Replacement team-library REST refresh failed")
        return {"fault": fault, "oldSocketDisconnect": disconnected, "replacementPod": replacement,
                "survivingPodUid": self.pods[1]["uid"], "recoveredDeployment": state, "freshTicket": metadata,
                "reconnectedRelay": delivered, "restRefreshStatus": 200}


def matrix_results(cases):
    statuses = {case["name"]: case["status"] for case in cases}
    groups = {"multi_api_jwt_and_ticket": (*REQUIRED_CASES[:4], PUBLIC_CASE),
              "cross_pod_websocket_and_reconnect": (REQUIRED_CASES[4], REQUIRED_CASES[5], RESTART_CASE)}
    rows = []
    for name in MATRIX:
        group = groups.get(name)
        status = "NOT_RUN"
        if group:
            values = [statuses.get(case, "NOT_RUN") for case in group]
            status = "FAIL" if "FAIL" in values else "PASS" if all(v == "PASS" for v in values) else "INCOMPLETE"
        rows.append({"scenario": name, "status": status,
                     "evidenceScope": ("Direct verified EKS Pod connections and public ALB JWT samples; browser reconnect unmeasured"
                                       if name == "multi_api_jwt_and_ticket" else "Direct verified EKS Pod connections; ingress/browser reconnect unmeasured") if group
                     else "Requires separate retained scenario evidence"})
    return rows


def selected_status(cases, restart_requested=False):
    required_cases = (*REQUIRED_CASES, PUBLIC_CASE, *((RESTART_CASE,) if restart_requested else ()))
    statuses = {case["name"]: case["status"] for case in cases}
    if any(statuses.get(name) == "FAIL" for name in required_cases):
        return "FAIL"
    return "PASS" if all(statuses.get(name) == "PASS" for name in required_cases) else "INCOMPLETE"


def verify_final_api_state(state, initial, cases, restart_requested):
    active = [p for p in state["pods"] if p["ready"] and not p["terminating"] and p["phase"] == "Running"]
    if (state["uid"] != initial["uid"] or state["replicas"] != initial["replicas"]
            or state["readyReplicas"] != state["replicas"] or state["updatedReplicas"] != state["replicas"]
            or len(active) != state["replicas"] or state["observedGeneration"] < state["generation"]):
        raise ValueError("Final API Deployment is replaced, unsettled or differs from the initial replica count")
    expected_uids = {p["uid"] for p in initial["pods"] if p["ready"] and not p["terminating"]}
    if restart_requested:
        restart = next((case for case in cases if case["name"] == RESTART_CASE and case["status"] == "PASS"), None)
        if restart is None:
            raise ValueError("A requested API fault needs retained successful replacement evidence")
        expected_uids.remove(restart["evidence"]["fault"]["podUid"])
        expected_uids.add(restart["evidence"]["replacementPod"]["uid"])
    if {p["uid"] for p in active} != expected_uids:
        raise ValueError("API Pod UIDs changed beyond the explicitly verified single-Pod fault")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="benchmarks/results/eks-replica-acceptance.json")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--restart-pod-a", action="store_true", help="Opt in to deleting exactly the selected API Pod UID")
    args = parser.parse_args()
    if not 30 <= args.timeout <= 1800:
        parser.error("timeout must be between 30 and 1800 seconds")
    report = {"kind": "real-aws-eks-multi-api-acceptance", "status": "FAIL", "matrixStatus": "INCOMPLETE",
              "cases": [], "routing": [], "restartRequested": args.restart_pod_a,
              "limitations": ["WebSocket loopback direct-Pod routing does not measure ALB, ingress or browser reconnect",
                              "A successful selected suite does not certify the full P6 matrix",
                              "Live team notifications remain best effort; no durable event replay guarantee"]}
    deadline = Deadline(args.timeout)
    process_window = ProcessWindow(deadline)
    try:
        process_window.start()
        if args.restart_pod_a and os.getenv("ALLOW_EKS_FAILURE_INJECTION") != "1":
            raise ValueError("Set ALLOW_EKS_FAILURE_INJECTION=1 before requesting the API Pod restart")
        _, control, report["provenance"] = eks_guard()
        initial, pods = select_api_pods(control)
        report["initialApiDeployment"] = initial
        # Origin validation permits only the configured cloud frontend HTTPS
        # origin; tokens/tickets are never sent through external proxy settings.
        origin = secure_origin(required("CLOUD_FRONTEND_ORIGIN"))
        if urlparse(origin).path not in ("", "/"):
            raise ValueError("WebSocket Origin must contain only scheme, host and optional port")
        with contextlib.ExitStack() as stack:
            apis = []
            for pod in pods:
                forward = stack.enter_context(PodForward(control, pod, initial["uid"], deadline))
                report["routing"].append(forward.evidence())
                api = PodAPI(forward, deadline)
                stack.callback(api.close)
                apis.append(api)
            write_report(args.output, report)
            suite = ReplicaSuite(control, initial, pods, apis, stack, deadline, origin, report, args.output)
            jwt_token = os.getenv("TEAM_A_TOKEN") or os.getenv("TEST_OWNER_TOKEN")
            suite.case(REQUIRED_CASES[0], suite.jwt, reason=None if jwt_token else "No USER JWT fixture configured")
            suite.case(PUBLIC_CASE, suite.public_distribution, reason=None if jwt_token else "No USER JWT fixture configured")
            missing = [key for key in ("TEAM_A_TOKEN", "TEAM_B_TOKEN", "TEAM_ID") if not os.getenv(key, "").strip()]
            fixture_ok = suite.case(REQUIRED_CASES[1], suite.fixture,
                                    reason="Team fixture missing: " + ", ".join(missing) if missing else None)
            dependency = None if fixture_ok else "Validated distinct USER team-member fixture unavailable"
            suite.case(REQUIRED_CASES[2], lambda: suite.cross_ticket(0), reason=dependency)
            suite.case(REQUIRED_CASES[3], lambda: suite.cross_ticket(1), reason=dependency)
            suite.case(REQUIRED_CASES[4], suite.bidirectional, reason=dependency)
            suite.case(REQUIRED_CASES[5], suite.reconnect, reason=dependency)
            suite.case(RESTART_CASE, suite.restart,
                reason=dependency if args.restart_pod_a else "Pod restart not selected; requires --restart-pod-a and explicit fault switch")
            deadline.remaining()
            report["finalApiDeployment"] = control.state("api")
            if selected_status(report["cases"], args.restart_pod_a) != "FAIL":
                verify_final_api_state(report["finalApiDeployment"], initial, report["cases"],
                    args.restart_pod_a and any(case["name"] == RESTART_CASE and case["status"] == "PASS" for case in report["cases"]))
        report["portForwardCleanup"] = "COMPLETE"
        report["status"] = selected_status(report["cases"], args.restart_pod_a)
    except (Exception, HarnessTerminated, HarnessDeadlineExpired) as exc:
        report["fatalErrorType"] = type(exc).__name__  # SDK/HTTP exception messages can contain tokens or URLs.
        report["status"] = "FAIL"
    finally:
        process_window.close()
        report["matrix"] = matrix_results(report["cases"])
        report["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_report(args.output, report)
    print(f"EKS replica acceptance: {report['status']}; full P6 matrix: INCOMPLETE; report: {args.output}")
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
