#!/usr/bin/env python3
"""Verify a public, catalogue-only deployment without credentials or playback.

Only configured public paths are requested. Responses are size bounded, redirects
are refused, and failures never echo response bodies, URLs, or exception text.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import time
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PERTH = timezone(timedelta(hours=8))
IMDB_ID = re.compile(r"tt\d{7,10}\Z")
CATALOG_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
MAX_BODY_BYTES = 5 * 1024 * 1024
MANIFEST_KEYS = {"id", "name", "description", "version", "resources", "types",
                 "idPrefixes", "catalogs", "behaviorHints"}
CATALOG_KEYS = {"metas", "cacheMaxAge", "staleRevalidate", "staleError"}
MOVIE_KEYS = {"id", "type", "name", "released", "releaseInfo", "posterShape",
              "genres", "description", "poster", "background"}


class VerificationError(Exception):
    """A fixed, credential-free diagnostic suitable for CI logs."""


class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def public_host(host: str) -> bool:
    """Reject local hostnames and non-public literal addresses before DNS."""
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
        return address.is_global and not address.is_multicast
    except ValueError:
        return bool("." in host and len(host) <= 253
                    and not host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
                    and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                            for part in host.split(".")))


def validate_base_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and bool(parts.hostname)
                 and public_host(parts.hostname) and parts.username is None
                 and parts.password is None and parts.port in (None, 443)
                 and not parts.query and not parts.fragment
                 and not re.search(r"[\s\\%]", value)
                 and bool(re.fullmatch(r"/[A-Za-z0-9._~/-]*|", parts.path))
                 and not any(p in (".", "..") for p in parts.path.split("/")))
    except (ValueError, TypeError):
        valid = False
    require(valid, "Base URL must be public HTTPS without credentials, query, or fragment.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self, base_url: str, timeout: float = 10, retries: int = 1):
        self.base_url = validate_base_url(base_url)
        require(0 < timeout <= 30 and retries in (0, 1), "Invalid request bounds.")
        self.timeout, self.retries, self.requests = timeout, retries, 0
        # No ambient proxy credentials, cookies, authorization, or redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _check_public_dns(self) -> None:
        host = urlsplit(self.base_url).hostname
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            require(bool(addresses) and all(public_host(a[4][0]) for a in addresses),
                    "Deployment hostname does not resolve exclusively to public addresses.")
        except (OSError, ValueError):
            raise VerificationError("Deployment hostname could not be verified.") from None

    def get(self, path: str) -> Response:
        require(bool(re.fullmatch(r"manifest\.json|catalog/movie/[a-z0-9-]+\.json", path)),
                "Unexpected verification path.")
        self._check_public_dns()
        request = Request(self.base_url + "/" + path,
                          headers={"Accept": "application/json", "Origin": "https://web.nuvio.tv",
                                   "User-Agent": "MovieSagasDeploymentCheck/1.0"}, method="GET")
        for attempt in range(self.retries + 1):
            self.requests += 1
            try:
                try:
                    response = self.opener.open(request, timeout=self.timeout)
                except HTTPError as error:
                    response = error
                with response:
                    status = response.getcode()
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    body = response.read(MAX_BODY_BYTES + 1)
                require(len(body) <= MAX_BODY_BYTES, "Deployment response exceeded its size limit.")
                if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(0.25)
                    continue
                return Response(status, headers, body)
            except (URLError, TimeoutError, OSError):
                if attempt < self.retries:
                    time.sleep(0.25)
                    continue
                raise VerificationError("Deployment request failed within its retry limit.") from None
        raise VerificationError("Deployment request failed.")


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON contains duplicate object keys.")
        result[key] = value
    return result


def read_json(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise VerificationError("Deployment returned invalid JSON.") from None
    require(isinstance(value, dict), "JSON response must be an object.")
    return value


def json_response(response: Response) -> dict:
    headers = {key.lower(): value for key, value in response.headers.items()}
    require(response.status == 200, "A required JSON endpoint did not return HTTP 200.")
    require(headers.get("access-control-allow-origin", "").strip() == "*",
            "A JSON endpoint is missing public cross-origin access.")
    require(headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json",
            "A JSON endpoint has the wrong content type.")
    return read_json(response.body)


def expected_catalogues(config: dict) -> list[dict]:
    require(isinstance(config, dict) and isinstance(config.get("addon"), dict), "Invalid local configuration.")
    slots, seeds = config.get("rotating_slots"), config.get("seed_collections")
    require(isinstance(slots, list) and len(slots) == 8 and isinstance(seeds, list) and len(seeds) == 37,
            "Configuration must contain eight rotating rows and 37 permanent franchises.")
    expected = []
    for slot in slots:
        require(isinstance(slot, dict) and isinstance(slot.get("id"), str)
                and bool(CATALOG_ID.fullmatch(slot["id"])) and not slot["id"].startswith("collection-")
                and isinstance(slot.get("name"), str) and bool(slot["name"].strip()),
                "Invalid rotating row configuration.")
        expected.append({"id": slot["id"], "name": slot["name"]})
    for seed in seeds:
        require(isinstance(seed, dict) and type(seed.get("id")) is int and seed["id"] > 0,
                "Invalid permanent franchise configuration.")
        expected.append({"id": f"collection-{seed['id']}"})
    require(len({item["id"] for item in expected}) == len(expected), "Configuration contains duplicate catalogue IDs.")
    return expected


def validate_manifest(manifest: dict, config: dict, expected: list[dict]) -> None:
    require(set(manifest) == MANIFEST_KEYS, "Manifest contains missing or unexpected fields.")
    for key in ("id", "name", "description"):
        require(isinstance(manifest.get(key), str) and manifest[key] == config["addon"].get(key),
                "Manifest identity does not match the local configuration.")
    require(isinstance(manifest.get("version"), str) and bool(re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", manifest["version"])),
            "Manifest version is invalid.")
    require(manifest["resources"] == ["catalog"] and manifest["types"] == ["movie"]
            and manifest["idPrefixes"] == ["tt"]
            and manifest["behaviorHints"] == {"configurable": False, "configurationRequired": False},
            "Manifest must remain a public movie-catalogue-only addon.")
    catalogs = manifest["catalogs"]
    require(isinstance(catalogs, list) and len(catalogs) == len(expected), "Manifest catalogue count is incorrect.")
    for actual, wanted in zip(catalogs, expected):
        require(isinstance(actual, dict) and set(actual) == {"id", "name", "type"},
                "Manifest catalogue contains missing or unexpected fields.")
        require(actual["id"] == wanted["id"] and actual["type"] == "movie",
                "Manifest catalogue IDs or order differ from the configuration.")
        require(isinstance(actual["name"], str) and bool(actual["name"].strip())
                and ("name" not in wanted or actual["name"] == wanted["name"]),
                "Manifest catalogue heading is missing or a stable heading changed.")


def plausible_image(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = validate_base_url(value)
    except VerificationError:
        return False
    return bool(re.search(r"/[^/]+\.(?:jpg|jpeg|png|webp|avif|gif)\Z", urlsplit(url).path, re.I))


def validate_catalogue(payload: dict, today: date, minimum_films: int = 2) -> list[str]:
    require("metas" in payload and set(payload) <= CATALOG_KEYS, "Catalogue contains missing or unexpected fields.")
    for key in CATALOG_KEYS - {"metas"}:
        if key in payload:
            require(type(payload[key]) is int and 0 <= payload[key] <= 604800, "Catalogue cache metadata is invalid.")
    metas = payload["metas"]
    require(isinstance(metas, list) and len(metas) >= minimum_films, "A catalogue has fewer than two released films.")
    ids, release_dates = [], []
    for movie in metas:
        require(isinstance(movie, dict) and set(movie) <= MOVIE_KEYS
                and {"id", "type", "name", "released", "releaseInfo", "poster", "posterShape"} <= set(movie),
                "Movie metadata contains missing or unexpected fields.")
        require(movie["type"] == "movie" and isinstance(movie["id"], str) and bool(IMDB_ID.fullmatch(movie["id"])),
                "A catalogue has a noncanonical movie identifier.")
        require(isinstance(movie["name"], str) and bool(movie["name"].strip()), "A film title is missing.")
        require(movie["posterShape"] == "poster" and plausible_image(movie["poster"])
                and ("background" not in movie or plausible_image(movie["background"])),
                "A film has a missing or implausible HTTPS artwork URL.")
        require("description" not in movie or isinstance(movie["description"], str), "A film description is invalid.")
        require("genres" not in movie or (isinstance(movie["genres"], list)
                and all(isinstance(g, str) and bool(g.strip()) for g in movie["genres"])), "Film genres are invalid.")
        try:
            raw = movie["released"]
            require(isinstance(raw, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00(?:\.000)?Z", raw)),
                    "A film release date is missing or invalid.")
            when = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(PERTH).date()
        except (ValueError, TypeError):
            raise VerificationError("A film release date is missing or invalid.") from None
        require(when <= today and movie["releaseInfo"] == str(when.year), "A film is unreleased or its release year is inconsistent.")
        ids.append(movie["id"])
        release_dates.append(when)
    require(len(ids) == len(set(ids)), "A catalogue contains duplicate movie IDs.")
    require(release_dates == sorted(release_dates, reverse=True), "A catalogue is not sorted newest first.")
    return ids


def verify(config: dict, client: PublicClient, today: date | None = None) -> dict:
    today = today or datetime.now(PERTH).date()
    expected = expected_catalogues(config)
    minimum = config.get("minimum_released_films", 2)
    require(type(minimum) is int and minimum >= 2, "Invalid minimum film count in configuration.")
    manifest = json_response(client.get("manifest.json"))
    validate_manifest(manifest, config, expected)
    all_ids = []
    for entry in expected:
        payload = json_response(client.get(f"catalog/movie/{entry['id']}.json"))
        all_ids.extend(validate_catalogue(payload, today, minimum))
    missing = client.get("catalog/movie/verification-missing-catalogue.json")
    require(missing.status == 404, "A missing catalogue does not return HTTP 404; check static-host routing.")
    return {"ok": True, "catalogues_checked": len(expected), "rotating_rows": 8,
            "permanent_rows": 37, "film_entries": len(all_ids), "unique_films": len(set(all_ids)),
            "http_requests": client.requests, "missing_catalogue_404": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--summary", type=Path, help="Optional public counts-only JSON summary")
    args = parser.parse_args(argv)
    try:
        config = read_json(args.config.read_bytes())
        summary = verify(config, PublicClient(args.base_url))
        serialized = json.dumps(summary, indent=2) + "\n"
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(serialized, encoding="utf-8")
        print(serialized, end="")
        return 0
    except VerificationError as error:
        print("Deployment verification failed: " + str(error), file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError):
        print("Deployment verification failed: invalid local data or filesystem error.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
