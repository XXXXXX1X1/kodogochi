from datetime import datetime, timedelta, timezone

from kodogochi import rules as R

T0 = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)
RULES = R.Rules()


def kinds(effects):
    return [e.kind for e in effects]


def test_ci_fail_then_fix():
    pet = R.Pet(ci_state="green")
    assert kinds(R.apply_ci(pet, "failure", 1, 1, "bob", T0)) == ["ci_failed"]
    assert pet.ci_state == "red" and pet.red_since == T0
    assert R.apply_ci(pet, "failure", 2, 1, "bob", T0) == []  # уже красная — одно уведомление
    fixed = R.apply_ci(pet, "success", 3, 1, "carol", T0 + timedelta(hours=3))
    assert kinds(fixed) == ["ci_fixed"] and fixed[0].helper == "carol"
    assert pet.health == 100 and pet.red_since is None


def test_old_success_after_new_failure_keeps_red():
    pet = R.Pet()
    R.apply_ci(pet, "failure", 10, 1, "bob", T0)
    assert R.apply_ci(pet, "success", 9, 1, "bob", T0) == []
    assert R.apply_ci(pet, "success", 10, 1, "bob", T0) == []  # тот же запуск повторно
    assert pet.ci_state == "red"
    assert kinds(R.apply_ci(pet, "success", 10, 2, "bob", T0)) == ["ci_fixed"]  # перезапуск — новая версия


def test_cancelled_run_is_ignored():
    pet = R.Pet()
    R.apply_ci(pet, "failure", 1, 1, "bob", T0)
    assert R.apply_ci(pet, "cancelled", 2, 1, "bob", T0) == []
    assert pet.ci_state == "red" and pet.run_number == 1


def test_health_drops_20_per_full_day_and_dies_on_day_five():
    pet = R.Pet()
    R.apply_ci(pet, "failure", 1, 1, "bob", T0)
    R.tick_health(pet, T0 + timedelta(hours=23, minutes=59), RULES)
    assert pet.health == 100
    R.tick_health(pet, T0 + timedelta(days=1), RULES)
    assert pet.health == 80
    assert R.tick_health(pet, T0 + timedelta(days=4, hours=23), RULES) == []
    assert pet.health == 20 and not pet.dead
    died = R.tick_health(pet, T0 + timedelta(days=5), RULES)
    assert kinds(died) == ["died"] and pet.health == 0 and pet.dead
    assert died[0].facts["red_days"] == 5
    assert R.tick_health(pet, T0 + timedelta(days=6), RULES) == []  # один некролог
    revived = R.apply_ci(pet, "success", 2, 1, "carol", T0 + timedelta(days=6))
    assert kinds(revived) == ["revived"] and pet.health == 100 and not pet.dead


def pr(**kw):
    base = dict(number=7, author="alice", opened_at=T0)
    base.update(kw)
    p = R.PR(**base)
    R.pr_open_event(p, "opened", p.draft, T0)
    return p


def test_pr_exactly_48h_no_penalty_then_one_call_and_daily_steps():
    pet, p = R.Pet(), pr()
    assert R.tick_prs(pet, [p], T0 + timedelta(hours=48), RULES) == []
    assert pet.mood == 100
    eff = R.tick_prs(pet, [p], T0 + timedelta(hours=49), RULES)
    assert kinds(eff) == ["pr_overdue"] and eff[0].person == "alice" and pet.mood == 90
    assert R.tick_prs(pet, [p], T0 + timedelta(hours=60), RULES) == []  # та же ступень
    assert R.tick_prs(pet, [p], T0 + timedelta(hours=72), RULES) == [] and pet.mood == 80
    assert pet.mood_anchor_at is None


def test_draft_is_excluded_and_ready_restarts_timer():
    pet, p = R.Pet(), pr(draft=True)
    assert R.tick_prs(pet, [p], T0 + timedelta(days=5), RULES) == []
    R.pr_open_event(p, "ready_for_review", False, T0 + timedelta(days=5))
    assert R.tick_prs(pet, [p], T0 + timedelta(days=6), RULES) == []
    assert kinds(R.tick_prs(pet, [p], T0 + timedelta(days=7, hours=1), RULES)) == ["pr_overdue"]


def test_converted_to_draft_stops_timer():
    pet, p = R.Pet(), pr()
    R.pr_open_event(p, "converted_to_draft", True, T0 + timedelta(hours=10))
    assert R.tick_prs(pet, [p], T0 + timedelta(days=4), RULES) == []


