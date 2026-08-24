from dataclasses import replace
from datetime import datetime, timezone

from goldbook.config import Settings
from goldbook.fact_check import (
    BranchDecision,
    BranchPredicate,
    BranchStatus,
    FactCheckImpact,
    FactCheckResult,
    FactValue,
    SearchEvidence,
)
from goldbook.web import create_app


pytest_plugins = ("tests.test_claim_web",)


NOW = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)


class _FactCheckQueue:
    def __init__(self):
        self.calls = []

    def enqueue_fact_check(self, bvid):
        self.calls.append(bvid)
        return object()


def _make_conditional(database, video):
    claim = replace(
        database.list_forecast_claims(video.bvid)[0],
        condition_text=(
            "若今晚CPI利好，金价先突破4400美元，再上探4450美元和4500美元"
        ),
    )
    database.replace_forecast_claims(video.bvid, 1, (claim,))
    return claim


def test_fact_check_post_only_enqueues_and_redirects(claim_web, tmp_path):
    _client, database, creator, video = claim_web
    _make_conditional(database, video)
    pipeline = _FactCheckQueue()
    app = create_app(
        Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)}), database, pipeline
    )
    app.config.update(TESTING=True)

    response = app.test_client().post(
        f"/creators/{creator.uid}/videos/{video.bvid}/fact-check",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert pipeline.calls == [video.bvid]
    assert response.headers["Location"].endswith(
        f"/creators/{creator.uid}/videos/{video.bvid}"
    )


def test_video_page_explains_fact_values_branches_and_cited_sources(
    claim_web, tmp_path
):
    _client, database, creator, video = claim_web
    claim = _make_conditional(database, video)
    run = database.create_fact_check_run(
        video.bvid, 1, "今晚CPI数据", "MiniMax-M3", created_at=NOW
    )
    evidence = (
        SearchEvidence(
            "e1", "2026 CPI actual forecast", "CPI release", "https://one.example/cpi",
            "one.example", NOW, "Headline CPI rose 0.2%; consensus was 0.3%.", NOW,
        ),
        SearchEvidence(
            "e2", "2026 CPI actual forecast", "Market reaction", "https://two.example/market",
            "two.example", NOW, "Core CPI was also below forecast.", NOW,
        ),
    )
    database.save_fact_check_evidence(run.run_id, evidence)
    database.save_fact_check_result(
        run.run_id,
        FactCheckResult(
            "CPI是否利好黄金？", "美国CPI公布", NOW,
            (FactValue("总体CPI环比", "0.2", "0.3", "0.3", "%"),),
            FactCheckImpact.SUPPORTIVE,
            "实际值低于市场预期，通常利好黄金。",
            ("e1", "e2"),
            (
                BranchDecision(
                    claim.claim_id, BranchPredicate.SUPPORTIVE,
                    BranchStatus.TRIGGERED, "CPI低于预期，利好分支已触发。",
                ),
            ),
            0.91,
        ),
        search_count=2,
        completed_at=NOW,
    )
    app = create_app(
        Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)}), database, _FactCheckQueue()
    )
    app.config.update(TESTING=True)

    response = app.test_client().get(
        f"/creators/{creator.uid}/videos/{video.bvid}"
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "联网事实核查" in body
    assert "实际值" in body and "0.2" in body
    assert "市场预期" in body and "0.3" in body
    assert "条件已触发" in body
    assert "CPI低于预期" in body
    assert 'href="https://one.example/cpi"' in body
    assert 'rel="noopener noreferrer"' in body
    assert "自动事实核查" in body and "不构成投资建议" in body


def test_not_triggered_branch_is_explicitly_not_a_miss(claim_web, tmp_path):
    _client, database, creator, video = claim_web
    claim = _make_conditional(database, video)
    run = database.create_fact_check_run(video.bvid, 1, "今晚CPI数据", "MiniMax-M3")
    evidence = (
        SearchEvidence("e1", "q", "one", "https://one.example/a", "one.example", NOW, "a", NOW),
        SearchEvidence("e2", "q", "two", "https://two.example/a", "two.example", NOW, "b", NOW),
    )
    database.save_fact_check_evidence(run.run_id, evidence)
    database.save_fact_check_result(
        run.run_id,
        FactCheckResult(
            "q", "CPI", NOW, (FactValue("CPI", "0.4", "0.3", "0.3", "%"),),
            FactCheckImpact.ADVERSE, "高于预期。", ("e1", "e2"),
            (BranchDecision(claim.claim_id, BranchPredicate.SUPPORTIVE, BranchStatus.NOT_TRIGGERED, "利好条件未成立。"),),
            0.8,
        ), search_count=2,
    )
    app = create_app(Settings.from_env({"GOLDBOOK_DATA_DIR": str(tmp_path)}), database, _FactCheckQueue())
    app.config.update(TESTING=True)

    body = app.test_client().get(
        f"/creators/{creator.uid}/videos/{video.bvid}"
    ).get_data(as_text=True)

    assert "条件未触发（不计为未命中）" in body
    assert "条件未成立，不进入价格命中率" in body
