from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from scripts import eks_scenarios as scenarios


ENV = {"EXPECTED_AWS_ACCOUNT_ID": "012345678901", "AWS_REGION": "us-east-1", "S3_BUCKET": "dev-bucket",
       "STORAGE_PREFIX": "authorized", "EKS_IAM_ALLOWED_S3_KEY": "authorized/known-sentinel",
       "EKS_IAM_DENIED_S3_KEY": "unrelated/known-sentinel", "EKS_EXPECTED_MEDIA_ROLE_ARN": "arn:aws:iam::012345678901:role/media",
       "EKS_IAM_ADMIN_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:012345678901:secret:migrator-abcd"}


class IAMScenariosTests(unittest.TestCase):
    def aws(self):
        clients = {"s3": Mock(), "secretsmanager": Mock()}
        clients["secretsmanager"].describe_secret.return_value = {"ARN": ENV["EKS_IAM_ADMIN_SECRET_ARN"]}
        aws = Mock()
        aws.client.side_effect = clients.__getitem__
        return aws, clients

    def test_missing_or_nonexistent_sentinel_never_dispatches_runtime_probe(self):
        aws, clients = self.aws()
        clients["s3"].head_object.side_effect = RuntimeError("not found")
        with patch.dict(os.environ, ENV), patch.object(scenarios, "worker_python") as remote:
            with self.assertRaises(RuntimeError):
                scenarios.iam_probe(aws, Mock(), "worker", "uid")
        remote.assert_not_called()
        clients["secretsmanager"].get_secret_value.assert_not_called()

    def test_foreign_secret_and_ambiguous_prefix_are_rejected_before_aws(self):
        aws, _ = self.aws()
        for overrides in ({"EKS_IAM_ADMIN_SECRET_ARN": ENV["EKS_IAM_ADMIN_SECRET_ARN"].replace("012345678901", "111111111111")},
                          {"EKS_IAM_DENIED_S3_KEY": "authorized/other"}, {"STORAGE_PREFIX": "/"}):
            with self.subTest(overrides=overrides), patch.dict(os.environ, ENV | overrides):
                with self.assertRaises(ValueError):
                    scenarios.iam_probe(aws, Mock(), "worker", "uid")
        aws.client.assert_not_called()

    def test_known_denials_require_positive_identity_and_keep_failed_evidence(self):
        aws, clients = self.aws()
        result = {"podUid": "uid", "credentialMethod": "container-role", "allowedPrefixHead": "PASS",
            "unrelatedS3Prefix": {"status": "PASS", "awsErrorCode": "AccessDenied"},
            "administratorSecret": {"status": "FAIL", "reason": "Unexpected authorized response; response content discarded"}}
        with patch.dict(os.environ, ENV), patch.object(scenarios, "worker_python", return_value=json.dumps(result)) as remote:
            evidence = scenarios.iam_probe(aws, Mock(), "worker", "uid")
        self.assertEqual(evidence["status"], "FAIL")
        self.assertEqual(evidence["runtime"]["administratorSecret"]["status"], "FAIL")
        self.assertEqual(clients["s3"].head_object.call_count, 2)
        clients["secretsmanager"].get_secret_value.assert_not_called()
        code = remote.call_args.args[3]
        self.assertIn("credentials.method != 'container-role'", code)
        self.assertIn("response['Body'].close()", code)
        self.assertNotIn(ENV["EKS_IAM_ALLOWED_S3_KEY"], json.dumps(evidence))

    def test_s3_wrong_error_is_not_an_iam_denial(self):
        class ClientError(Exception):
            def __init__(self, code):
                self.response = {"Error": {"Code": code}}
        s3, secret, sts = Mock(), Mock(), Mock()
        s3.get_object.side_effect = ClientError("NoSuchKey")
        secret.get_secret_value.side_effect = ClientError("AccessDeniedException")
        sts.get_caller_identity.return_value = {"Account": "012345678901", "Arn": "arn:aws:sts:012345678901:assumed-role/media/session"}
        session = Mock()
        session.get_credentials.return_value = SimpleNamespace(method="container-role")
        session.client.side_effect = lambda name, **kwargs: {"s3": s3, "secretsmanager": secret, "sts": sts}[name]
        payload = {"region": "us-east-1", "account": "012345678901", "roleName": "media", "bucket": "dev",
                   "allowedKey": "authorized/known", "deniedKey": "unrelated/known", "adminSecret": "secret-arn"}
        modules = {"boto3": Mock(Session=Mock(return_value=session)), "botocore": Mock(),
                   "botocore.config": Mock(Config=Mock()), "botocore.exceptions": Mock(ClientError=ClientError)}
        output = io.StringIO()
        with patch.dict("sys.modules", modules), redirect_stdout(output):
            exec(scenarios.IAM_PROBE, {"json": json, "os": SimpleNamespace(environ={"POD_UID": "uid"}),
                                     "sys": SimpleNamespace(argv=["probe", "uid", json.dumps(payload)])})
        result = json.loads(output.getvalue())
        self.assertEqual(result["unrelatedS3Prefix"], {"status": "FAIL", "awsErrorCode": "NoSuchKey"})
        self.assertEqual(result["administratorSecret"]["status"], "PASS")
        s3.head_object.assert_called_once()


class NetworkScenariosTests(unittest.TestCase):
    def test_ipv4_ipv6_and_all_protocol_internet_grants_detected(self):
        for rule in ({"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                     {"IpProtocol": "6", "FromPort": 1, "ToPort": 65535, "Ipv6Ranges": [{"CidrIpv6": "::/0"}]},
                     {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}):
            with self.subTest(rule=rule):
                self.assertEqual(len(scenarios.open_internet_grants({"GroupId": "sg-dev", "IpPermissions": [rule]}, 5432)), 1)

    def test_unrelated_ports_and_private_prefixes_do_not_false_fail(self):
        group = {"GroupId": "sg-dev", "IpPermissions": [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}]}
        self.assertEqual(scenarios.open_internet_grants(group, 5432), [])

    def test_public_database_fails_before_http_probes(self):
        rds = {"DBInstanceArn": "arn:aws:rds:us-east-1:012345678901:db:dev", "PubliclyAccessible": True}
        mq = {"BrokerArn": "arn:aws:mq:us-east-1:012345678901:broker:dev:broker-id", "BrokerId": "broker-id", "PubliclyAccessible": False}
        aws = Mock()
        aws.client.side_effect = lambda name: Mock(**({"describe_db_instances.return_value": {"DBInstances": [rds]}} if name == "rds" else {"describe_broker.return_value": mq}))
        with patch.dict(os.environ, ENV | {"EKS_RDS_INSTANCE_ID": "dev", "EKS_MQ_BROKER_ID": "broker-id"}), \
                patch.object(scenarios, "CloudAPI") as api:
            with self.assertRaises(AssertionError):
                scenarios.network_probe(aws, Mock())
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
