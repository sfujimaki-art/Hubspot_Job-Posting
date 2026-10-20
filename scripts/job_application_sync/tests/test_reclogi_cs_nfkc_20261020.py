"""担当CS の姓を NFKC でそろえる (2026-10-20)。HubSpot には触らない。"""
from scripts.job_application_sync import reclogi_applicants as rl


class _C:
    def __init__(self, name):
        self.name = name

    def get_listing_consultant(self, listing_id, media, login):
        return "1"

    def get_owner_last_name(self, owner):
        return self.name


def test_radical_ta_becomes_normal():
    assert rl.consultant_last_name(_C("柳⽥"), "L1") == "柳田"


def test_plain_name_unchanged_and_empty_ok():
    assert rl.consultant_last_name(_C(" 横山 "), "L1") == "横山"
    assert rl.consultant_last_name(_C(None), "L1") == ""
