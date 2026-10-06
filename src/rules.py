from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 结论取值：adverse（阳性）/ cleared（阴性）/ atypical（非典型，待查）
RESULT_VALUES = ("adverse", "cleared", "atypical")
# 样本类型：A（主样）/ B（留样）
ALIQUOTS = ("A", "B")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")
    # 赛外检查在无信号现场采集：允许创建时直接带封条号与采样时刻，
    # 但必须能在本地找到对应的封条交接记录。
    seal_id = data.get("seal_id")
    if seal_id:
        handover = _find_one(lookup, "seal_handover", "seal_id", seal_id)
        if not handover:
            raise ValidationError("seal_id has no chain-of-custody record")
        if not data.get("collected_at"):
            raise ValidationError("collected_at is required with seal_id")


def _validate_seal_handover(actor, data, lookup):
    if not data.get("seal_id", "").strip():
        raise ValidationError("seal_id is required")
    if not data.get("collected_at"):
        raise ValidationError("collected_at is required")
    if _find_one(lookup, "seal_handover", "seal_id", data["seal_id"]):
        raise ConflictError("seal_id already registered: " + data["seal_id"])


def _validate_lab_result(actor, data, lookup):
    if not data.get("report_no", "").strip():
        raise ValidationError("report_no is required")
    if not data.get("seal_id", "").strip():
        raise ValidationError("seal_id is required")
    if not data.get("collected_at"):
        raise ValidationError("collected_at is required")
    if data.get("result") not in RESULT_VALUES:
        raise ValidationError("result must be one of " + ",".join(RESULT_VALUES))
    if data.get("aliquot") not in ALIQUOTS:
        raise ValidationError("aliquot must be A or B")
    revision = data.get("revision", 0)
    if not isinstance(revision, int) or revision < 1:
        raise ValidationError("revision must be a positive integer")
    existing = _find_one(
        lookup, "lab_result", "report_revision",
        "%s#%s" % (data["report_no"], revision),
    )
    if existing:
        raise ConflictError(
            "lab result already recorded: %s#%s" % (data["report_no"], revision)
        )


def _validate_case(actor, data, lookup):
    # 案件在对账到阳性结果后自动立案，也允许人工就已匹配的阳性结果立案；
    # 不再要求样本实体本身先转到 adverse——样本链与案件链各走各的状态。
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("case requires a sample")
    result = None
    result_id = data.get("origin_result_id")
    if result_id:
        result = _find_one(lookup, "lab_result", "id", result_id)
    if not result:
        result = _latest_matched_result(lookup, sample)
    if not result or result["data"].get("result") != "adverse":
        raise ValidationError("case requires a matched adverse lab result")


def _validate_sample_ship(actor, entity, data, lookup):
    handover = _find_one(lookup, "seal_handover", "seal_id", entity["data"].get("seal_id"))
    if not handover:
        raise ValidationError("cannot ship without a chain-of-custody seal record")
    if handover["status"] not in ("handed_over", "received"):
        raise InvalidTransition("seal %s is not handed over" % handover["data"]["seal_id"])
    return {}


def _validate_sample_reconcile(actor, entity, data, lookup):
    if data.get("result") not in RESULT_VALUES:
        raise ValidationError("result must be one of " + ",".join(RESULT_VALUES))
    if not data.get("result_id"):
        raise ValidationError("result_id is required")
    patch = {
        "result": data["result"],
        "last_result_id": data["result_id"],
        "result_revision": int(data.get("revision", 1)),
        "substance": data.get("substance", ""),
    }
    if data.get("seal_id"):
        patch["seal_id"] = data["seal_id"]
    if data.get("seal_backfilled"):
        patch["seal_backfilled"] = True
    return patch


def _validate_sample_backfill(actor, entity, data, lookup):
    seal_id = data.get("seal_id")
    if not seal_id:
        raise ValidationError("seal_id is required")
    if entity["data"].get("seal_id"):
        raise ConflictError("sample already carries seal_id")
    handover = _find_one(lookup, "seal_handover", "seal_id", seal_id)
    if not handover:
        raise ValidationError("seal_id has no chain-of-custody record")
    return {"seal_id": seal_id, "seal_backfilled": True, "backfilled_by": actor.user_id}