def test_review_gives_bonus_once_and_stops_penalty():
    pet, p = R.Pet(mood=80), pr()
    R.tick_prs(pet, [p], T0 + timedelta(hours=49), RULES)
    eff = R.apply_review(pet, p, "bob", T0 + timedelta(hours=50), RULES)
    assert kinds(eff) == ["pr_reviewed"] and eff[0].helper == "bob" and eff[0].notify
    assert pet.mood == 80  # 80 − 10 + 10
    assert R.apply_review(pet, p, "carol", T0 + timedelta(hours=51), RULES) == []
    assert R.tick_prs(pet, [p], T0 + timedelta(days=5), RULES) == []


def test_review_of_fresh_pr_is_silent_and_self_review_ignored():
    pet, p = R.Pet(mood=50), pr()
    assert R.apply_review(pet, p, "alice", T0, RULES) == []
    eff = R.apply_review(pet, p, "bob", T0, RULES)
    assert not eff[0].notify and pet.mood == 60


def test_merge_without_review_counts_and_close_without_merge_does_not():
    pet, p = R.Pet(mood=50), pr()
    assert kinds(R.apply_pr_closed(pet, p, True, "bob", T0, RULES)) == ["pr_merged"] and pet.mood == 60
    pet2, p2 = R.Pet(mood=50), pr(number=8)
    assert R.apply_pr_closed(pet2, p2, False, None, T0, RULES) == [] and pet2.mood == 50
    assert p2.state == "closed"


def test_reopen_resets_steps():
    pet, p = R.Pet(), pr()
    R.tick_prs(pet, [p], T0 + timedelta(hours=49), RULES)
    R.apply_pr_closed(pet, p, False, None, T0 + timedelta(hours=50), RULES)
    R.pr_open_event(p, "reopened", False, T0 + timedelta(hours=60))
    assert p.eligible_since == T0 + timedelta(hours=60) and p.last_penalty_day == 0


def test_mood_recovers_10_per_full_day_without_overdue_prs():
    pet = R.Pet(mood=70)
    R.tick_prs(pet, [], T0, RULES)
    R.tick_prs(pet, [], T0 + timedelta(hours=23), RULES)
    assert pet.mood == 70
    R.tick_prs(pet, [], T0 + timedelta(days=2, hours=1), RULES)
    assert pet.mood == 90
    R.tick_prs(pet, [], T0 + timedelta(days=10), RULES)
    assert pet.mood == 100


NSK = timezone(timedelta(hours=7))


def friday(h, m, s=0):
    return datetime(2026, 11, 27, h, m, s, tzinfo=NSK)


def test_friday_boundary():
    pet = R.Pet()
    assert R.apply_push(pet, "main", "main", False, "a" * 40, "bob", friday(18, 0), RULES) == []
    assert kinds(R.apply_push(pet, "main", "main", False, "b" * 40, "bob", friday(18, 0, 1), RULES)) == ["friday_push"]
    assert R.apply_push(pet, "feature", "main", False, "c" * 40, "bob", friday(20, 0), RULES) == []
    assert R.apply_push(pet, "main", "main", True, "0" * 40, "bob", friday(20, 0), RULES) == []
    thursday = datetime(2026, 11, 26, 20, 0, tzinfo=NSK)
    assert R.apply_push(pet, "main", "main", False, "d" * 40, "bob", thursday, RULES) == []


def test_sleep_after_seven_days_and_wake_on_push():
    pet = R.Pet()
    assert R.tick_sleep(pet, T0, T0 + timedelta(days=6, hours=23), RULES) == []
    assert kinds(R.tick_sleep(pet, T0, T0 + timedelta(days=7), RULES)) == ["fell_asleep"]
    assert R.tick_sleep(pet, T0, T0 + timedelta(days=8), RULES) == []
    assert R.view(pet) == "sleeping"
    woke = R.apply_push(pet, "feature", "main", False, "e" * 40, "bob", friday(10, 0), RULES)
    assert kinds(woke) == ["woke"] and not woke[0].notify and not pet.sleeping


def test_view_order():
    assert R.view(R.Pet(dead=True, sleeping=True, ci_state="red")) == "dead"
    assert R.view(R.Pet(sleeping=True, ci_state="red")) == "sleeping"
    assert R.view(R.Pet(ci_state="red", mood=10)) == "sick"
    assert R.view(R.Pet(mood=50)) == "sad"
    assert R.view(R.Pet()) == "ok"
