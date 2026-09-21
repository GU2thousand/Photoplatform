# Network module

The module creates three public, three private application, and three isolated data subnets. Public subnets hold the internet-facing ALB and NAT gateways. Application subnets reach HTTPS services through NAT; an S3 gateway endpoint avoids NAT for same-region S3 traffic. Data subnets have no internet or NAT default route. RDS and Amazon MQ accept traffic only from the explicit application security groups.

`single_nat_gateway=true` lowers dev cost but creates an AZ dependency and may incur cross-AZ data transfer charges. Set it to `false` for one gateway per AZ. NAT, ALB, RDS, and MQ all incur cost while idle; use the environment destroy procedure after cloud testing.

The public ALB accepts443 and80; the ALB module must redirect80 to HTTPS. Only the ALB can reach API8080. The API can reach encoder8090. API and worker services can reach database5432 and broker5671. The collector can reach broker management443. Telemetry sidecars use localhost within each ECS task and do not need cross-service ingress. Security group rules are stateful, so return traffic does not require additional rules. VPC DNS resolver traffic is not filtered by security groups.

VPC flow logs record accepted and rejected traffic in CloudWatch with a30-day default retention. The unused default VPC security group has all rules removed. This module does not create public IPs for application tasks, internet routes in the data tier, or ingress rules for a database/broker from CIDR0.0.0.0/0.
