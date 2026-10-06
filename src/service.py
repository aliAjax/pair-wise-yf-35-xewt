from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .rules import RuleEngine

SYSTEM_ACTOR = Actor("system", "system")


def _instant(value):
    """归一化 ISO 时刻字符串，用于按封条号+采样时刻精确匹配。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    return text


def _make_id(prefix, *parts):
    return prefix + "-" + "|".join(str(part) for part in parts)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    # ------------------------------------------------------------------
    # 基础用例
    # ------------------------------------------------------------------
    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _find(self, kind, field, value):
        rows = self._lookup(kind, field, value)
        return rows[0] if rows else None

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
    # 赛外检查：无信号现场先记本地，网络恢复后整包补传
    # ------------------------------------------------------------------
    def record_collection_packet(self, packet, actor=None):
        """记录离线采集包。

        packet 至少包含 athlete_id、sample_code、event、collected_at、seal_id。
        全部派生 ID 都由业务自然键决定（seal_id / sample_code），
        因此断网期间重复保存、服务重启后再次补传都不会产生重复记录。
        """
        actor = actor or Actor("inspector-offline", "inspector")
        data = dict(packet or {})
        for field in ("athlete_id", "sample_code", "event", "collected_at", "seal_id"):
            if not data.get(field):
                raise ValidationError("collection packet missing field: " + field)

        seal_id = str(data["seal_id"]).strip()
        handover_id = _make_id("seal", seal_id)
        handover = self.repository.get_entity(handover_id)
        if not handover:
            handover = self.create(
                actor,
                "seal_handover",
                {
                    "id": handover_id,
                    "seal_id": seal_id,
                    "athlete_id": data["athlete_id"],
                    "collected_at": _instant(data["collected_at"]),
                    "event": data.get("event", ""),
                    "recorded_offline": True,
                    "carrier": data.get("carrier", ""),
                },
            )
        elif handover["data"].get("collected_at") != _instant(data["collected_at"]):
            raise ConflictError(
                "seal %s already recorded with a different collected_at" % seal_id
            )

        sample_id = _make_id("sample", str(data["sample_code"]).strip())
        sample = self.repository.get_entity(sample_id)
        if sample:
            return {"sample": sample, "seal_handover": handover, "replayed": True}

        sample_payload = {
            "id": sample_id,
            "athlete_id": data["athlete_id"],
            "sample_code": str(data["sample_code"]).strip(),
            "event": data.get("event", ""),
            "seal_id": seal_id,
            "collected_at": _instant(data["collected_at"]),
            "collected_offline": True,
            "local_note": data.get("local_note", ""),
        }
        # 样本直接以 collected 状态入库（现场已采集并封存）。
        sample = self.repository.create_entity(
            sample_id, "sample", "collected", sample_payload, actor.user_id
        )
        self.audit.record(sample_id, actor, "create", None, "collected",
                          {"kind": "sample", "offline_packet": True})
        return {"sample": sample, "seal_handover": handover, "replayed": False}

    # ------------------------------------------------------------------
    # 实验室结果上报：首报 + 复检更正按 report_no/revision 版本化
    # ------------------------------------------------------------------
    def upload_lab_result(self, result, actor=None):
        actor = actor or Actor("lab-ingest", "lab")
        data = dict(result or {})
        data["collected_at"] = _instant(data.get("collected_at"))
        revision = int(data.get("revision", 1))
        data["revision"] = revision
        data.setdefault("substance", "")
        data.setdefault("remark", "")
        report_no = str(data.get("report_no", "")).strip()
        data.setdefault("report_revision", "%s#%s" % (report_no, revision))

        # 已存在同报告号+修订次：实验室重传同一条结果是幂等重放，
        # 直接返回旧记录，created=False，不按更正处理。
        duplicate = self._find("lab_result", "report_revision", data["report_revision"])
        if duplicate:
            return duplicate, False

        result_id = data.get("id") or _make_id("result", report_no, revision)
        if self.repository.get_entity(result_id):
            raise ConflictError("entity already exists: " + result_id)
        data["id"] = result_id
        entity = self.create(actor, "lab_result", data)
        return entity, True
    # ------------------------------------------------------------------
    # 对账：只处理尚未匹配/判废的结果，天然支持中断后续办
    # ------------------------------------------------------------------
    def reconcile(self, actor=None, result_ids=None, notify=True):
        actor = actor or SYSTEM_ACTOR
        wanted = set(result_ids) if result_ids else None
        pending = []
        for item in self.repository.list_entities(kind="lab_result"):
            if wanted is not None and item["id"] not in wanted:
                continue
            if item["status"] in ("recorded",):
                pending.append(item)
            elif item["status"] == "matched":
                # 崩溃恢复：结果已匹配到样本，但案件链还没消化过这条结果时，
                # 仍属于"没对完"，需要续办。
                sample = self.repository.get_entity(item["data"].get("sample_id", ""))
                case = self._find("case", "sample_id", sample["id"]) if sample else None
                digested = False
                if case:
                    if case["data"].get("last_result_id") == item["id"]:
                        digested = True
                    elif case["status"] in ("hearing", "closed", "appeal"):
                        # 已进入听证/结案流程，说明结论已被下游消化。
                        digested = True
                if not digested:
                    pending.append(item)
        processed = []
        for lab_result in pending:
            outcome = self._reconcile_one(actor, lab_result, notify=notify)
            processed.append(outcome)
        return {"processed": len(processed), "items": processed}

    def _reconcile_one(self, actor, lab_result, notify=True):
        data = lab_result["data"]
        seal_id = str(data.get("seal_id", "")).strip()
        collected_at = _instant(data.get("collected_at"))

        # 已经匹配到样本的（续办场景）：直接沿链路传播。
        sample = None
        if lab_result["status"] == "matched":
            sample = self.repository.get_entity(data.get("sample_id", ""))

        # 先按封条号 + 采样时刻精确合并。
        if not sample:
            sample = self._find("sample", "seal_id", seal_id)
        if sample and sample["data"].get("collected_at") != collected_at:
            sample = None
        if not sample:
            # 旧记录没有封条号：退回运动员 + 采样时刻。
            athlete_id = data.get("athlete_id")
            if athlete_id:
                candidates = [
                    row for row in self._lookup("sample", "athlete_id", athlete_id)
                    if not row["data"].get("seal_id")
                    and row["data"].get("collected_at") == collected_at
                ]
                if len(candidates) == 1:
                    sample = candidates[0]
        if not sample:
            # 判不出（无封条、无运动员或多义）：交人工，不阻塞后续样本。
            review = self._open_review(
                actor,
                topic="lab_result_match",
                reason="cannot match lab result to a unique sample",
                payload={
                    "lab_result_id": lab_result["id"],
                    "seal_id": seal_id,
                    "collected_at": collected_at,
                    "athlete_id": data.get("athlete_id", ""),
                    "result": data.get("result"),
                    "aliquot": data.get("aliquot"),
                },
                review_id=_make_id("review", "match", lab_result["id"]),
            )
            self.transition(actor, lab_result["id"], "flag_review",
                            {"reason": review["id"]})
            return {"lab_result_id": lab_result["id"], "status": "manual_review",
                    "manual_review_id": review["id"]}

        return self._apply_result(actor, lab_result, sample, notify=notify)

    def _apply_result(self, actor, lab_result, sample, notify=True):
        """把一条已匹配结果沿链路向下游传播，全程幂等。"""
        data = lab_result["data"]

        # 1) 结果实体：标记匹配，并作废同一等分（A/B）上被本修订替代的旧结果。
        if lab_result["status"] == "recorded":
            self.transition(actor, lab_result["id"], "match",
                            {"sample_id": sample["id"]})
        for older in self._lookup("lab_result", "sample_id", sample["id"]):
            if older["id"] == lab_result["id"] or older["status"] != "matched":
                continue
            old = older["data"]
            if (old.get("aliquot") == data.get("aliquot")
                    and int(old.get("revision", 1)) < int(data.get("revision", 1))):
                self.transition(actor, older["id"], "supersede",
                                {"by": lab_result["id"]})
                older["status"] = "superseded"

        # 2) 样本链写回结论。
        findings = {
            "result": data["result"],
            "result_id": lab_result["id"],
            "revision": data.get("revision", 1),
            "seal_id": sample["data"].get("seal_id") or data.get("seal_id", ""),
        }
        if not sample["data"].get("seal_id") and data.get("seal_id"):
            # 旧记录靠运动员+采样时刻命中：合并时把封条号回填进样本链。
            findings["seal_backfilled"] = True
        if data.get("substance"):
            findings["substance"] = data["substance"]
        sample = self.transition(actor, sample["id"], "reconcile_findings", findings)

        # 3) 案件链：阳性立案/重新确认临时禁赛；阴性更正则解除禁赛。
        case = self._find("case", "sample_id", sample["id"])
        # 崩溃恢复短路：该结果已经传播过（已记账），重跑只补匹配状态，
        # 不再重复立案或通知。
        already_applied = bool(case and case["data"].get("last_result_id") == lab_result["id"])
        case_action = None
        if data["result"] == "adverse" and not already_applied:
            if not case:
                case = self.create(
                    actor,
                    "case",
                    {
                        "id": _make_id("case", sample["id"]),
                        "athlete_id": sample["data"].get("athlete_id", ""),
                        "sample_id": sample["id"],
                        "alleged_rule": data.get("substance") or "adverse-finding",
                        "origin_result_id": lab_result["id"],
                    },
                )
            if case["status"] in ("suspended", "hearing"):
                # 结果一更新，临时禁赛就重新确认（听证准备也要退回重确认）。
                case = self.transition(
                    actor, case["id"], "reconfirm_suspension",
                    {"result_id": lab_result["id"],
                     "reason": "result updated: revision %s" % data.get("revision", 1)},
                )
                case_action = "reconfirmed"
            elif case["status"] in ("open", "reopened"):
                case = self.transition(
                    actor, case["id"], "provisional_suspend",
                    {"reason": "adverse lab result %s" % lab_result["id"],
                     "result_id": lab_result["id"]},
                )
                case_action = "suspended"
            else:
                # 已结案/申诉中的案件收到新结论：不能静默改旧结论，交人工。
                self._open_review(
                    actor,
                    topic="closed_case_new_result",
                    reason="new lab result for a case already concluded",
                    payload={"case_id": case["id"], "lab_result_id": lab_result["id"]},
                    review_id=_make_id("review", "case", case["id"], lab_result["id"]),
                )
                case_action = "manual_review"
            case = self.repository.get_entity(case["id"])
            if case["data"].get("last_result_id") != lab_result["id"]:
                # last_result_id 记账：同一条结果的任何重放都短路。
                merged = dict(case["data"])
                merged["last_result_id"] = lab_result["id"]
                case = self.repository.update_entity(
                    case["id"], case["version"], case["status"], merged
                )
            if notify and case_action in ("suspended", "reconfirmed"):
                self._notify(
                    actor,
                    recipient=sample["data"].get("athlete_id", ""),
                    channel="provisional_suspension",
                    subject="provisional suspension %s" % case_action,
                    payload={
                        "case_id": case["id"],
                        "sample_id": sample["id"],
                        "lab_result_id": lab_result["id"],
                        "revision": data.get("revision", 1),
                        "action": case_action,
                    },
                )
        elif (data["result"] == "cleared" and case
                and case["status"] in ("suspended", "hearing")
                and not already_applied):
            # 复检更正为阴性（如 B 样阴性）：撤销临时禁赛，案件退回重开。
            self.transition(
                actor, case["id"], "lift_suspension",
                {"reason": "corrected lab result %s" % lab_result["id"],
                 "result_id": lab_result["id"]},
            )
            case_action = "lifted"
            case = self.repository.get_entity(case["id"])
            merged = dict(case["data"])
            merged["last_result_id"] = lab_result["id"]
            case = self.repository.update_entity(
                case["id"], case["version"], case["status"], merged
            )
            if notify:
                self._notify(
                    actor,
                    recipient=sample["data"].get("athlete_id", ""),
                    channel="suspension_lifted",
                    subject="provisional suspension lifted",
                    payload={
                        "case_id": case["id"],
                        "sample_id": sample["id"],
                        "lab_result_id": lab_result["id"],
                    },
                )

        return {
            "lab_result_id": lab_result["id"],
            "sample_id": sample["id"],
            "case_id": case["id"] if case else None,
            "case_action": case_action,
            "status": "matched",
        }

    # ------------------------------------------------------------------
    # 旧记录回填封条号：按运动员 + 采样时刻，唯一才回填，判不出交人工
    # ------------------------------------------------------------------
    def backfill_seals(self, actor=None):
        actor = actor or SYSTEM_ACTOR
        results = []
        samples = [
            sample for sample in self.repository.list_entities(kind="sample")
            if not sample["data"].get("seal_id")
            and sample["status"] not in ("scheduled",)
        ]
        for sample in samples:
            athlete_id = sample["data"].get("athlete_id")
            collected_at = sample["data"].get("collected_at")
            candidates = [
                handover for handover in self._lookup(
                    "seal_handover", "athlete_id", athlete_id
                )
                if handover["data"].get("collected_at") == collected_at
            ]
            if len(candidates) == 1:
                updated = self.transition(
                    actor, sample["id"], "backfill_seal",
                    {"seal_id": candidates[0]["data"]["seal_id"]},
                )
                results.append({"sample_id": sample["id"],
                                "seal_id": updated["data"]["seal_id"],
                                "status": "backfilled"})
            else:
                reason = ("no seal handover on file"
                          if not candidates else "multiple seal candidates")
                review = self._open_review(
                    actor,
                    topic="seal_backfill",
                    reason=reason,
                    payload={
                        "sample_id": sample["id"],
                        "athlete_id": athlete_id,
                        "collected_at": collected_at,
                        "candidate_seal_ids": [c["data"]["seal_id"] for c in candidates],
                    },
                    review_id=_make_id("review", "backfill", sample["id"]),
                )
                results.append({"sample_id": sample["id"], "status": "manual_review",
                                "manual_review_id": review["id"]})
        return {"processed": len(results), "items": results}

    # ------------------------------------------------------------------
    # 人工判读：解决后把结果并入正常链路
    # ------------------------------------------------------------------
    def resolve_manual_review(self, review_id, resolution, actor, sample_id=None, note=None):
        review = self.repository.get_entity(review_id)
        if not review or review["kind"] != "manual_review":
            raise NotFoundError("manual review not found: " + review_id)
        data = {"resolution": resolution, "sample_id": sample_id, "note": note}
        review = self.transition(actor, review_id, "resolve", data)

        if review["data"].get("topic") == "lab_result_match" and resolution == "matched":
            lab_result_id = review["data"].get("lab_result_id")
            lab_result = self.repository.get_entity(lab_result_id)
            sample = self.repository.get_entity(sample_id)
            if not lab_result or not sample:
                raise ValidationError("lab result or sample missing for resolution")
            if lab_result["status"] == "manual_review":
                self.transition(actor, lab_result_id, "resolve_match",
                                {"sample_id": sample_id})
            outcome = self._apply_result(
                actor, self.repository.get_entity(lab_result_id), sample
            )
            return {"review": review, "outcome": outcome}
        return {"review": review, "outcome": None}

    # ------------------------------------------------------------------
    # 通知与人工队列：确定性 ID，重启/重放不重复
    # ------------------------------------------------------------------
    def _notify(self, actor, recipient, channel, subject, payload):
        notification_id = _make_id(
            "note", channel, payload.get("case_id", ""),
            payload.get("lab_result_id", ""), payload.get("action", ""),
        )
        existing = self.repository.get_entity(notification_id)
        if existing:
            return existing
        return self.repository.create_entity(
            notification_id,
            "notification",
            "sent",
            {
                "recipient": recipient,
                "channel": channel,
                "subject": subject,
                "payload": payload,
            },
            actor.user_id,
        )

    def _open_review(self, actor, topic, reason, payload, review_id=None):
        review_id = review_id or _make_id("review", topic, uuid4())
        existing = self.repository.get_entity(review_id)
        if existing:
            return existing
        body = dict(payload)
        body.update({"topic": topic, "reason": reason})
        return self.repository.create_entity(
            review_id, "manual_review", "pending", body, actor.user_id
        )
