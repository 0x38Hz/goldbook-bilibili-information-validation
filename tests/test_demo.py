from goldbook.db import Database
from goldbook.models import EvaluationVerdict
from goldbook.scoring import aggregate_creator


def test_demo_seed_creates_ranked_and_insufficient_sample_creators(tmp_path):
    from scripts.seed_demo import seed_demo

    db = Database(tmp_path / "demo.db")
    db.initialize()
    seed_demo(db)

    metrics = [aggregate_creator(db.list_creator_outcomes(creator.uid)) for creator in db.list_creators()]

    assert any(item.eligible_for_rank for item in metrics)
    assert any(not item.eligible_for_rank for item in metrics)


def test_demo_seed_is_idempotent_and_has_positive_negative_and_excluded_examples(tmp_path):
    from scripts.seed_demo import seed_demo

    db = Database(tmp_path / "demo.db")
    db.initialize()
    seed_demo(db)
    seed_demo(db)

    analyses = [
        db.get_latest_analysis(video.bvid)
        for creator in db.list_creators()
        for video in db.list_videos(creator.uid)
    ]
    returns = [
        outcome.return_5d
        for creator in db.list_creators()
        for outcome in db.list_creator_outcomes(creator.uid)
    ]

    assert len(db.list_creators()) == 2
    assert sum(len(db.list_videos(creator.uid)) for creator in db.list_creators()) == 6
    assert len(db.list_prices()) == 30
    assert any(value is not None and value > 0 for value in returns)
    assert any(value is not None and value < 0 for value in returns)
    assert any(analysis is not None and analysis.review_status.value == "excluded" for analysis in analyses)


def test_demo_contains_hit_near_miss_and_unresolved_claims(tmp_path):
    from scripts.seed_demo import seed_demo

    db = Database(tmp_path / "demo.db")
    db.initialize()
    seed_demo(db)
    seed_demo(db)

    verdicts = {
        evaluation.verdict
        for creator in db.list_creators()
        for evaluation in db.list_creator_claim_evaluations(creator.uid)
    }

    assert {
        EvaluationVerdict.HIT,
        EvaluationVerdict.PARTIAL_NEAR,
        EvaluationVerdict.MISS,
        EvaluationVerdict.UNRESOLVED,
    } <= verdicts
