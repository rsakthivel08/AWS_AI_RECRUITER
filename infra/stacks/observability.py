"""ObservabilityStack: dashboards, alarms, SNS alerts, Budgets (doc section 7.1)."""
from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_budgets as _budgets
from aws_cdk import aws_cloudwatch as _cloudwatch
from aws_cdk import aws_sns as _sns
from aws_cdk import aws_sns_subscriptions as _subs
from constructs import Construct

from stacks.common import EnvSettings


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, *,
                 ingesters: dict, dqs: list, state_machines: list,
                 api=None, frontend_id: str = "", user_pool_id: str = "",
                 budget_monthly_usd: int = 5, alert_email: str | None = None,
                 **kw) -> None:
        super().__init__(scope, id, **kw)
        prefix = f"ar-{settings.env}"

        # ---- SNS email topic (prod: alarms on; dev: topic still free, no subs) ---
        self.topic = _sns.Topic(self, "alerts", topic_name=f"{prefix}-alerts".replace("/", "-"))
        if settings.alarm_email:
            self.topic.add_subscription(_subs.EmailSubscription(settings.alarm_email))

        alarms = []
        for name, function in ingesters.items():
            alarms.append(_cloudwatch.Alarm(
                self, f"err-{name}",
                metric=function.metric_errors(),
                threshold=1, evaluation_periods=1, treat_missing_data=_cloudwatch.TreatMissingData.IGNORE,
                alarm_description=f"{name} lambda errors",
            ))
        for dq in dqs:
            alarms.append(_cloudwatch.Alarm(
                self, f"dlq-{dq.node.id}",
                metric=dq.metric("ApproximateNumberOfMessagesVisible"),
                threshold=1, evaluation_periods=1, treat_missing_data=_cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="DLQ depth above zero (NFR-06/NFR-24)",
            ))
        for sm in state_machines:
            alarms.append(_cloudwatch.Alarm(
                self, f"sm-{sm.node.id}",
                metric=sm.metric("ExecutionsFailed"),
                threshold=1, evaluation_periods=1, treat_missing_data=_cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="Step Functions failed executions",
            ))
        for alarm in alarms:
            if settings.alarm_email:
                alarm.add_alarm_action(_cloudwatch.SnsAction(self.topic))

        # ---- dashboard -----------------------------------------------------------
        dashboard = _cloudwatch.Dashboard(
            self, "ops", dashboard_name=f"{prefix}-ops")
        widgets: list[_cloudwatch.IWidget] = [
            _cloudwatch.TextWidget(markdown="# AI Recruiter Ops", width=24, height=2)
        ]
        for name, function in list(ingesters.items())[:6]:
            widgets.append(_cloudwatch.GraphWidget(
                title=f"{name} errors/invocations",
                left=[function.metric_errors()],
                right=[function.metric_invocations()],
                width=12, height=6))
        dashboard.add_widgets(*widgets)

        # ---- budget (NFR-25): 50 / 80 / 100 percent alerts ----------------------
        _budgets.CfnBudget(
            self, "monthlyBudget",
            budget={
                "budgetName": f"ar-{settings.env}-monthly",
                "budgetType": "COST",
                "timeUnit": "MONTHLY",
                "budgetLimit": {"amount": float(budget_monthly_usd), "unit": "USD"},
            },
            notifications_with_subscribers=[
                {"notification": {"notificationType": "ACTUAL",
                                  "comparisonOperator": "GREATER_THAN",
                                  "threshold": float(pct), "thresholdType": "PERCENTAGE",
                                  "notificationState": "ALARM"},
                 "subscribers": [{"subscriptionType": "EMAIL",
                                  "address": settings.alarm_email or "no-alerts@example.com"}]}
                for pct in (50, 80, 100)
            ],
        )
