# Movie Sagas

A personal catalogue add-on for Nuvio. GitHub Actions refreshes public TMDB film metadata every six hours, and chooses a different set of featured franchises each Perth calendar day. It does not host video or provide streams.

## Layout

Use a separate **Movie Sagas** folder in What to Watch:

1. Eight rotating spotlights: Featured Saga, Space Saga, Dark Sci-Fi, Fantasy Quest, Adventure Classic, Action Saga, Horror Franchise, Rediscover a Saga.
2. The existing 37 named franchises, permanently accessible below the spotlights.

Each spotlight contains one complete franchise. No franchise repeats within the eight spotlights. A featured franchise can also appear in the permanent library below. Films are returned newest first and only after their recorded release date. A catalogue entry does not guarantee a playable stream.

All catalogue IDs and headings remain stable. Nuvio TV 1.3.1 caches manifests and catalogue bodies separately; mutable franchise headings could otherwise describe the wrong films. The landing page shows which franchise is currently in each spotlight.

Newly discovered franchises enter the spotlight pool only after validation: at least two released feature films, matching science fiction/adventure/fantasy/action/horror tastes and sufficient rating activity. Marvel Studios discoveries are excluded, with Blade allowed. The original seed franchises remain in the library. Discovery is bounded rather than exhaustive and is not a recommendation model trained on watch history.

## Hosting

The `Refresh Movie Sagas` workflow publishes a static GitHub Pages site. The PC can be off. GitHub schedules can be delayed, and Nuvio can keep loaded catalogue rows until app restart; this is periodic refresh, not continuous live streaming of updates.

1. Put a personal TMDB API Read Access Token in the repository Actions secret **TMDB_READ_TOKEN**. An API v3 key in **TMDB_API_KEY** is also supported. Do not put either value in a file or issue.
2. Enable GitHub Pages with **GitHub Actions** as its build source. Free Pages hosting normally requires a public repository; private repository Pages requires an eligible paid GitHub plan. The published site is public catalogue metadata.
3. Run **Refresh Movie Sagas** manually once. Verify the build and Pages deployment before adding the manifest URL to Nuvio.
4. Add `<site-url>/manifest.json` through Nuvio's Addons screen. Use its catalogues in the Movie Sagas folder.
5. Refresh Nuvio Addons and reopen the app for the first installation. Verify actual catalogue loading and an existing playback provider before replacing the old source rows.

No Nuvio login token, Comet URL, TorBox key, MDBList key or private account export belongs in this repository or its Actions secrets. Only a TMDB credential is required. All published movie IDs use IMDb identifiers so existing metadata and stream addons can resolve them.

Each successful refresh commits a small public selection-status file, keeping a useful history and repository activity. GitHub may disable scheduled workflows in inactive public repositories after 60 days, so an extended outage still needs attention. A failed refresh leaves the last successful site deployed. Missing TMDB credentials skip publication and mark the run as setup pending.

## Local checks

Python 3.12 or newer; no third-party Python packages.

```sh
python -m unittest discover -s tests -v
python src/build.py --config config.json --output dist --state .cache/state.json
```

The builder reads authentication from environment variables. Do not paste a credential into the command line. `--date YYYY-MM-DD` is available for deterministic rotation tests. A successful build writes standard manifest/catalogue JSON, `selections.json` and a public metadata cache. The hosting workflow copies the landing page and artwork into the output afterward.

## Account migration

Use a fresh local Nuvio export as the base. Move only the 37 `Movie Sagas` sources out of Adventure & Fantasy, leaving its existing 18 sources intact. Create one Movie Sagas folder with the eight spotlight catalogues and 37 permanent franchise catalogues. Hide this add-on's standalone home rows if Nuvio enables them on installation, while keeping its catalogues available inside the folder. Preserve every other collection, source, artwork URL, GIF setting and addon. Keep backups and full account exports outside this repository. After import, export again and compare the intended scope.

## Artwork and attribution

`assets/movie-sagas-cover.png` is an original generated cover; its prompt is saved alongside it. The focus animation links to the credited Tenor source in `assets/animation.json`; the GIF is not republished here.

This product uses the TMDB API but is not endorsed or certified by TMDB. Movie titles, metadata and artwork come from [TMDB](https://www.themoviedb.org/). This is a personal project, not an official Nuvio product.
