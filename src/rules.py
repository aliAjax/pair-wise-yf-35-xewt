from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    if data.get("decision") == "sanction":
        sample = _find_one(lookup, "sample", "id", entity["data"].get("sample_id"))
        if not sample or not sample["data"].get("b_confirmed"):
            raise ValidationError(
                "cannot close case with sanction before B sample confirmation"
            )
    return {"decided_by": actor.user_id}


def _validate_lab_result(actor, data, lookup):
    if data.get("result") not in ("adverse", "negative"):
        raise ValidationError("lab result must be adverse or negative")
    corrected_from = data.get("corrected_from")
    if corrected_from:
        prior = _find_one(lookup, "lab_result", "id", corrected_from)
        if not prior:
            raise ValidationError("corrected_from references an unknown lab result")


def _validate_handover(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("handover requires a sample")
    seal_id = data.get("seal_id", "").strip()
    if not seal_id:
        raise ValidationError("seal_id is required")
    if sample["data"].get("seal_id") and sample["data"]["seal_id"] != seal_id:
        raise ValidationError("seal_id does not match the sample seal")
    if not data.get("from_party") or not data.get("to_party"):
        raise ValidationError("from_party and to_party are required")
    if not data.get("handover_at"):
        raise ValidationError("handover_at is required")


def _validate_record_result(actor, entity, data, lookup):
    result = data.get("result")
    if result not in ("adverse", "negative"):
        raise ValidationError("result must be adverse or negative")
    next_status = "adverse" if result == "adverse" else "cleared"
    patch = {
        "result": result,
        "current_result_id": data.get("result_id"),
        "result_updated_at": data.get("reported_at"),
    }
    if data.get("b_sample"):
        patch["b_confirmed"] = True
    return next_status, patch


def _reconcile_match(actor, entity, data, lookup):
    """Match a lab result to a sample by seal_id, then by collected_at+athlete.

    Returns (next_status, patch). Unmatchable results go to the manual queue.
    """
    rdata = entity["data"]
    seal_id = rdata.get("seal_id")
    collected_at = rdata.get("collected_at")
    athlete_id = rdata.get("athlete_id")
    samples = []
    matched_by = None
    if seal_id:
        samples = [
            item
            for item in (lookup("sample", "seal_id", seal_id) or [])
            if item["data"].get("seal_id") == seal_id
        ]
        matched_by = "seal"
    if not samples and collected_at and athlete_id:
        day = str(collected_at)[:10]
        candidates = lookup("sample", "athlete_id", athlete_id) or []
        samples = [
            item
            for item in candidates
            if str(item["data"].get("collected_at", ""))[:10] == day
        ]
        matched_by = "time"
    if len(samples) == 1:
        sample = samples[0]
        return "matched", {
            "sample_id": sample["id"],
            "matched_by": matched_by,
            "matched_at": _now(),
        }
    if len(samples) == 0:
        if seal_id:
            reason = "no sample found for seal_id %s" % seal_id
        else:
            reason = "no sample found for collected_at/athlete"
    else:
        reason = "multiple samples match the result"
    return "manual", {"manual_reason": reason, "candidate_count": len(samples)}


def _validate_resolve_manual(actor, entity, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample:
        raise ValidationError("resolve requires an existing sample_id")
    return {"sample_id": sample["id"], "matched_by": "manual", "matched_at": _now()}


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CUSTOM_CREATE = {
    "athlete": _validate_athlete,
    "sample": _validate_sample,
    "case": _validate_case,
    "lab_result": _validate_lab_result,
    "handover": _validate_handover,
}
CUSTOM_TRANSITIONS = {
    ("sample", "report_adverse"): _validate_report_adverse,
    ("sample", "record_result"): _validate_record_result,
    ("case", "decide"): _validate_case_decision,
    ("case", "resolve_appeal"): _validate_case_decision,
    ("lab_result", "reconcile"): _reconcile_match,
    ("lab_result", "resolve"): _validate_resolve_manual,
}


class RuleEngine:
    ALIASES = {
        "athletes": "athlete",
        "samples": "sample",
        "cases": "case",
        "lab_results": "lab_result",
        "handovers": "handover",
    }
    INITIAL_STATUS = {
        "athlete": "active",
        "sample": "scheduled",
        "case": "open",
        "lab_result": "pending",
        "handover": "recorded",
    }
    TRANSITIONS = {
        "athlete": {"retire": (("active",), "retired")},
        "sample": {
            "collect": (("scheduled",), "collected"),
            "seal": (("collected",), "sealed"),
            "ship": (("sealed",), "in_transit"),
            "receive": (("in_transit",), "received"),
            "analyze": (("received",), "analyzed"),
            "report_adverse": (("analyzed",), "adverse"),
            "clear": (("analyzed",), "cleared"),
            "record_result": (
                ("received", "analyzed", "adverse", "cleared"),
                "adverse",
            ),
        },
        "case": {
            "provisional_suspend": (("open",), "suspended"),
            "schedule_hearing": (("suspended",), "hearing"),
            "decide": (("hearing",), "closed"),
            "appeal": (("closed",), "appeal"),
            "resolve_appeal": (("appeal",), "closed"),
        },
        "lab_result": {
            "reconcile": (("pending",), "matched"),
            "resolve": (("manual",), "matched"),
        },
        "handover": {
            "confirm": (("recorded",), "confirmed"),
        },
    }
    CREATE_REQUIRED = {
        "athlete": ("name", "discipline"),
        "sample": ("athlete_id", "sample_code", "event"),
        "case": ("athlete_id", "sample_id", "alleged_rule"),
        "lab_result": ("lab_id", "result", "reported_at"),
        "handover": ("sample_id", "seal_id", "from_party", "to_party", "handover_at"),
    }
    ACTION_REQUIRED = {
        ("sample", "collect"): ("collected_at",),
        ("sample", "seal"): ("seal_id",),
        ("sample", "ship"): ("carrier",),
        ("sample", "receive"): ("lab_id",),
        ("sample", "analyze"): ("result",),
        ("sample", "clear"): ("reason",),
        ("sample", "record_result"): ("result",),
        ("case", "provisional_suspend"): ("reason",),
        ("case", "schedule_hearing"): ("hearing_at",),
        ("case", "decide"): ("decision",),
        ("case", "appeal"): ("grounds",),
        ("case", "resolve_appeal"): ("decision",),
        ("lab_result", "resolve"): ("sample_id",),
    }
    CREATE_ROLES = {
        "athlete": ("admin", "panel"),
        "sample": ("admin", "inspector"),
        "case": ("admin", "panel"),
        "lab_result": ("admin", "lab"),
        "handover": ("admin", "inspector"),
    }
    ROLE_ACTIONS = {
        "retire": ("admin", "panel"),
        "collect": ("admin", "inspector"),
        "seal": ("admin", "inspector"),
        "ship": ("admin", "inspector"),
        "receive": ("admin", "lab"),
        "analyze": ("admin", "lab"),
        "report_adverse": ("admin", "lab"),
        "clear": ("admin", "lab"),
        "record_result": ("admin", "lab"),
        "provisional_suspend": ("admin", "panel"),
        "schedule_hearing": ("admin", "panel"),
        "decide": ("admin", "panel"),
        "appeal": ("admin", "panel"),
        "resolve_appeal": ("admin", "panel"),
        "reconcile": ("admin", "lab"),
        "resolve": ("admin", "panel"),
        "confirm": ("admin", "inspector"),
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
        if isinstance(extra, tuple):
            next_status, extra = extra
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