def _validate_lab_match(actor, entity, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("sample not found for match")
    return {
        "sample_id": sample["id"],
        "athlete_id": sample["data"].get("athlete_id"),
        "matched_by": actor.user_id,
    }


def _validate_case_reconfirm(actor, entity, data, lookup):
    result = _find_one(lookup, "lab_result", "id", data.get("result_id"))
    if not result or result["status"] not in ("matched", "superseded"):
        raise ValidationError("reconfirm needs a matched lab result")
    return {
        "reconfirm_result_id": result["id"],
        "reconfirm_revision": result["data"].get("revision", 1),
        "reconfirm_result": result["data"].get("result"),
        "reconfirmed_by": actor.user_id,
    }


def _validate_case_lift(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("reason is required")
    patch = {"lifted_by": actor.user_id}
    result_id = data.get("result_id")
    if result_id:
        result = _find_one(lookup, "lab_result", "id", result_id)
        if result:
            patch.update({
                "lift_result_id": result["id"],
                "lift_result": result["data"].get("result"),
            })
    return patch


def _validate_case_decide(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    patch = {"decided_by": actor.user_id}
    if data["decision"] == "sanction":
        # B 样确认前不能直接结案。
        sample = _find_one(lookup, "sample", "id", entity["data"].get("sample_id"))
        b_sample = None
        if sample:
            b_sample = _latest_matched_result(
                lookup, sample, aliquot="B", adverse_only=True
            )
        if not b_sample:
            raise InvalidTransition("cannot close: B sample confirmation is missing")
        patch["b_result_id"] = b_sample["id"]
    return patch


def _validate_review_resolve(actor, entity, data, lookup):
    resolution = data.get("resolution")
    if resolution not in ("matched", "rejected"):
        raise ValidationError("resolution must be matched or rejected")
    patch = {"resolution": resolution, "resolved_by": actor.user_id}
    if resolution == "matched":
        if not data.get("sample_id"):
            raise ValidationError("sample_id is required to resolve a match")
        sample = _find_one(lookup, "sample", "id", data["sample_id"])
        if not sample:
            raise ValidationError("sample not found for resolution")
        patch["sample_id"] = sample["id"]
    if data.get("note"):
        patch["resolution_note"] = data["note"]
    return patch


def _latest_matched_result(lookup, sample, aliquot=None, adverse_only=False):
    """取样本当前命中的最新修订结果（revision 最大）。"""
    rows = lookup("lab_result", "sample_id", sample["id"]) if lookup else []
    candidates = [
        row for row in rows
        if row["status"] in ("matched", "superseded")
        and (aliquot is None or row["data"].get("aliquot") == aliquot)
        and (not adverse_only or row["data"].get("result") == "adverse")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row["data"].get("revision", 1)))


CUSTOM_CREATE = {
    'athlete': _validate_athlete,
    'sample': _validate_sample,
    'seal_handover': _validate_seal_handover,
    'lab_result': _validate_lab_result,
    'case': _validate_case,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): lambda actor, entity, data, lookup: {"confirmed_by": actor.user_id},
    ('sample', 'ship'): _validate_sample_ship,
    ('sample', 'reconcile_findings'): _validate_sample_reconcile,
    ('sample', 'backfill_seal'): _validate_sample_backfill,
    ('case', 'decide'): _validate_case_decide,
    ('case', 'resolve_appeal'): _validate_case_decide,
    ('case', 'reconfirm_suspension'): _validate_case_reconfirm,
    ('case', 'lift_suspension'): _validate_case_lift,
    ('lab_result', 'match'): _validate_lab_match,
    ('manual_review', 'resolve'): _validate_review_resolve,
}

# 允许停留在同一状态的动作（原地补数据）。
SELF_LOOP_ACTIONS = {('sample', 'backfill_seal')}


class RuleEngine:
    ALIASES = {
        'athletes': 'athlete',
        'samples': 'sample',
        'seal_handovers': 'seal_handover',
        'lab_results': 'lab_result',
        'cases': 'case',
        'notifications': 'notification',
        'manual_reviews': 'manual_review',
    }
    INITIAL_STATUS = {
        'athlete': 'active',
        'sample': 'scheduled',
        'seal_handover': 'recorded',
        'lab_result': 'recorded',
        'case': 'open',
        'notification': 'sent',
        'manual_review': 'pending',
    }
    TRANSITIONS = {
        'athlete': {
            'retire': (('active',), 'retired'),
        },
        'sample': {
            # 离线采集：创建样本时可直接落在 collected（见 INITIAL 数据），
            # scheduled -> collected 仍是常规入口。
            'collect': (('scheduled',), 'collected'),
            'seal': (('collected',), 'sealed'),
            'ship': (('sealed', 'collected'), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'analyze': (('received',), 'analyzed'),
            'report_adverse': (('analyzed',), 'adverse'),
            'clear': (('analyzed',), 'cleared'),
            # 对账按封条号+采样时刻命中实验室结果后写回样本链，
            # 样本状态随本次结论变化（adverse/cleared/atypical）。
            'reconcile_findings': (
                ('collected', 'sealed', 'in_transit', 'received', 'analyzed',
                 'adverse', 'cleared', 'atypical'),
                '__RESULT__',
            ),
            # 旧记录回填封条号：状态不变，只补数据。
            'backfill_seal': (
                ('collected', 'sealed', 'in_transit', 'received',
                 'analyzed', 'adverse', 'cleared', 'atypical'),
                '__KEEP__',
            ),
        },
        'seal_handover': {
            'hand_over': (('recorded',), 'handed_over'),
            'confirm_receipt': (('handed_over',), 'received'),
        },
        'lab_result': {
            'match': (('recorded',), 'matched'),
            'flag_review': (('recorded',), 'manual_review'),
            'supersede': (('matched',), 'superseded'),
            'resolve_match': (('manual_review',), 'matched'),
            'dismiss': (('manual_review',), 'dismissed'),
        },
        'case': {
            'provisional_suspend': (('open', 'reopened'), 'suspended'),
            # 结果更新后重新确认临时禁赛：听证准备中的案件也退回停赛确认态。
            'reconfirm_suspension': (('suspended', 'hearing'), 'suspended'),
            'schedule_hearing': (('suspended',), 'hearing'),
            'lift_suspension': (('suspended', 'hearing'), 'reopened'),
            'decide': (('hearing',), 'closed'),
            'appeal': (('closed',), 'appeal'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
        'manual_review': {
            'resolve': (('pending',), 'resolved'),
        },
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'seal_handover': ('seal_id', 'collected_at'),
        'lab_result': ('seal_id', 'collected_at', 'result', 'aliquot'),
        'case': ('sample_id', 'alleged_rule'),
        'notification': ('recipient', 'channel', 'subject'),
        'manual_review': ('topic', 'reason'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('sample', 'reconcile_findings'): ('result', 'result_id'),
        ('sample', 'backfill_seal'): ('seal_id',),
        ('seal_handover', 'hand_over'): ('carrier',),
        ('seal_handover', 'confirm_receipt'): ('lab_id',),
        ('lab_result', 'match'): ('sample_id',),
        ('lab_result', 'flag_review'): ('reason',),
        ('lab_result', 'resolve_match'): ('sample_id',),
        ('lab_result', 'dismiss'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'reconfirm_suspension'): ('result_id',),
        ('case', 'lift_suspension'): ('reason',),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
        ('manual_review', 'resolve'): ('resolution',),
    }
    CREATE_ROLES = {
        'athlete': ('admin', 'panel'),
        'sample': ('admin', 'inspector'),
        'seal_handover': ('admin', 'inspector'),
        'lab_result': ('admin', 'lab'),
        'case': ('admin', 'panel', 'system'),
        'notification': ('admin', 'panel', 'system'),
        'manual_review': ('admin', 'panel', 'inspector', 'lab', 'system'),
    }
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'reconcile_findings': ('admin', 'lab', 'panel', 'system'),
        'backfill_seal': ('admin', 'inspector', 'panel', 'system'),
        'hand_over': ('admin', 'inspector'),
        'confirm_receipt': ('admin', 'lab'),
        'match': ('admin', 'lab', 'panel', 'system'),
        'flag_review': ('admin', 'lab', 'panel', 'system'),
        'supersede': ('admin', 'lab', 'panel', 'system'),
        'resolve_match': ('admin', 'lab', 'panel'),
        'dismiss': ('admin', 'lab', 'panel'),
        'provisional_suspend': ('admin', 'panel', 'system'),
        'reconfirm_suspension': ('admin', 'panel', 'system'),
        'lift_suspension': ('admin', 'panel', 'system'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
        'resolve': ('admin', 'panel', 'inspector', 'lab'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        if next_status == "__KEEP__":
            next_status = entity["status"]
        elif next_status == "__RESULT__":
            next_status = patch["result"]
        return next_status, patch
