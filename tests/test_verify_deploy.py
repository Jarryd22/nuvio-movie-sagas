import copy
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
from unittest import mock
import unittest
from urllib.error import URLError


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("verify_deploy", ROOT / "src" / "verify_deploy.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
TODAY = date(2026, 11, 4)
HEADERS = {"content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*"}


def json_reply(value, status=200, headers=None):
    return verifier.Response(status, copy.deepcopy(HEADERS if headers is None else headers),
                             json.dumps(value).encode("utf-8"))


def film(number, released="2026-11-04"):
    return {"id": f"tt{number:07d}", "type": "movie", "name": f"Film {number}",
            "released": released + "T00:00:00.000Z", "releaseInfo": released[:4],
            "poster": f"https://image.tmdb.org/t/p/w500/film{number}.jpg",
            "posterShape": "poster", "description": "Public movie metadata", "genres": ["Adventure"]}


def deployment():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    expected = verifier.expected_catalogues(config)
    manifest = {**config["addon"], "version": "2026.11.4", "resources": ["catalog"],
                "types": ["movie"], "idPrefixes": ["tt"],
                "behaviorHints": {"configurable": False, "configurationRequired": False},
                "catalogs": [{"id": c["id"], "name": c.get("name", "Public TMDB Collection Name"), "type": "movie"}
                             for c in expected]}
    responses = {"manifest.json": json_reply(manifest)}
    for index, entry in enumerate(expected):
        responses[f"catalog/movie/{entry['id']}.json"] = json_reply({
            "metas": [film(index * 2 + 1), film(index * 2 + 2, "2020-01-01")],
            "cacheMaxAge": 3600, "staleRevalidate": 3600, "staleError": 86400})
    responses["catalog/movie/verification-missing-catalogue.json"] = verifier.Response(
        404, {"content-type": "text/html"}, b"<h1>Not found</h1>")
    return config, responses


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = 0
        self.paths = []

    def get(self, path):
        self.requests += 1
        self.paths.append(path)
        return self.responses[path]


class DeploymentVerificationTests(unittest.TestCase):
    def setUp(self):
        self.config, self.responses = deployment()

    def run_check(self):
        return verifier.verify(self.config, FakeClient(self.responses), TODAY)

    def edit_json(self, path, change):
        value = json.loads(self.responses[path].body)
        change(value)
        self.responses[path] = json_reply(value)

    def test_complete_45_catalogue_deployment_returns_only_public_counts(self):
        client = FakeClient(self.responses)
        result = verifier.verify(self.config, client, TODAY)
        self.assertEqual(result, {"ok": True, "catalogues_checked": 45, "rotating_rows": 8,
                                 "permanent_rows": 37, "film_entries": 90, "unique_films": 90,
                                 "http_requests": 47, "missing_catalogue_404": True})
        self.assertEqual(len(client.paths), len(set(client.paths)))
        self.assertNotIn("http", json.dumps({k: v for k, v in result.items() if k != "http_requests"}))

    def test_actual_builder_output_passes_the_independent_public_contract(self):
        spec = importlib.util.spec_from_file_location("builder_contract_fixture", ROOT / "src" / "build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.config["discovery"]["enabled"] = False
        source = {}
        for seed in self.config["seed_collections"]:
            cid = seed["id"]
            mids = [cid * 100 + 1, cid * 100 + 2]
            source[f"collection/{cid}"] = {"id": cid, "name": "TMDB Collection", "parts": [{"id": m} for m in mids]}
            for index, mid in enumerate(mids):
                source[f"movie/{mid}"] = {
                    "id": mid, "title": f"Public film {mid}", "imdb_id": f"tt{mid:07d}",
                    "release_date": "2020-01-01" if index else TODAY.isoformat(), "status": "Released",
                    "genres": [{"id": g} for g in (878, 14, 12, 28, 27)], "vote_count": 200,
                    "belongs_to_collection": {"id": cid}, "production_companies": [],
                    "poster_path": f"/movie{mid}.jpg", "backdrop_path": f"/backdrop{mid}.jpg"}
        source_client = mock.Mock(secrets=())
        source_client.get.side_effect = lambda path, params=None: source[path]
        built = builder.build(self.config, source_client, TODAY)
        responses = {path: json_reply(value) for path, value in built["files"].items()}
        responses["catalog/movie/verification-missing-catalogue.json"] = verifier.Response(404, {}, b"Not found")
        result = verifier.verify(self.config, FakeClient(responses), TODAY)
        self.assertEqual(result["catalogues_checked"], 45)
        self.assertEqual(result["film_entries"], 90)
        self.assertEqual(result["unique_films"], 74)

    def test_wrong_addon_identity_and_missing_catalogue_are_rejected(self):
        self.edit_json("manifest.json", lambda m: m.update(id="org.wrong.addon"))
        with self.assertRaisesRegex(verifier.VerificationError, "identity"):
            self.run_check()
        self.config, self.responses = deployment()
        self.edit_json("manifest.json", lambda m: m["catalogs"].pop())
        with self.assertRaisesRegex(verifier.VerificationError, "count"):
            self.run_check()

    def test_unexpected_id_duplicate_id_and_wrong_order_are_rejected(self):
        for index, replacement in ((44, "collection-9999999"), (1, "featured"), (0, "space")):
            with self.subTest(index=index, replacement=replacement):
                self.config, self.responses = deployment()
                self.edit_json("manifest.json", lambda m: m["catalogs"][index].update(id=replacement))
                with self.assertRaisesRegex(verifier.VerificationError, "IDs or order"):
                    self.run_check()

    def test_fixed_rotating_heading_must_match_config(self):
        self.edit_json("manifest.json", lambda m: m["catalogs"][0].update(name="Today's Matrix Films"))
        with self.assertRaisesRegex(verifier.VerificationError, "stable heading"):
            self.run_check()

    def test_no_private_or_unsupported_manifest_fields(self):
        for change in (lambda m: m.update(api_key="do-not-print-this"),
                       lambda m: m["catalogs"][0].update(extra=[{"name": "skip"}]),
                       lambda m: m.update(resources=["catalog", "stream"])):
            with self.subTest(change=change):
                self.config, self.responses = deployment()
                self.edit_json("manifest.json", change)
                with self.assertRaises(verifier.VerificationError) as caught:
                    self.run_check()
                self.assertNotIn("do-not-print-this", str(caught.exception))

    def test_every_required_json_endpoint_needs_cors_and_json_content_type(self):
        for path in ("manifest.json", "catalog/movie/collection-115570.json"):
            for headers in ({"content-type": "application/json"},
                            {"content-type": "application/json", "access-control-allow-origin": "https://example.com"},
                            {"content-type": "text/html", "access-control-allow-origin": "*"}):
                with self.subTest(path=path, headers=headers):
                    self.config, self.responses = deployment()
                    old = self.responses[path]
                    self.responses[path] = verifier.Response(200, headers, old.body)
                    with self.assertRaises(verifier.VerificationError):
                        self.run_check()

    def test_empty_or_single_film_catalogue_is_rejected(self):
        for count in (0, 1):
            self.config, self.responses = deployment()
            self.responses["catalog/movie/featured.json"] = json_reply({"metas": [film(1)][:count]})
            with self.assertRaisesRegex(verifier.VerificationError, "fewer than two"):
                self.run_check()

    def test_duplicate_movie_id_is_rejected(self):
        self.responses["catalog/movie/featured.json"] = json_reply({"metas": [film(1), film(1)]})
        with self.assertRaisesRegex(verifier.VerificationError, "duplicate movie"):
            self.run_check()

    def test_newest_order_future_and_invalid_dates_are_rejected(self):
        for movies in ([film(1, "2020-01-01"), film(2)], [film(1, "2026-11-05"), film(2)],
                       [film(1, "2026-99-99"), film(2)]):
            with self.subTest(movies=movies):
                self.responses["catalog/movie/featured.json"] = json_reply({"metas": movies})
                with self.assertRaises(verifier.VerificationError):
                    self.run_check()

    def test_noncanonical_identifier_wrong_media_and_missing_title_are_rejected(self):
        for changes in ({"id": "tmdb:123"}, {"id": "tt123"}, {"type": "series"}, {"name": " "}):
            self.config, self.responses = deployment()
            self.edit_json("catalog/movie/featured.json", lambda c: c["metas"][0].update(changes))
            with self.assertRaises(verifier.VerificationError):
                self.run_check()

    def test_private_catalogue_fields_and_nested_movie_fields_are_rejected(self):
        for change in (lambda c: c.update(private_manifest_url="never-echo-this"),
                       lambda c: c["metas"][0].update(apiKey="never-echo-this")):
            self.config, self.responses = deployment()
            self.edit_json("catalog/movie/featured.json", change)
            with self.assertRaises(verifier.VerificationError) as caught:
                self.run_check()
            self.assertNotIn("never-echo-this", str(caught.exception))

    def test_artwork_must_be_public_https_image_without_embedded_credentials(self):
        for image in ("http://image.tmdb.org/poster.jpg", "https://127.0.0.1/poster.jpg",
                      "https://user:secret@example.com/poster.jpg", "https://example.com/poster.jpg?key=secret",
                      "https://example.com/some-page", ""):
            self.config, self.responses = deployment()
            self.edit_json("catalog/movie/featured.json", lambda c: c["metas"][0].update(poster=image))
            with self.assertRaisesRegex(verifier.VerificationError, "artwork"):
                self.run_check()

    def test_real_catalogue_returning_json_404_fails(self):
        self.responses["catalog/movie/featured.json"] = json_reply({"metas": [film(1), film(2)]}, status=404)
        with self.assertRaisesRegex(verifier.VerificationError, "HTTP 200"):
            self.run_check()

    def test_missing_catalogue_must_be_404_not_spa_or_json_200(self):
        for body in (b"<html>Movie Sagas landing page</html>", b'{"metas":[]}'):
            self.responses["catalog/movie/verification-missing-catalogue.json"] = verifier.Response(200, HEADERS, body)
            with self.assertRaisesRegex(verifier.VerificationError, "HTTP 404"):
                self.run_check()

    def test_invalid_html_json_and_duplicate_json_keys_fail_safely(self):
        for body in (b"<html>Sign in</html>", b'{"metas":[],"metas":[]}', b'{"value":NaN}'):
            self.responses["catalog/movie/featured.json"] = verifier.Response(200, HEADERS, body)
            with self.assertRaises(verifier.VerificationError):
                self.run_check()

    def test_base_url_validation_and_credential_free_errors(self):
        for url in ("http://example.com", "https://example.com?token=secret-sentinel",
                    "https://secret-sentinel@example.com", "https://127.0.0.1", "https://[::1]",
                    "https://192.168.1.115", "https://224.0.0.1", "https://host.internal", "https://example.com/%2e%2e",
                    "https://example.com/#secret-sentinel", "https://example.com:8080", "https://example.com/../x"):
            with self.subTest(url=url):
                with self.assertRaises(verifier.VerificationError) as caught:
                    verifier.validate_base_url(url)
                self.assertNotIn("secret-sentinel", str(caught.exception))
        self.assertEqual(verifier.validate_base_url("https://example.com/nuvio-movie-sagas/"),
                         "https://example.com/nuvio-movie-sagas")

    def test_dns_rejects_private_target_without_network_request(self):
        client = verifier.PublicClient("https://example.com")
        client.opener = mock.Mock()
        with mock.patch.object(verifier.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(verifier.VerificationError, "public addresses"):
                client.get("manifest.json")
        client.opener.open.assert_not_called()

    def test_http_client_has_bounded_retries_and_sends_no_credentials(self):
        client = verifier.PublicClient("https://example.com", timeout=3, retries=1)
        client.opener = mock.Mock()
        client.opener.open.side_effect = URLError("secret-sentinel upstream error")
        with mock.patch.object(client, "_check_public_dns"), mock.patch.object(verifier.time, "sleep"):
            with self.assertRaises(verifier.VerificationError) as caught:
                client.get("manifest.json")
        self.assertEqual(client.opener.open.call_count, 2)
        self.assertEqual(client.requests, 2)
        self.assertNotIn("secret-sentinel", str(caught.exception))
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(set(k.lower() for k in request.headers), {"accept", "origin", "user-agent"})
        self.assertEqual(client.opener.open.call_args.kwargs, {"timeout": 3})
        self.assertIsNone(verifier.NoRedirect().redirect_request(request, None, 302, "", {}, "https://127.0.0.1"))

    def test_cli_never_prints_a_rejected_secret_url(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
            code = verifier.main(["--config", str(ROOT / "config.json"),
                                  "--base-url", "https://example.com?token=secret-sentinel"])
        self.assertEqual(code, 1)
        self.assertNotIn("secret-sentinel", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
