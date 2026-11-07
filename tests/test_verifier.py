from datetime import datetime, timedelta, timezone

import pytest

from paperread.models import StatsPair
from paperread.verifier import compare


def pair(**updates):
    return StatsPair.model_validate({"book_id":"A","account_id":"test","book_seconds":100,"period_seconds":1000,
        "period_key":"monthly:1","source":"official","identity_hash":"hash",**updates})


@pytest.mark.parametrize("book_delta,total_delta,code",[(0,0,"NO_REMOTE_CHANGE"),(0,60,"ATTRIBUTION_UNPROVEN"),
    (60,120,"ATTRIBUTION_UNPROVEN"),(61,61,"DELTA_EXCEEDS_BUDGET"),(-1,-1,"COUNTER_DECREASED")])
def test_bad_remote_deltas_cannot_verify(book_delta,total_delta,code):
    before=pair()
    after=pair(book_seconds=100+book_delta,period_seconds=1000+total_delta,
               captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    result=compare(before,after,requested_seconds=60,elapsed_seconds=60)
    assert result.code == code and result.attributable_seconds == 0


@pytest.mark.parametrize("key,value",[("book_id","B"),("account_id","other"),("identity_hash","different"),
                                     ("period_key","monthly:2"),("source","mock")])
def test_identity_and_period_changes_are_not_attributed(key,value):
    before=pair()
    after=pair(book_seconds=160,period_seconds=1060,**{key:value})
    assert compare(before,after,requested_seconds=60,elapsed_seconds=60).attributable_seconds == 0


def test_true_partial_and_complete_deltas_are_capped_to_real_elapsed():
    before=pair()
    after=pair(book_seconds=130,period_seconds=1030,
               captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    assert compare(before,after,requested_seconds=60,elapsed_seconds=60).attributable_seconds == 30
    assert compare(before,after,requested_seconds=60,elapsed_seconds=29).attributable_seconds == 0


def test_app_minute_precision_does_not_relax_server_budget_or_attribution():
    from paperread.sync_service import SyncService
    baseline=pair(book_seconds=0,period_seconds=11409)
    assert SyncService._client_matches(baseline,0,11400,60)
    assert not SyncService._client_matches(baseline,0,11400,1)
    assert not SyncService._client_matches(baseline,0,11460,60)
    after=pair(book_seconds=61,period_seconds=11470,
               captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    assert SyncService._client_matches(after,60,11460,60)
    assert compare(baseline,after,requested_seconds=60,elapsed_seconds=60).code=="DELTA_EXCEEDS_BUDGET"


def test_legacy_counter_cannot_be_compared_to_corrected_reading_time_even_if_delta_looks_valid():
    before=pair(book_seconds=0,evidence={"book":{"recordReadingTime":0}})
    after=pair(book_seconds=60,period_seconds=1060,
        evidence={"book_time_field":"book.readingTime","book":{"readingTime":60}})
    result=compare(before,after,requested_seconds=60,elapsed_seconds=60)
    assert result.code=="MEASUREMENT_CHANGED" and result.attributable_seconds==0


def test_correct_ordinary_reading_metric_can_verify_exact_sixty_second_increase():
    evidence={"book_time_field":"book.readingTime"}
    before=pair(book_seconds=13515,evidence=evidence)
    after=pair(book_seconds=13575,period_seconds=1060,evidence=evidence,
        captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    result=compare(before,after,requested_seconds=60,elapsed_seconds=60)
    assert result.complete and result.attributable_seconds==60


@pytest.mark.parametrize("book_delta,period_delta",[(64,64),(58,60),(60,62),(55,55),(65,65)])
def test_explicit_tolerance_accepts_small_offsets_and_caps_credit_to_approval(book_delta,period_delta):
    before=pair()
    after=pair(book_seconds=100+book_delta,period_seconds=1000+period_delta,
               captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    verdict=compare(before,after,requested_seconds=60,elapsed_seconds=60,tolerance_seconds=5)
    assert verdict.complete and verdict.attributable_seconds==60
    assert verdict.code=="DUAL_DELTA_WITH_TOLERANCE"


@pytest.mark.parametrize("book_delta,period_delta,elapsed",[(76,76,60),(0,0,60),(0,60,60),(60,90,60),(64,64,59)])
def test_tolerance_keeps_no_change_large_errors_and_short_sessions_unverified(book_delta,period_delta,elapsed):
    before=pair()
    after=pair(book_seconds=100+book_delta,period_seconds=1000+period_delta,
               captured_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    assert not compare(before,after,requested_seconds=60,elapsed_seconds=elapsed,tolerance_seconds=5).complete
