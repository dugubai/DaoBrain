"""Local synthetic-only workflow, not a clinical record ingestion service.

No diagnoses, prescriptions, real consent documents, credential verification or
network publication. The clinician role is a locally provisioned role, not proof
of a licence. Clients cannot submit observation prose, even with synthetic=True.
The shared store bounds all objects to 2000; authorized pagination is applied
before returning anything. No standalone app: the hub must include this router.
"""
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, field_validator, model_validator

from application_hub.security import Input, Principal, Review, roles
from application_hub.store import create, get, listing, transaction, update

router = APIRouter(prefix="/api/clinical", tags=["synthetic-clinical"])
KIND = "clinical_record"
NOTE = ("仅固定完全合成观察的本地流程演示；不采集真实病历，不诊断、不处方；"
        "clinician 为本地账号角色，不代表已核验执业资格。")
# These are invented observations, not de-identified patient data.
_FIXTURES = {
    "SYN-DEMO1": {
        "inspection": "虚构观察甲：合成演示对象的外观占位描述，无真实人物。",
        "listening": "虚构观察甲：声音与气味均为预设占位观察，未采集音频。",
        "inquiry": "虚构观察甲：问询回答为固定合成文本，无身份或联系方式。",
        "palpation": "虚构观察甲：触察为程序预设占位描述，未实施检查。",
    },
    "SYN-DEMO2": {
        "inspection": "虚构观察乙：外观为另一组合成占位描述，无真实人物。",
        "listening": "虚构观察乙：未采集声音或气味，仅演示字段结构。",
        "inquiry": "虚构观察乙：问询来自静态合成夹具，不对应患者。",
        "palpation": "虚构观察乙：未测量脉搏或进行触察，仅演示流程。",
    },
}


class Observations(Input):
    inspection: str = Field(min_length=1, max_length=500)
    listening: str = Field(min_length=1, max_length=500)
    inquiry: str = Field(min_length=1, max_length=500)
    palpation: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def total_bound(self):
        if sum(len(value) for value in self.model_dump().values()) > 1600:
            raise ValueError("合成观察总量不得超过1600字符")
        return self


class ClinicalCreate(Input):
    case_id: str = Field(min_length=5, max_length=32, pattern=r"^SYN-[A-Za-z0-9]+$")
    synthetic: bool
    consent: bool
    clinician_id: str = Field(pattern=r"^[a-f0-9]{32}$")

    @field_validator("case_id")
    @classmethod
    def fixture_only(cls, value):
        if value not in _FIXTURES:
            raise ValueError("仅支持固定合成代号 SYN-DEMO1 / SYN-DEMO2")
        return value

    @field_validator("synthetic", "consent")
    @classmethod
    def must_be_true(cls, value):
        if value is not True:
            raise ValueError("必须明确确认完全合成演示及演示同意")
        return value


class ClinicalReview(Review):
    @field_validator("comment")
    @classmethod
    def code_only(cls, value):
        if value not in ("", "fixture-confirmed", "fixture-rejected"):
            raise ValueError("审核备注仅支持固定代号，禁止病历或个人资料")
        return value


def _visible(item, user):
    return (item["owner"] == user.id or
            (user.role == "clinician" and item["payload"]["clinician_id"] == user.id))


def _permitted(db, record_id, user):
    item = get(db, KIND, record_id)
    if not _visible(item, user):
        # Do not confirm another owner's record exists.
        raise HTTPException(404, "记录不存在")
    return item


@router.post("", status_code=201)
def create_record(req: ClinicalCreate, user: Principal = Depends(roles("editor", "clinician"))):
    with transaction() as db:
        reviewer = db.execute("SELECT id FROM users WHERE id=? AND role='clinician' AND enabled=1",
                              (req.clinician_id,)).fetchone()
        if reviewer is None:
            raise HTTPException(422, "需要有效的独立 clinician 账号")
        if req.clinician_id == user.id:
            raise HTTPException(403, "创建人与 clinician 复核人必须不同")
        observations = Observations(**_FIXTURES[req.case_id]).model_dump()
        payload = {"case_id": req.case_id, "synthetic": True, "consent": True,
                   "clinician_id": req.clinician_id, "observations": observations,
                   "fixture_version": "synthetic-observations-v1", "review": None,
                   "note": NOTE}
        return create(db, KIND, user.id, payload)


@router.get("")
def list_records(offset: int = Query(default=0, ge=0, le=2000),
                 limit: int = Query(default=20, ge=1, le=50),
                 status: str | None = Query(default=None, pattern=r"^(draft|approved|rejected)$"),
                 user: Principal = Depends(roles("editor", "clinician"))):
    with transaction() as db:
        # Bounded local scan, not a production-scale clinical search index.
        items = listing(db, KIND, owner=None if user.role == "clinician" else user.id,
                        limit=2000, status=status)
        permitted = [item for item in items if _visible(item, user)]
        return {"items": permitted[offset:offset + limit], "total": len(permitted), "note": NOTE}


@router.post("/real")
def real_record_entry(user: Principal = Depends(roles("editor", "clinician"))):
    # Deliberately do not parse or persist a body at this entry.
    raise HTTPException(501, "真实病历入口未实现且禁止接收；仅可使用固定完全合成夹具")


@router.get("/{record_id}")
def get_record(record_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
               user: Principal = Depends(roles("editor", "clinician"))):
    with transaction() as db:
        return _permitted(db, record_id, user)


@router.post("/{record_id}/review")
def review_record(req: ClinicalReview, record_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
                  user: Principal = Depends(roles("clinician"))):
    with transaction() as db:
        item = get(db, KIND, record_id)
        if item["owner"] == user.id:
            raise HTTPException(403, "禁止自审")
        if item["payload"]["clinician_id"] != user.id:
            raise HTTPException(404, "记录不存在")
        if item["status"] != "draft":
            raise HTTPException(409, "记录已审核；本原型不支持修改后重新送审")
        payload = dict(item["payload"])
        # No free-form review prose can become a covert real-record entry.
        payload["review"] = {"reviewer_id": user.id, "decision": req.decision,
                             "comment": req.comment}
        return update(db, item, req.expected_version, payload, req.decision,
                      user.id, KIND + ".review." + req.decision)
