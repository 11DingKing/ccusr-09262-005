"""观测来源谱系：查询返回来源链与版本号；链只追加、可撤回、重启后仍可追溯。"""
from __future__ import annotations

from service_09252_010.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from support import INST_A, INST_B, PROJECT, SUPERVISOR, RigTestCase, TARGET

KEY = {"measure": "enrollment_count", "period": "2024-01",
        "caliber": "DE-DUAL"}


def rec(value, evidence_id: str, source: str, retract: bool = False) -> dict:
    return {"measure": KEY["measure"], "period": KEY["period"],
            "caliber": KEY["caliber"], "value": value,
            "evidence_id": evidence_id, "source": source,
            "retract": retract}


class LineageServiceTests(RigTestCase):
    def test_chain_records_every_source_with_version_numbers(self) -> None:
        ev = self.seed_evidence()
        self.seed_import([rec(10, ev, "manual")], ev, reason="首批人工填报")
        self.seed_import([rec(12, ev, "partner_api")], ev,
                         reason="合作方接口同步")
        self.seed_import([rec(11, ev, "migration")], ev,
                         reason="历史迁移补录")

        result = self.rig.imports.lineage(SUPERVISOR, PROJECT, **KEY)
        self.assertEqual(result["current_version_no"], 3)
        self.assertEqual(result["current_value"], 11.0)
        self.assertFalse(result["retracted"])
        chain = result["chain"]
        self.assertEqual(len(chain), 3)
        self.assertEqual(
            [(h["version_no"], h["source"], h["source_label"], h["value"])
             for h in chain],
            [(1, "manual", "人工填报", 10.0),
             (2, "partner_api", "合作方接口", 12.0),
             (3, "migration", "历史迁移", 11.0)],
        )
        self.assertEqual([h["retracted"] for h in chain],
                         [False, False, False])
        self.assertEqual(chain[0]["batch_reason"], "首批人工填报")
        # 每跳携带证据明细而不只是 evidence_id
        self.assertEqual(chain[0]["evidence"]["sha256"], "f" * 64)
        self.assertEqual(chain[0]["evidence"]["uri"], "s3://evidence/a.pdf")

    def test_chain_defaults_to_manual_and_ends_with_retraction(self) -> None:
        ev = self.seed_evidence()
        # 省略 source 时按人工填报处理（向后兼容）
        self.seed_import(
            [{"measure": KEY["measure"], "period": KEY["period"],
              "caliber": KEY["caliber"], "value": 10,
              "evidence_id": ev}], ev
        )
        self.seed_import([rec(None, ev, "manual", retract=True)], ev,
                        reason="撤回误报")

        result = self.rig.imports.lineage(SUPERVISOR, PROJECT, **KEY)
        self.assertEqual(result["current_version_no"], 2)
        self.assertIsNone(result["current_value"])
        self.assertTrue(result["retracted"])
        self.assertEqual(
            [(h["version_no"], h["source"], h["value"], h["retracted"])
             for h in result["chain"]],
            [(1, "manual", 10.0, False),
             (2, "manual", None, True)],
        )

    def test_unknown_natural_key_is_404(self) -> None:
        ev = self.seed_evidence()
        self.seed_import([rec(10, ev, "manual")], ev)
        with self.assertRaises(NotFoundError):
            self.rig.imports.lineage(
                SUPERVISOR, PROJECT, measure="enrollment_count",
                period="2024-02", caliber=KEY["caliber"],
            )

    def test_bad_source_rejected(self) -> None:
        ev = self.seed_evidence()
        with self.assertRaises(ValidationError):
            self.seed_import([rec(10, ev, "carrier_pigeon")], ev)

    def test_lineage_requires_view_grant(self) -> None:
        ev = self.seed_evidence()
        self.seed_import([rec(10, ev, "manual")], ev)
        with self.assertRaises(PermissionDeniedError):
            self.rig.imports.lineage(INST_B, PROJECT, **KEY)
        self.rig.grant(INST_A.institution_id, permission="view")
        result = self.rig.imports.lineage(INST_A, PROJECT, **KEY)
        self.assertEqual(result["chain"][0]["source"], "manual")

    def test_lineage_survives_database_reopen(self) -> None:
        """关闭并重开 SQLite（模拟服务重启）后，同一条谱系仍可追溯。"""
        ev = self.seed_evidence()
        self.seed_import([rec(10, ev, "manual")], ev, reason="首批人工填报")
        self.seed_import([rec(12, ev, "partner_api")], ev,
                         reason="合作方接口同步")
        before = self.rig.imports.lineage(SUPERVISOR, PROJECT, **KEY)

        # 重新装配服务，指向同一数据库文件，内存中不保留任何状态
        from support import FixedClock, Rig

        restarted = Rig(self._tmp.name)
        after = restarted.imports.lineage(SUPERVISOR, PROJECT, **KEY)
        self.assertEqual(after, before)
        self.assertEqual(
            [(h["version_no"], h["source"], h["value"])
             for h in after["chain"]],
            [(1, "manual", 10.0), (2, "partner_api", 12.0)],
        )
