"""DynamoDB repo tests against moto (keeps CI offline)."""
import unittest.mock as mock

import pytest

moto = pytest.importorskip("moto")

from src.shared.repo import Repo  # noqa: E402

TABLE = "recruiter-test"
SUB = "sub-abc"


def _dynamo_table(dynamo):
    return dynamo.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
            {"AttributeName": "gsi2pk", "AttributeType": "S"},
            {"AttributeName": "gsi2sk", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {"IndexName": "gsi1",
             "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"},
                           {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "gsi2",
             "KeySchema": [{"AttributeName": "gsi2pk", "KeyType": "HASH"},
                           {"AttributeName": "gsi2sk", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture()
def repo():
    with moto.mock_aws():
        import boto3

        dynamo = boto3.resource("dynamodb", region_name="ap-south-1")
        _dynamo_table(dynamo)
        with mock.patch.object(Repo, "__init__", lambda self, table_name, **kw:
                               setattr(self, "_table", dynamo.Table(table_name))):
            yield Repo(TABLE, region="ap-south-1")


def analysis(cid: str, jid: str, score: int) -> dict:
    return {"candidateId": cid, "jobId": jid, "totalScore": score,
            "criteria": [], "gaps": [], "claimChecks": [],
            "flags": [], "questions": [], "modelId": "m", "promptVersion": "v1"}


def test_candidate_roundtrip_and_user_scoping(repo):
    repo.create_candidate(SUB, "c_1", s3_key="uploads/sub-abc/resumes/1.pdf",
                          filename="1.pdf", sha256="abc")
    got = repo.get_candidate(SUB, "c_1")
    assert got and got["candidateId"] == "c_1"
    assert repo.get_candidate("someone-else", "c_1") is None       # FR-15 negative
    items, _ = repo.list_candidates("someone-else")
    assert items == []


def test_job_lifecycle_and_rubric_update(repo):
    repo.create_job(SUB, "j_1", title="Backend engineer",
                    jd_s3_key=None, rubric_weights={"requiredSkills": 35}, status="PARSING")
    assert repo.get_job(SUB, "j_1")["status"] == "PARSING"
    updated = repo.update_job(SUB, "j_1", {"status": "READY",
                                           "rubricWeights": {"requiredSkills": 40}})
    assert updated["status"] == "READY" and updated["rubricWeights"]["requiredSkills"] == 40
    assert repo.get_job("other-user", "j_1") is None


def test_analysis_ranking_order_and_min_score(repo):
    repo.put_analysis(SUB, analysis("c_1", "j_1", 72))
    repo.put_analysis(SUB, analysis("c_2", "j_1", 90))
    repo.put_analysis(SUB, analysis("c_3", "j_1", 20))
    ranked = repo.list_rankings(SUB, "j_1")
    assert [r["candidateId"] for r in ranked] == ["c_2", "c_1", "c_3"]
    assert [r["candidateId"] for r in repo.list_rankings(SUB, "j_1", min_score=50)] == [
        "c_2", "c_1"]
    assert repo.list_rankings("other-user", "j_1") == []


def test_dedup_lookup_scopes_to_user(repo):
    repo.create_candidate(SUB, "c_1", s3_key="k", filename="f", sha256="beef")
    hit = repo.dedup_lookup(SUB, "beef")
    assert hit and hit["candidateId"] == "c_1"
    assert repo.dedup_lookup("other-user", "beef") is None


def test_cascade_delete_removes_everything(repo):
    repo.create_candidate(SUB, "c_9", s3_key="k", filename="f", sha256="s1")
    repo.put_analysis(SUB, analysis("c_9", "j_1", 50))
    repo.put_role_suggestion(SUB, "c_9", [{"role": "dev", "score": 80}])
    deleted = repo.delete_candidate_cascade(SUB, "c_9")
    assert deleted >= 3
    assert repo.get_candidate(SUB, "c_9") is None
    assert repo.get_analysis(SUB, "j_1", "c_9") is None
    assert repo.get_role_suggestion(SUB, "c_9") is None


def test_increment_daily_usage(repo):
    first = repo.increment_daily_usage(SUB)
    second = repo.increment_daily_usage(SUB)
    assert second == first + 1
