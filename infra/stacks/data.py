"""DataStack: DynamoDB single-table `recruiter` + GSI1/GSI2 (+ PITR in prod)."""
from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as _dynamodb
from constructs import Construct

from stacks.common import EnvSettings


class DataStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings,
                 uploads_bucket, **kw) -> None:
        super().__init__(scope, id, **kw)
        self.table = _dynamodb.Table(
            self, "recruiter",
            table_name=f"recruiter-{settings.env}",
            partition_key={"name": "pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "sk", "type": _dynamodb.AttributeType.STRING},
            billing_mode=_dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=(RemovalPolicy.DESTROY if settings.env != "prod"
                            else RemovalPolicy.RETAIN),
            time_to_live_attribute="expiresAt",     # NFR-21: TTL on retained data
            point_in_time_recovery=False if not settings.pitr else True,
        )
        self.table.add_global_secondary_index(
            index_name="gsi1",
            partition_key={"name": "gsi1pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "gsi1sk", "type": _dynamodb.AttributeType.STRING},
        )
        self.table.add_global_secondary_index(
            index_name="gsi2",
            partition_key={"name": "gsi2pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "gsi2sk", "type": _dynamodb.AttributeType.STRING},
        )
