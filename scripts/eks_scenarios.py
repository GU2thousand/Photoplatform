"""Scoped EKS IAM and network negative probes using the protected validation role.

These read-only probes require known resource targets and a verified release. A
PASS covers only the observations listed; it does not complete the P6 matrix.
Other executable scenarios live in eks_failure.py, eks_dependency_faults.py,
eks_release_scenarios.py and eks_replica_acceptance.py.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, required, write_report
from scripts.eks_common import eks_guard, worker_python


IAM_PROBE = r'''
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
p=json.loads(sys.argv[2])
cfg=Config(connect_timeout=3,read_timeout=5,retries={'max_attempts':0})
session=boto3.Session(region_name=p['region'])
credentials=session.get_credentials()
if credentials is None or credentials.method != 'container-role':
    raise SystemExit(43)
caller=session.client('sts',config=cfg).get_caller_identity()
expected='arn:aws:sts:'+p['account']+':assumed-role/'+p['roleName']+'/'
if caller.get('Account') != p['account'] or not caller.get('Arn','').startswith(expected):
    raise SystemExit(44)
result={'callerArn':caller['Arn'],'credentialMethod':credentials.method,'podUid':os.environ['POD_UID']}
s3=session.client('s3',config=cfg)
s3.head_object(Bucket=p['bucket'],Key=p['allowedKey'],ExpectedBucketOwner=p['account'])
result['allowedPrefixHead']='PASS'
def denied(name,operation,expected_codes):
    try:
        response=operation()
        if hasattr(response.get('Body'),'close'): response['Body'].close()
        result[name]={'status':'FAIL','reason':'Unexpected authorized response; response content discarded'}
    except ClientError as error:
        code=error.response.get('Error',{}).get('Code','unknown')
        result[name]={'status':'PASS' if code in expected_codes else 'FAIL','awsErrorCode':code}
denied('unrelatedS3Prefix',lambda:s3.get_object(Bucket=p['bucket'],Key=p['deniedKey'],ExpectedBucketOwner=p['account']),{'AccessDenied'})
denied('administratorSecret',lambda:session.client('secretsmanager',config=cfg).get_secret_value(SecretId=p['adminSecret']),{'AccessDeniedException'})
print(json.dumps(result))
'''


def iam_probe(aws, control, pod_name, pod_uid):
    account, region, bucket = required("EXPECTED_AWS_ACCOUNT_ID"), required("AWS_REGION"), required("S3_BUCKET")
    role = required("EKS_EXPECTED_MEDIA_ROLE_ARN")
    if not re.fullmatch(f"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9+=,.@_/-]+", role):
        raise ValueError("Expected runtime IAM role must belong to the guarded AWS account")
    allowed, denied, raw_prefix = required("EKS_IAM_ALLOWED_S3_KEY"), required("EKS_IAM_DENIED_S3_KEY"), required("STORAGE_PREFIX").strip("/")
    prefix = raw_prefix + "/"
    if not raw_prefix or allowed == denied or not allowed.startswith(prefix) or denied.startswith(prefix):
        raise ValueError("IAM sentinels must be distinct existing keys inside/outside the selected nonempty storage prefix")
    secret = required("EKS_IAM_ADMIN_SECRET_ARN")
    if not secret.startswith(f"arn:aws:secretsmanager:{region}:{account}:secret:"):
        raise ValueError("Administrator Secret must be in the guarded AWS account/region")
    # The operator verifies existence without reading the object/secret contents.
    s3 = aws.client("s3")
    for key in (allowed, denied):
        s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    metadata = aws.client("secretsmanager").describe_secret(SecretId=secret)
    if metadata.get("ARN") != secret or metadata.get("DeletedDate"):
        raise ValueError("Administrator Secret must exist and must not be scheduled for deletion")
    result = json.loads(worker_python(control, pod_name, pod_uid, IAM_PROBE, {
        "region": region, "account": account, "roleName": role.rsplit("/", 1)[1], "bucket": bucket,
        "allowedKey": allowed, "deniedKey": denied, "adminSecret": secret}))
    expected_caller = f"arn:aws:sts:{account}:assumed-role/{role.rsplit('/', 1)[1]}/"
    passed = (result.get("podUid") == pod_uid and result.get("credentialMethod") == "container-role" and
            result.get("callerArn", "").startswith(expected_caller) and result.get("allowedPrefixHead") == "PASS" and
            all(result.get(name, {}).get("status") == "PASS" for name in ("unrelatedS3Prefix", "administratorSecret")))
    return {"status": "PASS" if passed else "FAIL", "runtime": result, "expectedRoleArn": role, "sentinelKeysSha256": [hashlib.sha256(k.encode()).hexdigest() for k in (allowed, denied)],
            "administratorSecretArn": secret, "scope": "One verified media-worker Pod and existing sentinels; API/migrator/encoder roles require their own probes"}


def open_internet_grants(group, port):
    output = []
    for rule in group.get("IpPermissions", []):
        protocol = str(rule.get("IpProtocol"))
        if protocol != "-1" and (protocol not in {"tcp", "6"} or not rule.get("FromPort", -1) <= port <= rule.get("ToPort", -1)):
            continue
        for row in rule.get("IpRanges", []) + rule.get("Ipv6Ranges", []):
            cidr = row.get("CidrIp", row.get("CidrIpv6"))
            if ipaddress.ip_network(cidr, strict=False).prefixlen == 0:
                output.append({"groupId": group["GroupId"], "port": port, "cidr": cidr})
    return output


def network_probe(aws, control):
    account, region = required("EXPECTED_AWS_ACCOUNT_ID"), required("AWS_REGION")
    rds_id, broker_id = required("EKS_RDS_INSTANCE_ID"), required("EKS_MQ_BROKER_ID")
    rows = aws.client("rds").describe_db_instances(DBInstanceIdentifier=rds_id)["DBInstances"]
    if len(rows) != 1:
        raise ValueError("Expected one known RDS dev instance")
    rds = rows[0]
    broker = aws.client("mq").describe_broker(BrokerId=broker_id)
    if (rds.get("DBInstanceArn") != f"arn:aws:rds:{region}:{account}:db:{rds_id}" or
            not broker.get("BrokerArn", "").startswith(f"arn:aws:mq:{region}:{account}:broker:") or
            broker.get("BrokerId") != broker_id):
        raise ValueError("Managed dependencies must belong to the guarded AWS account/region")
    if rds.get("PubliclyAccessible") is not False or broker.get("PubliclyAccessible") is not False:
        raise AssertionError("RDS and MQ must both be private")
    db_port = rds["Endpoint"]["Port"]
    group_ports = [(s["VpcSecurityGroupId"], db_port) for s in rds["VpcSecurityGroups"]]
    group_ports.extend((group, 5671) for group in broker["SecurityGroups"])
    ec2 = aws.client("ec2")
    groups = ec2.describe_security_groups(GroupIds=sorted({group for group, _ in group_ports}))["SecurityGroups"]
    by_id = {group["GroupId"]: group for group in groups}
    if any(group not in by_id for group, _ in group_ports):
        raise ValueError("Managed dependency security groups could not be inventoried")
    grants = [grant for group, port in group_ports for grant in open_internet_grants(by_id[group], port)]
    if grants:
        raise AssertionError("A managed dependency security group admits the entire internet")
    services = control.json("get", "services", "-l", f"app.kubernetes.io/instance={control.release}", "-o", "json")["items"]
    isolated = []
    for service in services:
        component = service["metadata"].get("labels", {}).get("app.kubernetes.io/component")
        if component not in {"api", "encoder", "media-worker", "embedding-worker"}:
            continue
        if service["spec"].get("type", "ClusterIP") != "ClusterIP" or service["spec"].get("externalIPs"):
            raise AssertionError("Workload Services must remain internal ClusterIP without externalIPs")
        if component == "api" and any(p["port"] != 8080 for p in service["spec"]["ports"]):
            raise AssertionError("API Service must not expose the management port")
        isolated.append({"name": service["metadata"]["name"], "component": component, "type": "ClusterIP"})
    if not any(s["component"] == "api" for s in isolated):
        raise ValueError("Selected API Service could not be inventoried")
    api = CloudAPI()
    denied = []
    for path in ("/actuator/prometheus", "/metrics"):
        for anonymous in (True, False):
            response = api.request("GET", path, anonymous=anonymous)
            if response.status_code not in {401, 403, 404}:
                raise AssertionError("Business hostname unexpectedly exposes a metrics route")
            denied.append({"path": path, "anonymous": anonymous, "statusCode": response.status_code})
    return {"rds": {"arn": rds["DBInstanceArn"], "publiclyAccessible": False, "port": db_port},
        "mq": {"arn": broker["BrokerArn"], "publiclyAccessible": False, "amqpsPort": 5671},
        "securityGroups": sorted(by_id), "openInternetDependencyGrants": grants, "services": isolated,
        "businessHostMetricsDenial": denied,
        "scope": "AWS private-resource inventory, relevant SG rules, internal Service types and actual public business-host HTTP denials",
        "limitation": "An independent external-network TCP probe, Kubernetes API/ALB listeners, endpoint-controller IAM and all NetworkPolicies require separate evidence"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("iam", "network"), required=True)
    parser.add_argument("--pod-name")
    parser.add_argument("--pod-uid")
    parser.add_argument("--output", default="benchmarks/results/eks-scenarios.json")
    args = parser.parse_args()
    if args.scenario == "iam" and (not args.pod_name or not args.pod_uid):
        parser.error("iam requires pod-name and pod-uid")
    report = {"kind": "real-aws-eks-negative-probes", "scenario": args.scenario,
              "status": "FAIL", "matrixStatus": "INCOMPLETE"}
    try:
        aws, control, report["provenance"] = eks_guard()
        write_report(args.output, report)
        report["evidence"] = (iam_probe(aws, control, args.pod_name, args.pod_uid) if args.scenario == "iam" else network_probe(aws, control))
        report["status"] = report["evidence"].get("status", "PASS")
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        write_report(args.output, report)
    print(f"EKS {args.scenario} scoped probes: {report['status']}; full matrix INCOMPLETE; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
