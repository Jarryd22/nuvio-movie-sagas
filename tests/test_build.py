import importlib.util
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("sagas_build", Path(__file__).parents[1] / "src" / "build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
TODAY = date(2026, 11, 4)


class FakeClient:
    secrets = ("test-credential-DO-NOT-PUBLISH",)

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append(endpoint)
        if endpoint not in self.responses:
            raise builder.BuildError("Fixture request failed.")
        return self.responses[endpoint]


def film(mid, cid=1, released="2020-01-01", **updates):
    value = {"id": mid, "title": f"Movie {mid}", "imdb_id": f"tt{mid:07d}",
             "release_date": released, "status": "Released", "vote_count": 200,
             "genres": [{"id": 878}], "belongs_to_collection": {"id": cid},
             "production_companies": [], "poster_path": f"/poster{mid}.jpg"}
    value.update(updates)
    return value


def fixtures(ids=(1,), discovery=False):
    config = {"addon": {"id": "org.example.sagas", "name": "Sagas"}, "slot_count": len(ids),
              "seed_collections": [{"id": cid} for cid in ids], "pool_limit": 80,
              "keep_all_seeds": True, "discovery": {"enabled": discovery}}
    data = {}
    for cid in ids:
        mids = [cid * 100 + 1, cid * 100 + 2]
        data[f"collection/{cid}"] = {"id": cid, "name": f"Saga {cid}", "parts": [{"id": m} for m in mids]}
        for mid in mids:
            data[f"movie/{mid}"] = film(mid, cid)
    return config, data


class CatalogueTests(unittest.TestCase):
    def test_rotating_slots_are_unique_deterministic_and_featured_stays(self):
        args = ([1, 2, 3, 4, 5, 6], [1, 2, 3], [1], 3)
        a = builder.choose_collections(*args, TODAY, False)
        self.assertEqual(a, builder.choose_collections(*args, TODAY, False))
        b = builder.choose_collections(*args, date(2026, 11, 5), False)
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), len(set(a)))
        self.assertIn(1, a)
        self.assertIn(1, b)
        self.assertNotEqual(a.index(1), b.index(1))

    def test_seed_pool_survives_rotation(self):
        cfg, data = fixtures((1, 2, 3))
        cfg.update(slot_count=2, keep_all_seeds=False, featured_collection_ids=[1])
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(set(result["state"]["pool"]), {"1", "2", "3"})
        self.assertEqual(len(result["files"]["manifest.json"]["catalogs"]), 2)

    def test_future_and_unknown_dates_excluded_and_newest_first(self):
        cfg, data = fixtures()
        data["movie/101"]["release_date"] = "2025-01-01"
        data["movie/102"]["release_date"] = "2026-11-04"
        for mid, when in ((103, "2027-01-01"), (104, "")):
            data["collection/1"]["parts"].append({"id": mid})
            data[f"movie/{mid}"] = film(mid, released=when)
        metas = builder.build(cfg, FakeClient(data), TODAY)["files"]["catalog/movie/saga-01.json"]["metas"]
        self.assertEqual([m["id"] for m in metas], ["tt0000102", "tt0000101"])

    def test_missing_imdb_uses_external_id_and_excludes_unresolvable(self):
        cfg, data = fixtures()
        data["movie/101"].update(imdb_id=None, external_ids={"imdb_id": "tt0000101"})
        data["collection/1"]["parts"].append({"id": 103})
        data["movie/103"] = film(103, imdb_id=None)
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(len(result["files"]["catalog/movie/saga-01.json"]["metas"]), 2)
        self.assertEqual(result["summary"]["skipped"]["missing_imdb"], 1)

    def test_no_imdb_fallback_to_tmdb_identifiers(self):
        cfg, data = fixtures()
        data["movie/101"]["imdb_id"] = None
        with self.assertRaises(builder.BuildError):
            builder.build(cfg, FakeClient(data), TODAY)

    def test_id_collision_fails_before_publishing(self):
        cfg, data = fixtures()
        data["movie/102"]["imdb_id"] = data["movie/101"]["imdb_id"]
        with self.assertRaisesRegex(builder.BuildError, "share an IMDb"):
            builder.build(cfg, FakeClient(data), TODAY)

    def test_duplicate_parts_deduplicated_and_catalog_only(self):
        cfg, data = fixtures()
        data["collection/1"]["parts"].append({"id": 101})
        result = builder.build(cfg, FakeClient(data), TODAY)
        manifest = result["files"]["manifest.json"]
        self.assertEqual(manifest["resources"], ["catalog"])
        self.assertEqual(len(result["files"]["catalog/movie/saga-01.json"]["metas"]), 2)
        self.assertFalse(any(path.startswith(("stream/", "meta/")) for path in result["files"]))

    def test_failed_fetch_keeps_existing_output_untouched(self):
        cfg, data = fixtures()
        del data["movie/102"]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            output.mkdir()
            sentinel = output / "manifest.json"
            sentinel.write_text('{"previous":true}', encoding="utf8")
            with self.assertRaises(builder.BuildError):
                result = builder.build(cfg, FakeClient(data), TODAY)
                builder.write_output(result, output)
            self.assertEqual(sentinel.read_text(), '{"previous":true}')
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_public_artifacts_exclude_credentials_and_private_fields(self):
        cfg, data = fixtures()
        cfg["unrelated_private_key"] = FakeClient.secrets[0]
        data["movie/101"]["private_untrusted_field"] = FakeClient.secrets[0]
        result = builder.build(cfg, FakeClient(data), TODAY)
        serialized = json.dumps(result["files"])
        self.assertNotIn(FakeClient.secrets[0], serialized)
        self.assertNotIn("private_untrusted_field", serialized)
        data["movie/101"]["overview"] = FakeClient.secrets[0]
        with self.assertRaisesRegex(builder.BuildError, "credential appeared"):
            builder.build(cfg, FakeClient(data), TODAY)

    def test_cached_movies_refresh_on_release_date(self):
        cfg, data = fixtures()
        cfg["movie_cache_days"] = 7
        data["collection/1"]["parts"].append({"id": 103})
        data["movie/103"] = film(103, released="2026-11-05", status="Post Production")
        first = builder.build(cfg, FakeClient(data), TODAY)
        data["movie/103"]["status"] = "Released"
        next_client = FakeClient(data)
        second = builder.build(cfg, next_client, date(2026, 11, 5), first["state"])
        self.assertIn("movie/103", next_client.calls)
        self.assertNotIn("movie/101", next_client.calls)
        self.assertEqual(len(second["files"]["catalog/movie/saga-01.json"]["metas"]), 3)

    def test_discovery_quality_genres_and_marvel_exception(self):
        cfg, data = fixtures((1, 2, 735, 4), discovery=True)
        cfg["seed_collections"] = [{"id": 1}]
        cfg.update(slot_count=2, keep_all_seeds=False, featured_collection_ids=[1])
        data["collection/2"]["name"] = "Avengers Collection"
        data["collection/735"]["name"] = "Blade Collection"
        for mid in (201, 202, 73501, 73502):
            data[f"movie/{mid}"]["production_companies"] = [{"id": 420}]
        for mid in (401, 402):
            data[f"movie/{mid}"]["vote_count"] = 99
        data["trending/movie/day"] = {"results": [{"id": 201}, {"id": 73501}, {"id": 401}]}
        data["movie/popular"] = {"results": [{"id": 73501}]}
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(set(result["state"]["pool"]), {"1", "735"})
        self.assertEqual(result["summary"]["candidate_movies_checked"], 3)

    def test_discovery_never_checks_more_than_forty_candidate_films(self):
        cfg, data = fixtures(discovery=True)
        cfg["discovery"]["max_candidate_movies"] = 1000
        for mid in range(1000, 1060):
            data[f"movie/{mid}"] = film(mid, belongs_to_collection=None)
        data["trending/movie/day"] = {"results": [{"id": m} for m in range(1000, 1030)]}
        data["movie/popular"] = {"results": [{"id": m} for m in range(1030, 1060)]}
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(result["summary"]["candidate_movies_checked"], 40)

    def test_output_transaction_removes_stale_slots_and_preserves_valid_json(self):
        cfg, data = fixtures()
        result = builder.build(cfg, FakeClient(data), TODAY)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "dist"
            output.mkdir()
            (output / "stale.json").write_text("old")
            builder.write_output(result, output)
            self.assertFalse((output / "stale.json").exists())
            self.assertEqual(json.loads((output / "manifest.json").read_text())["catalogs"][0]["id"], "saga-01")

    def test_semantic_headings_stay_stable_while_eight_unique_selections_rotate(self):
        cfg, data = fixtures(tuple(range(1, 12)))
        cfg.update(slot_count=8, keep_all_seeds=False, featured_collection_ids=[1, 2])
        cfg["rotating_slots"] = [
            {"id": "featured", "name": "Featured Saga", "genres": []},
            {"id": "space", "name": "Space Saga", "genres": [878], "collection_ids": [1, 2, 3]},
            {"id": "dark-scifi", "name": "Dark Sci-Fi", "genres": [878], "collection_ids": [2, 3, 4]},
            {"id": "fantasy", "name": "Fantasy Quest", "genres": [14]},
            {"id": "adventure", "name": "Adventure Classic", "genres": [12]},
            {"id": "action", "name": "Action Saga", "genres": [28]},
            {"id": "horror", "name": "Horror Franchise", "genres": [27]},
            {"id": "rediscover", "name": "Rediscover a Saga", "genres": []},
        ]
        for key, value in data.items():
            if key.startswith("movie/"):
                value["genres"] = [{"id": g} for g in [878, 14, 12, 28, 27]]
        first = builder.build(cfg, FakeClient(data), TODAY)
        repeat = builder.build(cfg, FakeClient(data), TODAY)
        tomorrow = builder.build(cfg, FakeClient(data), date(2026, 11, 5))
        self.assertEqual(first["files"]["manifest.json"]["catalogs"], tomorrow["files"]["manifest.json"]["catalogs"])
        self.assertEqual(first["files"]["selections.json"]["rows"], repeat["files"]["selections.json"]["rows"])
        today_ids = [r["collection_id"] for r in first["summary"]["slots"][:8]]
        tomorrow_ids = [r["collection_id"] for r in tomorrow["summary"]["slots"][:8]]
        self.assertEqual(len(set(today_ids)), 8)
        self.assertNotEqual(today_ids, tomorrow_ids)
        self.assertEqual(len(first["files"]["manifest.json"]["catalogs"]), 19)
        self.assertTrue(all(f"catalog/movie/collection-{cid}.json" in first["files"] for cid in range(1, 12)))
        self.assertTrue(first["files"]["catalog/movie/featured.json"]["metas"][0]["name"].startswith("Movie "))
        self.assertTrue(first["files"]["catalog/movie/featured.json"]["metas"][0]["description"].startswith("Saga:"))

    def test_semantic_assignment_does_not_starve_restrictive_slots(self):
        pool = {str(cid): {"films": [{"genre_ids": [878]}]} for cid in (1, 2, 3)}
        slots = [{"id": "broad", "name": "Broad", "genres": [], "collection_ids": []},
                 {"id": "only-one", "name": "One", "genres": [], "collection_ids": [1]},
                 {"id": "only-two", "name": "Two", "genres": [], "collection_ids": [2]}]
        result = builder.assign_rotating_slots(slots, pool, TODAY, [])
        self.assertEqual([cid for _, cid in result], [3, 1, 2])
        slots[0]["collection_ids"] = [1, 2]
        with self.assertRaises(builder.BuildError):
            builder.assign_rotating_slots(slots, pool, TODAY, [])

    def test_wrong_id_dates_and_nonreleased_status_are_rejected(self):
        cfg, data = fixtures()
        data["movie/101"]["id"] = 555
        with self.assertRaisesRegex(builder.BuildError, "different movie ID"):
            builder.build(cfg, FakeClient(data), TODAY)
        self.assertFalse(builder.released(builder.sanitize_movie(film(101, released="2026-99-01")), TODAY))
        self.assertFalse(builder.released(builder.sanitize_movie(film(101, status="Planned")), TODAY))
        self.assertFalse(builder.released(builder.sanitize_movie(film(101, adult=True)), TODAY))

    def test_default_config_schema_supports_all_parent_fields(self):
        cfg, data = fixtures()
        cfg.update(excluded_company_ids=[420], allowed_marvel_collection_ids=[735],
                   minimum_released_films=2, discovery_min_vote_count=150)
        normalized = builder.normalize_config(cfg)
        self.assertEqual(normalized["discovery"]["minimum_votes"], 150)
        self.assertEqual(normalized["allowed_marvel_collection_ids"], [735])

    def test_full_pool_admits_new_discoveries_without_removing_seeds(self):
        cfg, data = fixtures((1, 2, 3, 4), discovery=True)
        cfg.update(seed_collections=[{"id": 1}], slot_count=2, pool_limit=3,
                   keep_all_seeds=False, featured_collection_ids=[1])
        old = {"schema_version": 1, "pool": {"1": {}, "2": {}, "3": {}}, "movie_cache": {}}
        data["trending/movie/day"] = {"results": [{"id": 401}]}
        data["movie/popular"] = {"results": []}
        result = builder.build(cfg, FakeClient(data), TODAY, old)
        self.assertEqual(set(result["state"]["pool"]), {"1", "2", "4"})
        self.assertEqual(result["summary"]["pool_size"], 3)

    def test_daily_assignments_survive_new_pool_members_until_next_day(self):
        cfg, data = fixtures((1, 2, 3, 4), discovery=True)
        cfg.update(seed_collections=[{"id": 1}, {"id": 2}], slot_count=2,
                   keep_all_seeds=False, featured_collection_ids=[])
        cfg["rotating_slots"] = [{"id": "featured", "name": "Featured", "genres": []},
                                 {"id": "rediscover", "name": "Rediscover", "genres": []}]
        data["trending/movie/day"] = {"results": []}
        data["movie/popular"] = {"results": []}
        first = builder.build(cfg, FakeClient(data), TODAY)
        data["trending/movie/day"] = {"results": [{"id": 301}, {"id": 401}]}
        later = builder.build(cfg, FakeClient(data), TODAY, first["state"])
        self.assertEqual(first["state"]["rotation"], later["state"]["rotation"])
        self.assertEqual(len(later["state"]["pool"]), 4)
        tomorrow = builder.build(cfg, FakeClient(data), date(2026, 11, 5), later["state"])
        self.assertNotEqual(later["state"]["rotation"]["assignments"], tomorrow["state"]["rotation"]["assignments"])

    def test_marvel_company_names_and_avengers_without_company_metadata_are_blocked(self):
        cfg, data = fixtures((1, 2, 3, 4), discovery=True)
        cfg.update(seed_collections=[{"id": 1}], slot_count=1, keep_all_seeds=False)
        data["collection/2"]["name"] = "Licensed Superhero Collection"
        data["movie/201"]["production_companies"] = [{"id": 9999, "name": "MARVEL Enterprises"}]
        data["collection/3"]["name"] = "Avengers Collection"
        data["collection/4"]["name"] = "Blade Collection"  # Similar name is not the exact exception.
        data["movie/401"]["production_companies"] = [{"id": 9998, "name": "Marvel Entertainment"}]
        data["trending/movie/day"] = {"results": [{"id": 201}, {"id": 301}, {"id": 401}]}
        data["movie/popular"] = {"results": []}
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(set(result["state"]["pool"]), {"1"})

    def test_exact_blade_exception_does_not_override_avengers_name(self):
        cfg, data = fixtures((1, 735), discovery=True)
        cfg.update(seed_collections=[{"id": 1}], slot_count=1, keep_all_seeds=False)
        data["collection/735"]["name"] = "Avengers Collection"
        data["movie/73501"]["production_companies"] = [{"id": 9998, "name": "Marvel Entertainment"}]
        data["trending/movie/day"] = {"results": [{"id": 73501}]}
        data["movie/popular"] = {"results": []}
        result = builder.build(cfg, FakeClient(data), TODAY)
        self.assertEqual(set(result["state"]["pool"]), {"1"})

    def test_company_names_are_sanitized_and_survive_public_cache_roundtrip(self):
        source = film(101, production_companies=[{"id": 777, "name": "Marvel Entertainment", "secret": "never"}])
        public = builder.sanitize_movie(source)
        self.assertEqual(public["company_names"], ["Marvel Entertainment"])
        self.assertEqual(builder.sanitize_movie(public)["company_names"], ["Marvel Entertainment"])
        self.assertNotIn("secret", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
