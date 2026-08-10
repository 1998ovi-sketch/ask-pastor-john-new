def crawl_historical_archive(
    s: requests.Session,
    cutoff: datetime,
    first_year: int = 2013,
    max_pages_per_year: int = 20,
) -> list[dict]:
    """
    Crawl Desiring God's official yearly Interview archives.

    These archive cards explicitly label APJ resources as
    "Ask Pastor John", including special episodes.

    Only resources older than the current RSS window are returned.

    A 404 on the next archive page is treated as the normal end
    of pagination for that year.
    """
    out: dict[str, dict] = {}

    for year in range(first_year, cutoff.year + 1):
        year_seen_before = len(out)
        empty_or_repeat = 0

        for page in range(1, max_pages_per_year + 1):
            url = YEAR_ARCHIVE_URL.format(
                year=year,
                page=page,
            )

            # Desiring God returns HTTP 404 after the final valid
            # pagination page for a year.
            #
            # Example:
            # 2013 page 12 = valid
            # 2013 page 13 = 404
            #
            # That is not a crawler failure; it simply means
            # that we reached the end of that year's archive.
            try:
                body = get_text(s, url)

            except requests.exceptions.HTTPError as e:
                if (
                    e.response is not None
                    and e.response.status_code == 404
                ):
                    log(
                        f"Archive {year}: page {page} "
                        "does not exist; year complete."
                    )
                    break

                # Any HTTP error other than 404 should still
                # stop the build so that we don't silently
                # publish an incomplete archive.
                raise

            soup = BeautifulSoup(body, "lxml")

            interview_links = [
                a
                for a in soup.find_all("a", href=True)
                if re.match(
                    r"^/interviews/[^/?#]+/?$",
                    a.get("href", ""),
                )
            ]

            # If Desiring God ever changes pagination to return
            # an empty page instead of a 404, this also gives us
            # a clean stopping condition.
            if not interview_links:
                log(
                    f"Archive {year}: page {page} "
                    "contains no interviews; year complete."
                )
                break

            before = len(out)

            for a in interview_links:
                title, context = archive_title_and_context(a)

                # The yearly archive contains all kinds of
                # interviews. We only want Ask Pastor John.
                if "Ask Pastor John" not in context:
                    continue

                href = a.get("href", "")
                slug = clean_slug(href)

                dt = parse_date_text(context)

                if not dt:
                    continue

                # The official current RSS owns the cutoff date
                # and everything newer.
                #
                # This prevents overlap between the historical
                # archive and the current 1,000-item RSS feed.
                if dt.date() >= cutoff.date():
                    continue

                out[slug] = {
                    "title": title,
                    "slug": slug,
                    "page_url": urljoin(SITE, href),
                    "date": dt.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    "episode_number": None,
                    "special": False,
                    "description": "",
                }

            gained = len(out) - before

            log(
                f"Archive {year} page {page}: "
                f"+{gained} APJ "
                f"(total {len(out)})"
            )

            if gained == 0:
                empty_or_repeat += 1
            else:
                empty_or_repeat = 0

            # Extra protection against a site that repeats the
            # final archive page instead of returning 404.
            if empty_or_repeat >= 2:
                log(
                    f"Archive {year}: pagination appears "
                    "to have ended; year complete."
                )
                break

            # Small delay so that we don't unnecessarily hammer
            # Desiring God's website.
            time.sleep(0.08)

        year_total = len(out) - year_seen_before

        log(
            f"Archive {year}: "
            f"+{year_total} APJ items"
        )

    # Sanity check.
    #
    # There should be well over 1,200 historical APJ entries
    # before the January 2019 cutoff. If we get dramatically
    # fewer, something probably changed on Desiring God's site.
    if len(out) < 1200:
        raise RuntimeError(
            f"Historical archive crawl found only "
            f"{len(out)} APJ items; expected >1200 "
            "before the January 2019 RSS cutoff. "
            "Aborting instead of publishing an "
            "incomplete feed."
        )

    # Check three known historical boundary/special episodes.
    #
    # These ensure that:
    # - we reached Episode 1;
    # - we reached Episode 1303;
    # - special episodes were not accidentally filtered out.
    required = {
        "reflections-from-john-piper-on-his-birthday",
        "why-is-god-withholding-marriage-from-me",
        "john-pipers-prayer-at-planned-parenthood",
    }

    absent = sorted(required - set(out))

    if absent:
        raise RuntimeError(
            "Historical archive did not reach the "
            "complete range / specials. "
            f"Missing expected page(s): {absent}"
        )

    return list(out.values())