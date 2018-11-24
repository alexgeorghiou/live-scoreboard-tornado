from tornado.testing import gen_test

from tests.support import GOAL, ScoreboardTestCase


class MatchApiTest(ScoreboardTestCase):
    @gen_test
    async def test_create_and_fetch_match(self) -> None:
        code, data, headers = await self.api(
            "POST",
            "/api/matches",
            {"id": "derby", "home_team": "Rovers", "away_team": "United", "competition": "Cup"},
        )
        assert code == 201
        assert headers["Location"] == "/api/matches/derby"
        assert data["status"] == "scheduled"

        code, data, headers = await self.api("GET", "/api/matches/derby")
        assert code == 200
        assert data["competition"] == "Cup"
        assert headers["ETag"] == '"0"'

        code, data, _ = await self.api("GET", "/api/matches")
        assert [m["match_id"] for m in data["matches"]] == ["derby"]
        assert data["matches"][0]["watchers"] == 0

    @gen_test
    async def test_create_errors(self) -> None:
        await self.create_match(kickoff=False)
        code, data, _ = await self.api(
            "POST", "/api/matches", {"id": "derby", "home_team": "A", "away_team": "B"}
        )
        assert (code, data["error"]) == (409, "conflict")
        code, data, _ = await self.api("POST", "/api/matches", {"home_team": "A"})
        assert (code, data["error"]) == (400, "validation_error")
        code, data, _ = await self.api("POST", "/api/matches", raw=b"{nope")
        assert (code, data["error"]) == (400, "invalid_json")

    @gen_test
    async def test_unknown_match_is_404(self) -> None:
        for path in ("/api/matches/ghost", "/api/matches/ghost/events"):
            code, data, _ = await self.api("GET", path)
            assert (code, data["error"]) == (404, "not_found")


class EventApiTest(ScoreboardTestCase):
    @gen_test
    async def test_post_event_updates_score(self) -> None:
        await self.create_match()
        code, data, headers = await self.api("POST", "/api/matches/derby/events", GOAL)
        assert code == 201
        assert data["duplicate"] is False
        assert data["event"]["seq"] == 2
        assert data["state"]["score"] == {"home": 1, "away": 0}
        assert headers["ETag"] == '"2"'

    @gen_test
    async def test_duplicate_event_returns_200(self) -> None:
        await self.create_match()
        event = {**GOAL, "id": "feed-7"}
        first, _, _ = await self.api("POST", "/api/matches/derby/events", event)
        code, data, _ = await self.api("POST", "/api/matches/derby/events", event)
        assert (first, code) == (201, 200)
        assert data["duplicate"] is True
        assert data["state"]["score"]["home"] == 1

    @gen_test
    async def test_illegal_and_invalid_events(self) -> None:
        await self.create_match(kickoff=False)
        code, data, _ = await self.api("POST", "/api/matches/derby/events", GOAL)
        assert (code, data["error"]) == (409, "illegal_event")
        assert "pre_match" in data["message"]
        code, data, _ = await self.api("POST", "/api/matches/derby/events", {"type": "corner"})
        assert (code, data["error"]) == (400, "validation_error")

    @gen_test
    async def test_if_match_enforces_expected_version(self) -> None:
        await self.create_match()
        code, data, _ = await self.api(
            "POST", "/api/matches/derby/events", GOAL, headers={"If-Match": '"0"'}
        )
        assert (code, data["error"], data["current_version"]) == (412, "version_conflict", 1)
        code, _, _ = await self.api(
            "POST", "/api/matches/derby/events", GOAL, headers={"If-Match": '"1"'}
        )
        assert code == 201
        code, data, _ = await self.api(
            "POST", "/api/matches/derby/events", GOAL, headers={"If-Match": "abc"}
        )
        assert (code, data["error"]) == (400, "invalid_header")

    @gen_test
    async def test_event_log_since(self) -> None:
        await self.create_match()
        await self.post_event("derby", GOAL)
        await self.post_event("derby", {**GOAL, "team": "away", "player": "Ito"})
        code, data, _ = await self.api("GET", "/api/matches/derby/events?since=1")
        assert code == 200
        assert [e["seq"] for e in data["events"]] == [2, 3]
        code, data, _ = await self.api("GET", "/api/matches/derby/events?since=x")
        assert code == 400

    @gen_test
    async def test_snapshot_replaces_state(self) -> None:
        await self.create_match()
        code, data, _ = await self.api(
            "PUT",
            "/api/matches/derby/snapshot",
            {"period": "second_half", "minute": 67, "score": {"home": 2, "away": 1}},
        )
        assert code == 200
        assert data["version"] == 2
        assert data["score"] == {"home": 2, "away": 1}
        _, events, _ = await self.api("GET", "/api/matches/derby/events")
        assert events["events"] == []
        code, data, _ = await self.api("PUT", "/api/matches/derby/snapshot", {"period": "x"})
        assert code == 400


class AuthTest(ScoreboardTestCase):
    settings_overrides = {"api_token": "s3cret"}

    @gen_test
    async def test_writes_require_token_reads_do_not(self) -> None:
        body = {"id": "derby", "home_team": "A", "away_team": "B"}
        code, data, _ = await self.api("POST", "/api/matches", body)
        assert (code, data["error"]) == (401, "unauthorized")
        code, _, _ = await self.api(
            "POST", "/api/matches", body, headers={"Authorization": "Bearer wrong"}
        )
        assert code == 401
        code, _, _ = await self.api(
            "POST", "/api/matches", body, headers={"Authorization": "Bearer s3cret"}
        )
        assert code == 201
        code, _, _ = await self.api("GET", "/api/matches/derby")
        assert code == 200


class OpsTest(ScoreboardTestCase):
    @gen_test
    async def test_health_and_stats(self) -> None:
        code, data, _ = await self.api("GET", "/healthz")
        assert (code, data["status"]) == (200, "ok")
        code, data, _ = await self.api("GET", "/api/stats")
        assert code == 200
        assert data["connections"] == 0
        assert data["hub"]["messages_in"] == 0

    def test_serves_frontend(self) -> None:
        response = self.fetch("/")
        assert response.code == 200
        assert b"<!doctype html>" in response.body.lower()
