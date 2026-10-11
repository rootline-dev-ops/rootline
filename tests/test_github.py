from rootline.github import GitHub
from rootline.render import marker

from .conftest import FakeResp, FakeSession

REPO = "acme/invoicer"


def gh_with(routes):
    s = FakeSession(routes)
    return GitHub(token="t", repo=REPO, session=s), s


def test_upsert_creates_then_updates():
    mk = marker(".github/workflows/ci.yml")
    gh, s = gh_with(
        {
            ("GET", f"/repos/{REPO}/issues/7/comments"): FakeResp(
                200, [{"id": 1, "body": "unrelated"}]
            ),
            ("POST", f"/repos/{REPO}/issues/7/comments"): FakeResp(
                201, {"id": 2, "html_url": "u2"}
            ),
        }
    )
    action, c = gh.upsert_comment(7, f"{mk}\nhello", mk)
    assert action == "created" and c["id"] == 2

    gh, s = gh_with(
        {
            ("GET", f"/repos/{REPO}/issues/7/comments"): FakeResp(
                200, [{"id": 1, "body": "x"}, {"id": 5, "body": f"{mk}\nold"}]
            ),
            ("PATCH", f"/repos/{REPO}/issues/comments/5"): FakeResp(
                200, {"id": 5, "html_url": "u5"}
            ),
        }
    )
    action, c = gh.upsert_comment(7, f"{mk}\nnew", mk)
    assert action == "updated" and c["id"] == 5
    assert s.requests[-1][2]["json"] == {"body": f"{mk}\nnew"}
    assert not any(m == "POST" for m, _, _ in s.requests)


def test_other_workflow_marker_not_overwritten():
    gh, _s = gh_with(
        {
            ("GET", f"/repos/{REPO}/issues/7/comments"): FakeResp(
                200, [{"id": 5, "body": marker("release.yml")}]
            ),
            ("POST", f"/repos/{REPO}/issues/7/comments"): FakeResp(201, {"id": 6}),
        }
    )
    action, _ = gh.upsert_comment(7, "b", marker("ci.yml"))
    assert action == "created"


def test_find_pr_for_run_fallbacks():
    gh, _ = gh_with({})
    assert gh.find_pr_for_run({"pull_requests": [{"number": 12}]}) == 12

    gh, _ = gh_with(
        {
            ("GET", f"/repos/{REPO}/commits/abc/pulls"): FakeResp(
                200, [{"number": 3, "state": "closed"}, {"number": 4, "state": "open"}]
            ),
        }
    )
    assert gh.find_pr_for_run({"pull_requests": [], "head_sha": "abc"}) == 4

    # fork PR: no pull_requests, commit lookup empty -> owner:branch search
    gh, s = gh_with(
        {
            ("GET", f"/repos/{REPO}/commits/abc/pulls"): FakeResp(200, []),
            ("GET", f"/repos/{REPO}/pulls"): FakeResp(200, [{"number": 9, "head": {"sha": "abc"}}]),
        }
    )
    run = {
        "pull_requests": [],
        "head_sha": "abc",
        "head_branch": "fix",
        "head_repository": {"owner": {"login": "forker"}},
    }
    assert gh.find_pr_for_run(run) == 9
    assert s.requests[-1][2]["params"] == {"head": "forker:fix", "state": "open"}


def test_get_file_base64():
    gh, _ = gh_with(
        {
            ("GET", f"/repos/{REPO}/contents/a.py"): FakeResp(
                200, {"encoding": "base64", "content": "eCA9IDEK"}
            ),
        }
    )
    assert gh.get_file("a.py", "sha") == "x = 1\n"


def test_marker_must_start_comment_and_foreign_comment_is_skipped():
    mk = marker("ci.yml")
    gh, s = gh_with(
        {
            ("GET", f"/repos/{REPO}/issues/7/comments"): FakeResp(
                200,
                [
                    {"id": 1, "body": f"quoted: {mk}"},
                    {"id": 2, "body": f"{mk}\nposted by someone else"},
                ],
            ),
            ("PATCH", f"/repos/{REPO}/issues/comments/2"): FakeResp(
                403, {"message": "Forbidden"}, text="Forbidden"
            ),
            ("POST", f"/repos/{REPO}/issues/7/comments"): FakeResp(201, {"id": 3}),
        }
    )
    action, c = gh.upsert_comment(7, f"{mk}\nnew", mk)
    assert action == "created" and c["id"] == 3
    assert not any(m == "PATCH" and p.endswith("/1") for m, p, _ in s.requests)
