"""DynamoDB single-table repository (doc section 3.3).

Table: `recruiter` (on-demand) with GSI1 (rankings per job) and GSI2 (dedup).
Every method takes the authenticated `sub` explicitly; API functions never read a
user id from request payload (NFR-11). Cross-user reads simply return None / 404.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer

from .keys import (
    analysis_sk,
    cand_sk,
    gsi_dedup_pk,
    gsi_rank_pk,
    job_sk,
    rank_sort_key,
    role_sk,
    user_pk,
)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _to_decimal(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


_TDES = TypeDeserializer()


def _from_ddb(value: Any) -> Any:
    return _TDES.deserialize(value)


class Repo:
    """resource-level tables (and moto) return already-deserialized items."""

    _resource_level = True

    def __init__(self, table_name: str, region: str | None = None, resource: Any = None,
                 resource_level: bool | None = None):
        """resource_level=True forces the already-deserialized shape (tests)."""
        if resource_level is not None:
            self._resource_level = bool(resource_level)
        if resource is not None:  # moto / test injection
            self._table = resource.Table(table_name)
        else:
            self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # ---------- low level --------------------------------------------------------

    def put(self, item: dict) -> None:
        self._table.put_item(Item=_to_decimal(item))

    def _item(self, resp: dict) -> dict | None:
        raw = resp.get("Item")
        if raw is None:
            return None
        # resource-level Table already deserializes; client-level requires it.
        if self._resource_level:
            return dict(raw)
        return _from_ddb({"M": raw})

    def count_items(self, sub: str) -> dict[str, int]:
        """Counts by item type for the dashboard (C#, J#, A#)."""
        counts = {"candidates": 0, "jobs": 0, "analyses": 0}
        paginator = self._table.meta.client.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table.name,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": user_pk(sub)},
        ):
            for it in page.get("Items", []):
                raw = dict(it)
                sk = raw["sk"]
                if sk.startswith("C#"):
                    counts["candidates"] += 1
                elif sk.startswith("J#"):
                    counts["jobs"] += 1
                elif sk.startswith("A#"):
                    counts["analyses"] += 1
        return counts

    # ---------- candidates (FR-14, FR-02/03) --------------------------------------

    def create_candidate(self, sub: str, cid: str, *, s3_key: str, filename: str,
                         sha256: str | None = None, status: str = "PROCESSING") -> dict:
        item = {
            "pk": user_pk(sub), "sk": cand_sk(cid),
            "candidateId": cid, "status": status, "s3Key": s3_key,
            "filename": filename, "sha256": sha256,
            "createdAt": now_iso(),
            "gsi2pk": gsi_dedup_pk(sub, sha256) if sha256 else None,
            "gsi2sk": cand_sk(cid),
        }
        item = {k: v for k, v in item.items() if v is not None}
        self.put(item)
        return item

    def get_candidate(self, sub: str, cid: str) -> dict | None:
        return self._item(self._table.get_item(Key={"pk": user_pk(sub), "sk": cand_sk(cid)}))

    @staticmethod
    def _existing_only(updates: dict) -> tuple:
        """Build UpdateExpression parts guarded by attribute_exists(pk AND sk).

        Without this guard DynamoDB's update_item silently UPSERTS: a pipeline
        write against a job/candidate that was never created (or was already
        deleted) chould otherwise materialise a partial 'ghost' item that
        breaks list rendering (no jobId/candidateId) and every action on it.
        """
        expr, names, values = [], {"#k": "pk", "#s": "sk"}, {}
        for i, (key, val) in enumerate(updates.items()):
            if val is None:
                continue
            n, v = f"#u{i}", f":u{i}"
            names[n] = key
            values[v] = _to_decimal(val)
            expr.append(f"{n} = {v}")
        condition = "attribute_exists(#k) AND attribute_exists(#s)"
        return expr, names, values, condition

    def list_candidates(self, sub: str, status: str | None = None,
                        limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :c)",
            "ExpressionAttributeValues": {":pk": user_pk(sub), ":c": "C#"},
            "Limit": max(1, min(200, limit)),
        }
        if status:
            kwargs["FilterExpression"] = "#st = :st"
            kwargs["ExpressionAttributeNames"] = {"#st": "status"}
            kwargs["ExpressionAttributeValues"][":st"] = status
        if cursor:
            kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(cursor))
        resp = self._table.query(**kwargs)
        items = [dict(it) for it in resp.get("Items", [])]
        lek = resp.get("LastEvaluatedKey")
        next_cursor = base64.urlsafe_b64encode(json.dumps(lek).encode()).decode() if lek else None
        return items, next_cursor

    def update_candidate(self, sub: str, cid: str, updates: dict) -> dict:
        """Merge-update the candidate item (profile, status, failure reason...).

        Fails with ConditionCheckFailedException when the candidate item is
        absent — never creates a ghost row (see _existing_only).
        """
        expr, names, values, condition = self._existing_only(updates)
        resp = self._table.update_item(
            Key={"pk": user_pk(sub), "sk": cand_sk(cid)},
            UpdateExpression="SET " + ", ".join(expr),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return dict(resp["Attributes"])

    def delete_candidate_cascade(self, sub: str, cid: str) -> int:
        """Hard delete candidate + its analyses + role suggestions (FR-14).

        The caller separately deletes the S3 objects and the vector entries.
        Only EXISTING items are touched — deleting another user's ids or a
        missing candidate deletes nothing and returns 0.
        """
        pk = user_pk(sub)
        c_sk = cand_sk(cid)
        r_sk = role_sk(cid)
        a_suffix = f"#{cid}"

        # Collect keys to delete: only ones that actually exist under this user's pk.
        keys_to_delete: list[dict[str, str]] = [
            {"pk": pk, "sk": sk} for sk in (c_sk, r_sk)
            if self._table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        ]

        paginator = self._table.meta.client.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table.name,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": user_pk(sub)},
            ProjectionExpression="pk, sk",
        ):
            for item in page.get("Items", []):
                sk_val = item.get("sk", "")
                if sk_val.startswith("A#") and sk_val.endswith(a_suffix):
                    keys_to_delete.append({"pk": item["pk"], "sk": sk_val})

        deleted = 0
        with self._table.batch_writer() as batch:
            for key in keys_to_delete:
                batch.delete_item(Key=key)
                deleted += 1
        return deleted

    def dedup_lookup(self, sub: str, sha256: str) -> dict | None:
        if not sha256:
            return None
        resp = self._table.query(
            IndexName="gsi2",
            KeyConditionExpression="gsi2pk = :h",
            ExpressionAttributeValues={":h": gsi_dedup_pk(sub, sha256)},
            Limit=1,
        )
        items = resp.get("Items", [])
        return dict(items[0]) if items else None

    # ---------- jobs (FR-04) ------------------------------------------------------

    def create_job(self, sub: str, jid: str, *, title: str, jd_s3_key: str | None,
                   rubric_weights: dict[str, int], status: str = "PARSING") -> dict:
        item = {
            "pk": user_pk(sub), "sk": job_sk(jid),
            "jobId": jid, "title": title or "(untitled job)",
            "jdS3Key": jd_s3_key, "status": status,
            "rubricWeights": rubric_weights,
            "createdAt": now_iso(),
        }
        item = {k: v for k, v in item.items() if v is not None}
        self.put(item)
        return item

    def get_job(self, sub: str, jid: str) -> dict | None:
        return self._item(self._table.get_item(Key={"pk": user_pk(sub), "sk": job_sk(jid)}))

    def list_jobs(self, sub: str, limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :j)",
            "ExpressionAttributeValues": {":pk": user_pk(sub), ":j": "J#"},
            "Limit": max(1, min(200, limit)),
        }
        if cursor:
            kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(cursor))
        resp = self._table.query(**kwargs)
        # Rows missing their id attribute are ghost upserts (DynamoDB
        # update_item created a partial item); they can never be opened or
        # deleted from the UI, so drop them instead of listing poison.
        items = [dict(it) for it in resp.get("Items", []) if it.get("jobId")]
        lek = resp.get("LastEvaluatedKey")
        next_cursor = base64.urlsafe_b64encode(json.dumps(lek).encode()).decode() if lek else None
        return items, next_cursor

    def update_job(self, sub: str, jid: str, updates: dict) -> dict:
        """Merge-update a job (parse output, status, edits). Fails with
        ConditionCheckFailedException when the job item is absent — never
        creates a ghost row (see _existing_only)."""
        expr, names, values, condition = self._existing_only(updates)
        resp = self._table.update_item(
            Key={"pk": user_pk(sub), "sk": job_sk(jid)},
            UpdateExpression="SET " + ", ".join(expr),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return dict(resp["Attributes"])

    def delete_job_cascade(self, sub: str, jid: str) -> int:
        """Hard delete a job + all of its analysis rows. Only existing items
        under this user's pk are touched — missing jobs delete nothing (0)."""
        pk = user_pk(sub)
        j_sk = job_sk(jid)
        a_prefix = f"A#{jid}#"
        keys_to_delete: list[dict[str, str]] = [
            {"pk": pk, "sk": j_sk}
        ] if self._table.get_item(Key={"pk": pk, "sk": j_sk}).get("Item") else []

        paginator = self._table.meta.client.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table.name,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": user_pk(sub)},
            ProjectionExpression="pk, sk",
        ):
            for item in page.get("Items", []):
                sk_val = item.get("sk", "")
                if sk_val.startswith(a_prefix):
                    keys_to_delete.append({"pk": item["pk"], "sk": sk_val})

        deleted = 0
        with self._table.batch_writer() as batch:
            for key in keys_to_delete:
                batch.delete_item(Key=key)
                deleted += 1
        return deleted

    # ---------- analyses + rankings (FR-06, FR-09) ---------------------------------

    def put_analysis(self, sub: str, analysis: dict) -> dict:
        cid, jid = analysis["candidateId"], analysis["jobId"]
        item = dict(analysis)
        item.update({
            "pk": user_pk(sub),
            "sk": analysis_sk(jid, cid),
            "gsi1pk": gsi_rank_pk(sub, jid),
            "gsi1sk": f"{rank_sort_key(analysis['totalScore'])}#{cid}",
        })
        self.put(item)
        return item

    def get_analysis(self, sub: str, jid: str, cid: str) -> dict | None:
        return self._item(self._table.get_item(
            Key={"pk": user_pk(sub), "sk": analysis_sk(jid, cid)}))

    def list_rankings(self, sub: str, jid: str, min_score: int = 0,
                      limit: int = 200) -> list[dict]:
        resp = self._table.query(
            IndexName="gsi1",
            KeyConditionExpression="gsi1pk = :k",
            ExpressionAttributeValues={":k": gsi_rank_pk(sub, jid)},
        )
        items = []
        for it in resp.get("Items", []):
            raw = dict(it)
            if int(raw.get("totalScore", 0)) >= min_score:
                items.append(raw)
        items.sort(key=lambda r: (-int(r.get("totalScore", 0)), r.get("candidateId", "")))
        return items[:limit]

    def list_analyses_for_job(self, sub: str, jid: str) -> list[dict]:
        resp = self._table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :a)",
            ExpressionAttributeValues={":pk": user_pk(sub), ":a": f"A#{jid}#"},
        )
        return [dict(it) for it in resp.get("Items", [])]

    def put_role_suggestion(self, sub: str, cid: str, roles: list[dict]) -> None:
        self.put({
            "pk": user_pk(sub), "sk": role_sk(cid),
            "candidateId": cid, "roles": roles, "createdAt": now_iso(),
        })

    def get_role_suggestion(self, sub: str, cid: str) -> list[dict] | None:
        item = self._item(self._table.get_item(Key={"pk": user_pk(sub), "sk": role_sk(cid)}))
        return item.get("roles") if item else None

    # ---------- failures / observability ------------------------------------------

    def record_failure(self, sub: str, cid: str, reason: str) -> None:
        self.update_candidate(sub, cid, {"status": "FAILED", "failureReason": reason[:1000]})

    def increment_daily_usage(self, sub: str) -> int:
        """Atomically bump today's usage counter and return the new value (NFR-25)."""
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        resp = self._table.update_item(
            Key={"pk": user_pk(sub), "sk": f"USAGE#{today}"},
            UpdateExpression="ADD #c :one SET #t = :now",
            ExpressionAttributeNames={"#c": "count", "#t": "updatedAt"},
            ExpressionAttributeValues={":one": 1, ":now": now_iso()},
            ReturnValues="ALL_NEW",
        )
        item = dict(resp["Attributes"])
        return int(item.get("count", 1))
