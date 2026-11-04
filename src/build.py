#!/usr/bin/env python3
"""Build a static, catalogue-only Stremio addon from exact TMDB collections.

Only public film metadata is written. Authentication is read from the environment
and never included in a manifest, URL in an error, state file, or build summary.
The caller should deploy the output only when this command exits successfully.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BuildError(Exception):
    """An actionable, credential-free failure safe to show in CI logs."""


IMDB_ID = re.compile(r"tt\d{7,10}\Z")
TASTE_GENRES = {28, 12, 14, 878, 27}
GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
          80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
          14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
          9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
          10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western"}


class TmdbClient:
    def __init__(self, token: str = "", api_key: str = "", timeout: int = 25):
        if not token and not api_key:
            raise BuildError("Set TMDB_READ_TOKEN or TMDB_API_KEY in the build environment.")
        self.token, self.api_key, self.timeout = token, api_key, timeout
        self.secrets = tuple(s for s in (token, api_key) if s)

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        if not re.fullmatch(r"(?:collection/\d+|movie/\d+|movie/popular|trending/movie/day)", endpoint):
            raise BuildError("Unsupported TMDB endpoint.")
        query = {"language": "en-US", **(params or {})}
        headers = {"Accept": "application/json", "User-Agent": "NuvioMovieSagas/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        else:
            query["api_key"] = self.api_key
        request = Request("https://api.themoviedb.org/3/" + endpoint + "?" + urlencode(query), headers=headers)
        for attempt in range(3):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    value = json.load(response)
                if not isinstance(value, dict) or value.get("success") is False:
                    raise BuildError("TMDB returned an unsuccessful response.")
                return value
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                    time.sleep(min(10, int(retry)) if retry.isdigit() else 2 ** attempt)
                    continue
                raise BuildError(f"TMDB request failed with HTTP {exc.code}; existing output was not replaced.") from None
            except (URLError, TimeoutError, OSError, ValueError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise BuildError("TMDB request failed or returned invalid JSON; existing output was not replaced.") from None
        raise BuildError("TMDB request failed.")


def positive_id(value: Any) -> int:
    if isinstance(value, bool):
        raise BuildError("Collection and movie IDs must be positive integers.")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BuildError("Collection and movie IDs must be positive integers.") from None
    if value <= 0:
        raise BuildError("Collection and movie IDs must be positive integers.")
    return value


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def text(value: Any, limit: int = 5000) -> str:
    return value[:limit] if isinstance(value, str) else ""


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def image_path(value: Any) -> str:
    value = text(value, 256)
    return value if re.fullmatch(r"/[A-Za-z0-9_.-]+", value) else ""


def normalize_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise BuildError("Config must be a JSON object.")
    seeds = []
    for value in raw.get("seed_collections", []):
        item = value if isinstance(value, dict) else {"id": value}
        seeds.append({"id": positive_id(item.get("id")), "title": text(item.get("title"), 200)})
    if not seeds or len({s["id"] for s in seeds}) != len(seeds):
        raise BuildError("Provide a nonempty seed_collections list with unique IDs.")
    slots = integer(raw.get("slot_count", 37))
    pool_limit = integer(raw.get("pool_limit", 80))
    if not 1 <= slots <= 80 or not slots <= pool_limit <= 80 or len(seeds) > pool_limit:
        raise BuildError("Use 1–80 slots and a pool_limit between slot_count and 80 that fits every seed.")
    keep = raw.get("keep_all_seeds", True)
    if not isinstance(keep, bool):
        raise BuildError("keep_all_seeds must be true or false.")
    if keep and len(seeds) > slots:
        raise BuildError("keep_all_seeds needs at least one slot per seed.")
    featured = [positive_id(v) for v in raw.get("featured_collection_ids", [])]
    if len(featured) != len(set(featured)) or len(featured) > slots:
        raise BuildError("Featured collection IDs must be unique and fit the available slots.")
    addon = raw.get("addon", {})
    addon_id = text(addon.get("id"), 150)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", addon_id):
        raise BuildError("Config addon.id must be a stable identifier containing letters, digits, dots, underscores or hyphens.")
    discovery = raw.get("discovery", {})
    rotating_slots = []
    for slot in raw.get("rotating_slots", []):
        sid, name = text(slot.get("id"), 100), text(slot.get("name"), 200)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", sid) or sid.startswith("collection-") or not name:
            raise BuildError("Rotating slots need a stable lowercase ID and a nonempty name.")
        rotating_slots.append({"id": sid, "name": name,
                               "genres": sorted({positive_id(v) for v in slot.get("genres", [])}),
                               "collection_ids": sorted({positive_id(v) for v in slot.get("collection_ids", [])})})
    if len(rotating_slots) > 20 or len({s["id"] for s in rotating_slots}) != len(rotating_slots):
        raise BuildError("Use at most 20 rotating slots with unique IDs.")
    return {"addon": {"id": addon_id, "name": text(addon.get("name"), 150) or "Movie Sagas",
                      "description": text(addon.get("description"), 1000) or "Released films from exact movie franchises."},
            "seed_collections": seeds, "slot_count": slots, "pool_limit": pool_limit,
            "keep_all_seeds": keep, "featured_collection_ids": featured,
            "rotating_slots": rotating_slots,
            "excluded_company_ids": sorted({positive_id(v) for v in raw.get("excluded_company_ids", [420])}),
            "allowed_marvel_collection_ids": sorted({positive_id(v) for v in raw.get("allowed_marvel_collection_ids", [735])}),
            "minimum_released_films": max(2, integer(raw.get("minimum_released_films", 2))),
            "discovery": {"enabled": discovery.get("enabled", True) is True,
                          "max_candidate_movies": min(40, max(0, integer(discovery.get("max_candidate_movies", 40)))),
                          "minimum_votes": max(100, integer(discovery.get("minimum_votes", raw.get("discovery_min_vote_count", 100))))},
            "movie_cache_days": min(30, max(1, integer(raw.get("movie_cache_days", 7))))}


def sanitize_movie(raw: dict) -> dict:
    mid = positive_id(raw.get("id"))
    external = raw.get("external_ids") if isinstance(raw.get("external_ids"), dict) else {}
    imdb = raw.get("imdb_id") or external.get("imdb_id") or ""
    imdb = imdb if isinstance(imdb, str) and IMDB_ID.fullmatch(imdb) else ""
    genres = raw.get("genres", [])
    genre_ids = raw.get("genre_ids", [])
    if genres:
        genre_ids = [x.get("id") for x in genres if isinstance(x, dict)]
    companies = raw.get("production_companies", [])
    company_ids = raw.get("company_ids", [])
    company_names = raw.get("company_names", [])
    if companies:
        company_ids = [x.get("id") for x in companies if isinstance(x, dict)]
        company_names = [x.get("name") for x in companies if isinstance(x, dict)]
    return {"id": mid, "imdb_id": imdb, "title": text(raw.get("title") or raw.get("name"), 300),
            "release_date": text(raw.get("release_date"), 10), "status": text(raw.get("status"), 40),
            "adult": raw.get("adult") is True, "video": raw.get("video") is True,
            "poster_path": image_path(raw.get("poster_path")), "backdrop_path": image_path(raw.get("backdrop_path")),
            "overview": text(raw.get("overview")), "genre_ids": sorted({integer(g) for g in genre_ids if integer(g) > 0}),
            "company_ids": sorted({integer(c) for c in company_ids if integer(c) > 0}),
            "company_names": sorted({text(name, 200).strip() for name in company_names if text(name, 200).strip()}),
            "vote_count": max(0, integer(raw.get("vote_count"))),
            "belongs_to_collection": positive_id(raw["belongs_to_collection"]["id"])
                if isinstance(raw.get("belongs_to_collection"), dict) and raw["belongs_to_collection"].get("id") else integer(raw.get("belongs_to_collection"))}


def released(movie: dict, today: date) -> bool:
    when = parse_date(movie.get("release_date"))
    return bool(when and when <= today and movie.get("imdb_id") and movie.get("title")
                and not movie.get("adult") and not movie.get("video")
                and movie.get("status") in ("", "Released"))


def choose_collections(pool_ids: list[int], seed_ids: list[int], featured: list[int],
                       slots: int, today: date, keep_all_seeds: bool) -> list[int]:
    pool_ids = sorted(set(pool_ids))
    if len(pool_ids) < slots:
        raise BuildError("Too few valid franchises to fill every catalogue slot; no output was replaced.")
    mandatory = set(featured) | (set(seed_ids) if keep_all_seeds else set())
    if not mandatory.issubset(pool_ids) or len(mandatory) > slots:
        raise BuildError("The required featured/seed franchises do not fit the valid pool.")
    optional = [cid for cid in pool_ids if cid not in mandatory]
    offset = today.toordinal() % len(optional) if optional else 0
    optional = optional[offset:] + optional[:offset]
    chosen = sorted(mandatory) + optional[:slots - len(mandatory)]
    rotation = today.toordinal() % len(chosen)
    return chosen[rotation:] + chosen[:rotation]


def assign_rotating_slots(slots: list[dict], pool: dict[str, dict], today: date,
                          featured: list[int], previous: dict[str, int] | None = None) -> list[tuple[dict, int]]:
    """Give every semantic slot one distinct, qualifying franchise.

    Resolve the smallest candidate sets first. Backtracking avoids starving a
    restrictive slot when several headings can draw from the same franchise.
    """
    allowed = {}
    for slot in slots:
        candidates = []
        wanted_ids, wanted_genres = set(slot["collection_ids"]), set(slot["genres"])
        for key, franchise in pool.items():
            cid = int(key)
            genres = {g for film in franchise["films"] for g in film["genre_ids"]}
            if wanted_ids and cid not in wanted_ids:
                continue
            if wanted_genres and not genres.intersection(wanted_genres):
                continue
            candidates.append(cid)
        candidates.sort()
        if not candidates:
            raise BuildError(f"Rotating slot {slot['id']} has no qualifying franchise; output was not replaced.")
        if slot["id"] == "featured" and set(featured).intersection(candidates):
            candidates = sorted(set(featured).intersection(candidates)) + [c for c in candidates if c not in featured]
            featured_count = len(set(featured).intersection(candidates))
            offset = today.toordinal() % featured_count
            candidates = candidates[offset:featured_count] + candidates[:offset] + candidates[featured_count:]
        else:
            salt = int.from_bytes(hashlib.sha256(slot["id"].encode()).digest()[:4], "big")
            offset = (today.toordinal() + salt) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        allowed[slot["id"]] = candidates
    if previous is not None:
        values = [previous.get(s["id"]) for s in slots]
        if len(set(values)) != len(slots) or any(previous.get(s["id"]) not in allowed[s["id"]] for s in slots):
            raise BuildError("Today's saved rotation is no longer valid; refusing to publish empty or silently reassigned rows.")
        return [(slot, previous[slot["id"]]) for slot in slots]
    priority = sorted(slots, key=lambda s: (len(allowed[s["id"]]), s["id"]))
    assignment = {}

    def match(index: int, used: set[int]) -> bool:
        if index == len(priority):
            return True
        sid = priority[index]["id"]
        for cid in allowed[sid]:
            if cid in used:
                continue
            assignment[sid] = cid
            if match(index + 1, used | {cid}):
                return True
            del assignment[sid]
        return False

    if not match(0, set()):
        raise BuildError("Cannot give every rotating slot a unique franchise; output was not replaced.")
    return [(slot, assignment[slot["id"]]) for slot in slots]


def movie_meta(movie: dict, franchise_name: str = "") -> dict:
    result = {"id": movie["imdb_id"], "type": "movie", "name": movie["title"],
              "released": movie["release_date"] + "T00:00:00.000Z",
              "releaseInfo": movie["release_date"][:4], "posterShape": "poster",
              "genres": [GENRES[g] for g in movie["genre_ids"] if g in GENRES],
              "description": (f"Saga: {franchise_name}.\n\n" if franchise_name else "") + movie["overview"]}
    if movie["poster_path"]:
        result["poster"] = "https://image.tmdb.org/t/p/w500" + movie["poster_path"]
    if movie["backdrop_path"]:
        result["background"] = "https://image.tmdb.org/t/p/w1280" + movie["backdrop_path"]
    return result


def build(config: dict, client: Any, today: date, old_state: dict | None = None) -> dict:
    """Fetch, validate and assemble artifacts in memory; never mutates output."""
    cfg = normalize_config(config)
    old_state = old_state or {}
    if old_state and (old_state.get("schema_version") != 1 or not isinstance(old_state.get("pool"), dict)):
        raise BuildError("State has an unsupported schema.")
    old_cache = old_state.get("movie_cache", {})
    if not isinstance(old_cache, dict):
        raise BuildError("State movie_cache must be an object.")
    cache: dict[str, dict] = {}

    def get_movie(mid: int) -> dict:
        key = str(mid)
        if key in cache:
            return cache[key]["movie"]
        cached = old_cache.get(key, {})
        when = parse_date(cached.get("fetched_on")) if isinstance(cached, dict) else None
        if when and 0 <= (today - when).days < cfg["movie_cache_days"] and isinstance(cached.get("movie"), dict):
            movie = sanitize_movie(cached["movie"])
            if movie["id"] != mid:
                raise BuildError("Cached movie ID does not match its key.")
            # Recheck films crossing their release date rather than trusting old status.
            release = parse_date(movie.get("release_date"))
            if (release and when < release <= today) or not released(movie, today) or "company_names" not in cached["movie"]:
                movie = sanitize_movie(client.get(f"movie/{mid}", {"append_to_response": "external_ids"}))
                when = today
        else:
            movie = sanitize_movie(client.get(f"movie/{mid}", {"append_to_response": "external_ids"}))
            when = today
        if movie["id"] != mid:
            raise BuildError("TMDB returned a different movie ID than requested.")
        cache[key] = {"fetched_on": when.isoformat(), "movie": movie}
        return movie

    seeds = cfg["seed_collections"]
    seed_ids = [s["id"] for s in seeds]
    seed_titles = {s["id"]: s["title"] for s in seeds if s["title"]}
    prior_rotation = old_state.get("rotation", {})
    prior_assignments = None
    if isinstance(prior_rotation, dict) and prior_rotation.get("date") == today.isoformat():
        values = prior_rotation.get("assignments", {})
        if isinstance(values, dict) and set(values) == {s["id"] for s in cfg["rotating_slots"]}:
            prior_assignments = {key: positive_id(cid) for key, cid in values.items()}
    active_ids = sorted(set((prior_assignments or {}).values()))
    existing_ids = sorted({positive_id(k) for k in old_state.get("pool", {})})
    if len(set(existing_ids) | set(seed_ids)) > cfg["pool_limit"]:
        existing_ids = seed_ids + [i for i in existing_ids if i not in seed_ids][:cfg["pool_limit"] - len(seed_ids)]
    candidate_ids = []
    candidate_count = 0
    if cfg["discovery"]["enabled"] and cfg["discovery"]["max_candidate_movies"]:
        candidates = []
        for endpoint in ("trending/movie/day", "movie/popular"):
            response = client.get(endpoint, {"page": 1})
            if not isinstance(response.get("results"), list):
                raise BuildError("TMDB discovery returned an invalid result list.")
            candidates.extend(response["results"][:20])
        seen = set()
        for item in candidates:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            mid = positive_id(item["id"])
            if mid in seen:
                continue
            if candidate_count >= cfg["discovery"]["max_candidate_movies"]:
                break
            seen.add(mid)
            candidate_count += 1
            movie = get_movie(mid)
            cid = movie["belongs_to_collection"]
            if cid and released(movie, today) and TASTE_GENRES.intersection(movie["genre_ids"]):
                if cid not in candidate_ids:
                    candidate_ids.append(cid)

    pool: dict[str, dict] = {}
    imdb_owners: dict[str, int] = {}
    skipped = {"missing_imdb": 0, "unreleased_or_other": 0, "unqualified_discoveries": 0}
    # Seeds are permanent. Fresh discoveries get a chance before the old
    # non-seed pool so reaching the cap never freezes discovery indefinitely.
    to_fetch = seed_ids + [i for i in active_ids if i not in seed_ids]
    to_fetch += [i for i in candidate_ids if i not in to_fetch]
    to_fetch += [i for i in existing_ids if i not in to_fetch]
    for cid in to_fetch:
        if len(pool) >= cfg["pool_limit"] and cid not in seed_ids:
            break
        raw = client.get(f"collection/{cid}")
        if positive_id(raw.get("id")) != cid or not isinstance(raw.get("parts"), list):
            raise BuildError("TMDB returned an invalid collection.")
        name = seed_titles.get(cid) or text(raw.get("name"), 200)
        if not name:
            raise BuildError("A collection has no title.")
        movie_ids = sorted({positive_id(p["id"]) for p in raw["parts"] if isinstance(p, dict) and p.get("id")})
        all_movies = [get_movie(mid) for mid in movie_ids]
        films = []
        for movie in all_movies:
            if not movie["imdb_id"]:
                skipped["missing_imdb"] += 1
            elif released(movie, today):
                previous = imdb_owners.get(movie["imdb_id"])
                if previous is not None and previous != movie["id"]:
                    raise BuildError("Conflicting TMDB films share an IMDb ID; refusing an ambiguous catalogue.")
                imdb_owners[movie["imdb_id"]] = movie["id"]
                films.append(movie)
            else:
                skipped["unreleased_or_other"] += 1
        films.sort(key=lambda m: (m["release_date"], m["id"]), reverse=True)
        if cid in seed_ids:
            if len(films) < cfg["minimum_released_films"]:
                raise BuildError(f"Seed collection {cid} has fewer than {cfg['minimum_released_films']} released IMDb-linked films; existing output was not replaced.")
        else:
            quality = [m for m in films if m["vote_count"] >= cfg["discovery"]["minimum_votes"]]
            excluded_company = any(set(cfg["excluded_company_ids"]).intersection(m["company_ids"]) for m in all_movies)
            marvel_company = any("marvel" in name.casefold() for m in all_movies for name in m["company_names"])
            avengers = "avengers" in text(raw.get("name")).casefold()
            # The user's one Marvel exception is the exact original Blade saga;
            # a similarly named collection or an Avengers title is not exempt.
            allowed = cid == 735 and cid in cfg["allowed_marvel_collection_ids"]
            if (len(quality) < cfg["minimum_released_films"]
                    or not any(TASTE_GENRES.intersection(m["genre_ids"]) for m in quality)
                    or avengers or ((excluded_company or marvel_company) and not allowed)):
                skipped["unqualified_discoveries"] += 1
                continue
        old_first_seen = parse_date(old_state.get("pool", {}).get(str(cid), {}).get("first_seen"))
        pool[str(cid)] = {"id": cid, "name": name, "seed": cid in seed_ids, "movie_ids": movie_ids,
                          "first_seen": (old_first_seen or today).isoformat(),
                          "films": films}
    if cfg["rotating_slots"]:
        assigned = assign_rotating_slots(cfg["rotating_slots"], pool, today, cfg["featured_collection_ids"], prior_assignments)
        rows = [{"catalog_id": slot["id"], "label": slot["name"], "collection_id": cid, "kind": "rotating"}
                for slot, cid in assigned]
        rows += [{"catalog_id": f"collection-{cid}", "label": pool[str(cid)]["name"],
                  "collection_id": cid, "kind": "permanent"} for cid in seed_ids]
    else:
        selected = choose_collections([int(k) for k in pool], seed_ids, cfg["featured_collection_ids"],
                                      cfg["slot_count"], today, cfg["keep_all_seeds"])
        rows = [{"catalog_id": f"saga-{i:02d}", "label": pool[str(cid)]["name"],
                 "collection_id": cid, "kind": "legacy-rotating"} for i, cid in enumerate(selected, 1)]
    manifest = {**cfg["addon"], "version": f"{today.year}.{today.month}.{today.day}",
                "resources": ["catalog"], "types": ["movie"], "idPrefixes": ["tt"], "catalogs": [],
                "behaviorHints": {"configurable": False, "configurationRequired": False}}
    files: dict[str, Any] = {}
    slot_map = []
    for row in rows:
        catalog_id, cid = row["catalog_id"], row["collection_id"]
        franchise = pool[str(cid)]
        manifest["catalogs"].append({"id": catalog_id, "type": "movie", "name": row["label"]})
        files[f"catalog/movie/{catalog_id}.json"] = {
            "metas": [movie_meta(m, franchise["name"]) for m in franchise["films"]], "cacheMaxAge": 3600,
            "staleRevalidate": 3600, "staleError": 86400}
        slot_map.append({"catalog_id": catalog_id, "collection_id": cid, "name": row["label"],
                         "franchise": franchise["name"], "kind": row["kind"],
                         "movie_count": len(franchise["films"])})
    referenced = {str(mid) for c in pool.values() for mid in c["movie_ids"]}
    state = {"schema_version": 1, "built_on": today.isoformat(),
             "pool": {k: {key: val for key, val in p.items() if key != "films"} for k, p in pool.items()},
             "movie_cache": {k: v for k, v in cache.items() if k in referenced},
             "rotation": {"date": today.isoformat(), "assignments": {
                 row["catalog_id"]: row["collection_id"] for row in rows if row["kind"] == "rotating"}}}
    summary = {"built_on": today.isoformat(), "slot_count": len(rows), "pool_size": len(pool),
               "seed_count": len(seed_ids), "candidate_movies_checked": candidate_count,
               "keep_all_seeds": cfg["keep_all_seeds"], "skipped": skipped, "slots": slot_map,
               "notes": ["Semantic catalogue IDs/headings stay fixed; their franchise assignments rotate daily.",
                         "Permanent collection rows always expose every configured seed franchise.",
                         "Clients cache catalogues and may display the previous daily selection until refresh.",
                         "TMDB release dates do not establish availability from any streaming provider."]}
    selections = {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "rotation_date": today.isoformat(),
                  "rows": [{"catalog_id": s["catalog_id"], "label": s["name"],
                            "collection_id": s["collection_id"], "collection_name": s["franchise"],
                            "movie_count": s["movie_count"]} for s in slot_map]}
    files.update({"manifest.json": manifest, "state.json": state, "build-summary.json": summary,
                  "selections.json": selections})
    secrets = tuple(s for s in getattr(client, "secrets", ()) if s)
    for value in files.values():
        serialized = json.dumps(value, ensure_ascii=False)
        if any(secret in serialized for secret in secrets):
            raise BuildError("A credential appeared in generated data; publication was blocked.")
    return {"files": files, "state": state, "summary": summary}


def write_output(result: dict, output: Path) -> None:
    """Replace a complete artifact directory only after all remote work validates."""
    if output.is_symlink():
        raise BuildError("Refusing an unsafe output directory.")
    output = output.resolve()
    if output == output.parent:
        raise BuildError("Refusing an unsafe output directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".sagas-build-", dir=output.parent))
    backup: Path | None = None
    try:
        for relative, value in result["files"].items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix=".sagas-previous-", dir=output.parent))
            backup.rmdir()
            output.rename(backup)
        try:
            stage.rename(output)
        except OSError:
            if backup is not None and backup.exists() and not output.exists():
                backup.rename(output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def load_json(path: Path, missing_ok: bool = False) -> dict:
    if missing_ok and not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise BuildError("Unable to read valid JSON from the supplied config/state file.") from None
    if not isinstance(value, dict):
        raise BuildError("Config/state JSON must be an object.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", type=Path, help="Previous state, updated after a successful build; default OUTPUT/state.json")
    parser.add_argument("--date", help="ISO build/cutoff date; default current Australia/Perth date")
    args = parser.parse_args(argv)
    try:
        # Perth is UTC+08:00 year-round; no platform tzdata dependency is needed.
        today = parse_date(args.date) if args.date else datetime.now(timezone(timedelta(hours=8))).date()
        if today is None:
            raise BuildError("--date must use YYYY-MM-DD.")
        config = load_json(args.config)
        state_path = args.state or args.output / "state.json"
        old_state = load_json(state_path, missing_ok=True)
        client = TmdbClient(os.environ.get("TMDB_READ_TOKEN", ""), os.environ.get("TMDB_API_KEY", ""))
        result = build(config, client, today, old_state)
        write_output(result, args.output)
        if args.state and args.state.resolve() != (args.output / "state.json").resolve():
            args.state.parent.mkdir(parents=True, exist_ok=True)
            fd, pending = tempfile.mkstemp(prefix=".sagas-state-", dir=args.state.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(result["state"], handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(pending, args.state)
            finally:
                if os.path.exists(pending):
                    os.unlink(pending)
        summary = result["summary"]
        print(f"Built {summary['slot_count']} nonempty catalogue slots from {summary['pool_size']} franchises.")
        return 0
    except BuildError as exc:
        print("Build failed: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("Build failed: invalid data or filesystem error; do not deploy this build.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
