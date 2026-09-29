"""观测来源谱系：来源链查询、版本号固化、SQLite 重启可追溯、旧库迁移。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from service_09252_010.domain.errors import NotFoundError, ValidationError
from service_09252_010.persistence.database import Database
from service_09252_010.persistence.store import Store
from support import (
    INST_A,
    PROJECT,
    SUPERVISOR,
    RigTestCase,
    TARGET,
    FixedClock,
    SeqIds,
)
from service_09252_010.services.imports import ImportService


def chain_record(measure: str, period: str, value, evidence_id: str, *,
                 origin_kind: str = "manual", origin_ref: str = "") -> dict:
    return {"measure": measure, "period": period, "caliber": TARGET,
            "value": value, "evidence_id": evidence_id,
            "origin_kind": origin_kind, "origin_ref": origin_ref}


class ProvenanceTests(RigTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ev = self.seed_evidence()
        self.rig.grant(INST_A.institution_id,
                       permission="import")
        self.rig.grant(INST_A.institution_id, permission="view")

    def _import(self, value, kind, ref, reason):
        return self.rig.imports.import_batch(
            INST_A, PROJECT,
            records=[chain_record("enrollment_count", "2024-01", value, self.ev,
                                  origin_kind=kind, origin_ref=ref)],
            reason=reason,
        )["version_no"]

    def test_chain_follows_origins_with_versions(self) -> None:
        v1 = self._import(100, "manual", "张老师", "人工填报")
        v2 = self._import(105, "partner_api", "api://partner", "合作方同步")
        v3 = self._import(108, "historical_migration", "legacy-batch", "历史迁移")
        self.assertEqual([v1, v2, v3], [1, 2, 3])

        result = self.rig.imports.provenance(
            INST_A, PROJECT, measure="enrollment_count",
            period="2024-01", caliber=TARGET,
        )
        self.assertEqual(result["version_no"], 3)
        self.assertEqual(result["latest_version_no"], 3)
        self.assertEqual(result["versions"], [3, 2, 1])
        kinds = [node["origin"]["kind"] for node in result["chain"]]
        self.assertEqual(
            kinds, ["historical_migration", "partner_api", "manual"]
        )
        self.assertEqual(
            [node["relation_from_parent"] for node in result["chain"]],
            ["migrated_from", "partner_sync", None],
        )
        # 不只返回最终数值：每跳数值与版本号都在链上
        self.assertEqual(
            [(node["version_no"], node["value"]) for node in result["chain"]],
            [(3, 108.0), (2, 105.0), (1, 100.0)],
        )

    def test_chain_pinned_to_historical_version(self) -> None:
        self._import(100, "manual", "张老师", "人工填报")
        self._import(105, "partner_api", "api://partner", "合作方同步")
        self._import(108, "historical_migration", "legacy-batch", "历史迁移")

        pinned = self.rig.imports.provenance(
            INST_A, PROJECT, measure="enrollment_count",
            period="2024-01", caliber=TARGET, version_no=2,
        )
        self.assertEqual(pinned["version_no"], 2)
        self.assertEqual(pinned["versions"], [2, 1])
        self.assertEqual(
            [node["origin"]["kind"] for node in pinned["chain"]],
            ["partner_api", "manual"],
        )

    def test_chain_survives_database_reopen(self) -> None:
        """重新打开同一 SQLite 文件（模拟服务重启）返回同一条谱系。"""
        self._import(100, "manual", "张老师", "人工填报")
        self._import(105, "partner_api", "api://partner", "合作方同步")

        db_path = self.rig.db.path
        before = self.rig.imports.provenance(
            INST_A, PROJECT, measure="enrollment_count",
            period="2024-01", caliber=TARGET,
        )

        reopened_db = Database(db_path)
        reopened = ImportService(reopened_db, FixedClock(), SeqIds())
        after = reopened.provenance(
            INST_A, PROJECT, measure="enrollment_count",
            period="2024-01", caliber=TARGET,
        )
        self.assertEqual(after, before)
        self.assertEqual(
            [node["origin"]["ref"] for node in after["chain"]],
            ["api://partner", "张老师"],
        )

    def test_missing_key_and_version_404(self) -> None:
        self._import(100, "manual", "张老师", "人工填报")
        with self.assertRaises(NotFoundError):
            self.rig.imports.provenance(
                INST_A, PROJECT, measure="other_count",
                period="2024-01", caliber=TARGET,
            )
        with self.assertRaises(NotFoundError):
            self.rig.imports.provenance(
                INST_A, PROJECT, measure="enrollment_count",
                period="2024-01", caliber=TARGET, version_no=9,
            )

    def test_bad_origin_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.rig.imports.import_batch(
                INST_A, PROJECT,
                records=[chain_record("enrollment_count", "2024-01", 1,
                                      self.ev, origin_kind="telephone")],
                reason="非法来源",
            )

    def test_retraction_breaks_effective_chain(self) -> None:
        self._import(100, "manual", "张老师", "人工填报")
        self._import(105, "partner_api", "api://partner", "合作方同步")
        self.rig.imports.import_batch(
            INST_A, PROJECT,
            records=[{"measure": "enrollment_count", "period": "2024-01",
                      "caliber": TARGET, "evidence_id": self.ev,
                      "retract": True}],
            reason="撤回误报",
        )
        # 最新版本下该键已撤回：无有效观测
        with self.assertRaises(NotFoundError):
            self.rig.imports.provenance(
                INST_A, PROJECT, measure="enrollment_count",
                period="2024-01", caliber=TARGET,
            )
        # 撤回前的版本 v2 仍可回溯
        pinned = self.rig.imports.provenance(
            INST_A, PROJECT, measure="enrollment_count",
            period="2024-01", caliber=TARGET, version_no=2,
        )
        self.assertEqual(pinned["versions"], [2, 1])


class ProvenanceMigrationTests(unittest.TestCase):
    def test_legacy_database_without_origin_columns_opens(self) -> None:
        """旧库（无 origin 列、无 provenance_links 表）可平滑打开并补列。"""
        tmp = tempfile.TemporaryDirectory(prefix="svc09252-legacy-")
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "legacy.db")

        # 手工构造一张仅含旧结构的库
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE import_batches (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, seq INTEGER NOT NULL,
                reason TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (project_id, seq)
            );
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL, project_id TEXT NOT NULL,
                measure TEXT NOT NULL, period TEXT NOT NULL, caliber TEXT NOT NULL,
                value REAL, retracted INTEGER NOT NULL DEFAULT 0,
                evidence_id TEXT NOT NULL, institution_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        legacy.execute(
            "INSERT INTO import_batches VALUES ('b1', 'P9', 1, '旧库', 'o', 't')"
        )
        legacy.execute(
            "INSERT INTO observations (batch_id, project_id, measure, period,"
            " caliber, value, retracted, evidence_id, institution_id, created_at)"
            " VALUES ('b1', 'P9', 'm', '2024-01', 'CN-STD', 7, 0, 'ev1', 'o', 't')"
        )
        legacy.commit()
        legacy.close()

        # 重新走标准连接：建表脚本补齐缺失表，迁移补 origin 列
        db = Database(db_path)
        with db.read() as conn:
            chain = Store(conn).provenance_chain("P9", "m", "2024-01", "CN-STD")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].value, 7.0)
        self.assertEqual(chain[0].origin_kind.value, "manual")
        self.assertEqual(chain[0].origin_ref, "")
        self.assertIsNone(chain[0].relation_from_parent)
        self.assertEqual(chain[0].version_no, 1)
