from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine

# 系统演员：实验室结果触发的自动立案 / 禁赛确认由系统完成，
# 与提交结果的实验室角色解耦，也保证重启后幂等。
SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "lab_result" and action == "reconcile":
            return self._reconcile_result(actor, entity, data, expected_version)
        if entity["kind"] == "lab_result" and action == "resolve":
            return self._resolve_manual(actor, entity, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # 对账：把实验室结果按封条号 / 采样时刻并到样本上，再驱动案件链路。
    # 只处理 pending 结果；每一步幂等，服务重启后从断点续办。
    # ------------------------------------------------------------------

    def reconcile(self, actor, result_id=None):
        if result_id:
            entity = self.repository.get_entity(result_id)
            if not entity:
                raise NotFoundError("lab result not found: " + result_id)
            results = [entity]
        else:
            results = self.repository.list_entities("lab_result", status="pending")
        processed = []
        for entity in results:
            if entity["status"] != "pending":
                continue
            processed.append(self._reconcile_result(actor, entity, None, None))
        return {"items": processed, "processed": len(processed)}

    def _reconcile_result(self, actor, entity, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, entity, "reconcile", dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if next_status == "matched":
            sample = self.repository.get_entity(patch["sample_id"])
            if not sample:
                raise NotFoundError("sample not found: " + patch["sample_id"])
            # 幂等：结果已落到样本上时（崩溃后续办）不重复应用。
            if sample["data"].get("current_result_id") != entity["id"]:
                self._apply_result(actor, sample, entity)
        else:
            self._notify(actor, entity["id"], "manual", "manual_review_required", entity)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"], actor, "reconcile", entity["status"], next_status, patch
        )
        return updated

    def _resolve_manual(self, actor, entity, data, expected_version):
        next_status, patch = self.rules.validate_transition(
            actor, entity, "resolve", dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        sample = self.repository.get_entity(patch["sample_id"])
        # 幂等：结果已落到样本上时不重复应用。
        if sample["data"].get("current_result_id") != entity["id"]:
            self._apply_result(actor, sample, entity)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"], actor, "resolve", entity["status"], next_status, patch
        )
        return updated

    def _apply_result(self, actor, sample, result):
        """把实验室结果落到样本上，并驱动案件 / 禁赛链路。幂等。"""
        rdata = result["data"]
        sdata = dict(sample["data"])
        # 回填：旧样本没有封条号时，用结果上的封条号补上。
        if not sdata.get("seal_id") and rdata.get("seal_id"):
            sdata["seal_id"] = rdata["seal_id"]
        sdata["result"] = rdata["result"]
        sdata["current_result_id"] = result["id"]
        sdata["result_updated_at"] = rdata.get("reported_at")
        if rdata.get("b_sample"):
            sdata["b_confirmed"] = True
        target = "adverse" if rdata["result"] == "adverse" else "cleared"
        final = target if sample["status"] in ("received", "analyzed", "adverse", "cleared") else sample["status"]
        updated_sample = self.repository.update_entity(
            sample["id"], sample["version"], final, sdata
        )
        self.audit.record(
            sample["id"],
            actor,
            "record_result",
            sample["status"],
            final,
            {
                "result_id": result["id"],
                "result": rdata["result"],
                "b_sample": bool(rdata.get("b_sample")),
            },
        )
        if rdata["result"] == "adverse":
            case = self._find_case_for_sample(sample["id"])
            if not case:
                case = self.create(
                    SYSTEM_ACTOR,
                    "case",
                    {
                        "athlete_id": sdata.get("athlete_id") or rdata.get("athlete_id"),
                        "sample_id": sample["id"],
                        "alleged_rule": rdata.get("alleged_rule")
                        or "anti-doping-rule-violation",
                    },
                    idempotency_key="lab-result:%s:case" % result["id"],
                )
            self._reconfirm_suspension(SYSTEM_ACTOR, case, result)
        else:
            case = self._find_case_for_sample(sample["id"])
            if case and case["status"] in ("open", "suspended", "hearing"):
                self._lift_suspension(SYSTEM_ACTOR, case, result)
        return updated_sample

    def _find_case_for_sample(self, sample_id):
        cases = self.repository.find_entities("case", "sample_id", sample_id)
        return cases[0] if cases else None

    def _reconfirm_suspension(self, actor, case, result):
        reason = "adverse result %s" % result["id"]
        cdata = dict(case["data"])
        now = utcnow()
        if case["status"] == "open":
            cdata["suspended_at"] = now
            cdata["suspension_reason"] = reason
            updated = self.repository.update_entity(
                case["id"], case["version"], "suspended", cdata
            )
            self.audit.record(
                case["id"], actor, "provisional_suspend", "open", "suspended",
                {"reason": reason, "result_id": result["id"]},
            )
            self._notify(actor, case["id"], "suspended", "case_opened", result)
        elif case["status"] in ("suspended", "hearing"):
            cdata["last_reconfirmed_at"] = now
            cdata["reconfirm_reason"] = reason
            updated = self.repository.update_entity(
                case["id"], case["version"], "suspended", cdata
            )
            self.audit.record(
                case["id"], actor, "reconfirm_suspension", case["status"], "suspended",
                {"result_id": result["id"]},
            )
            self._notify(actor, case["id"], "suspended", "suspension_reconfirmed", result)
        elif case["status"] == "closed":
            cdata["reopened_at"] = now
            cdata["reopen_reason"] = reason
            updated = self.repository.update_entity(
                case["id"], case["version"], "suspended", cdata
            )
            self.audit.record(
                case["id"], actor, "reopen_case", "closed", "suspended",
                {"result_id": result["id"]},
            )
            self._notify(actor, case["id"], "suspended", "case_reopened", result)

    def _lift_suspension(self, actor, case, result):
        cdata = dict(case["data"])
        cdata["decision"] = "no_sanction"
        cdata["lifted_at"] = utcnow()
        updated = self.repository.update_entity(
            case["id"], case["version"], "closed", cdata
        )
        self.audit.record(
            case["id"], actor, "lift_suspension", case["status"], "closed",
            {"result_id": result["id"]},
        )
        self._notify(actor, case["id"], "closed", "suspension_lifted", result)

    def _notify(self, actor, entity_id, status, ntype, result):
        # 幂等：同一结果同一类型的通知只记一次，重启不重复通知。
        for entry in self.repository.list_audit(entity_id):
            if (
                entry["action"] == "notify"
                and entry["detail"].get("result_id") == result["id"]
                and entry["detail"].get("type") == ntype
            ):
                return
        self.audit.record(
            entity_id,
            actor,
            "notify",
            None,
            status,
            {"type": ntype, "result_id": result["id"], "channel": "email"},
        )

    # ------------------------------------------------------------------
    # 回填：旧样本没有封条号时，按采样时刻 + 运动员从实验室结果补封条号。
    # ------------------------------------------------------------------

    def backfill_seals(self, actor):
        samples = self.repository.list_entities("sample")
        results = self.repository.list_entities("lab_result")
        backfilled = 0
        for sample in samples:
            if sample["data"].get("seal_id"):
                continue
            athlete_id = sample["data"].get("athlete_id")
            collected_at = sample["data"].get("collected_at")
            if not athlete_id or not collected_at:
                continue
            day = str(collected_at)[:10]
            for result in results:
                rdata = result["data"]
                if not rdata.get("seal_id"):
                    continue
                if rdata.get("athlete_id") != athlete_id:
                    continue
                rday = str(rdata.get("collected_at") or rdata.get("reported_at"))[:10]
                if rday != day:
                    continue
                sdata = dict(sample["data"])
                sdata["seal_id"] = rdata["seal_id"]
                self.repository.update_entity(
                    sample["id"], sample["version"], sample["status"], sdata
                )
                self.audit.record(
                    sample["id"], actor, "backfill_seal", sample["status"], sample["status"],
                    {"seal_id": rdata["seal_id"], "result_id": result["id"]},
                )
                backfilled += 1
                break
        return {"backfilled": backfilled}
