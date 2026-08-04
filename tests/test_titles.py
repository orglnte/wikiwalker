"""Tests for the title/URL convention.

Everything that produces a title must produce the same form, or one article is
stored under two names and the visited set stops working. These check the three
producers agree: what a human types, what an href says, what gets displayed.
"""

from __future__ import annotations

from wikifetcher.titles import canonical, from_href, to_name, to_url

SITE = "en.wikipedia.org"


def test_typed_spaces_become_the_underscore_form() -> None:
    assert canonical("Walton Cardiff") == "Walton_Cardiff"
    assert canonical("  Bristol  ") == "Bristol"


def test_an_href_and_a_typed_title_agree() -> None:
    assert from_href("/wiki/Walton_Cardiff", SITE) == canonical("Walton Cardiff")


def test_percent_escapes_are_decoded() -> None:
    assert from_href("/wiki/Caf%C3%A9_wall_illusion", SITE) == "Café_wall_illusion"


def test_every_href_form_a_wiki_serves_is_understood() -> None:
    for href in (
        "/wiki/Bristol",
        "./Bristol",
        "https://en.wikipedia.org/wiki/Bristol",
        "//en.wikipedia.org/wiki/Bristol",
    ):
        assert from_href(href, SITE) == "Bristol", href


def test_a_link_to_a_section_is_a_link_to_the_article() -> None:
    """Wikipedia writes a great many of its links this way. Dropping them
    costs the search edges it should follow, silently."""
    for href in (
        "/wiki/Bristol#History",
        "./Bristol#History",
        "https://en.wikipedia.org/wiki/Bristol#History",
        "//en.wikipedia.org/wiki/Bristol#Etymology_and_early_history",
    ):
        assert from_href(href, SITE) == "Bristol", href


def test_a_section_link_into_another_wiki_is_still_not_this_wikis() -> None:
    assert from_href("https://it.wikipedia.org/wiki/Bristol#Storia", SITE) is None


def test_a_section_link_to_a_non_article_namespace_is_still_rejected() -> None:
    assert from_href("/wiki/Help:Contents#Top", SITE) is None


def test_another_wikis_link_is_not_a_link_on_this_one() -> None:
    """Interlanguage links differ from ordinary ones only by host; following
    one would silently leave the graph being searched."""
    assert from_href("https://it.wikipedia.org/wiki/Bristol", SITE) is None


def test_non_article_namespaces_are_rejected() -> None:
    for href in ("/wiki/Help:Contents", "/wiki/Category:Cities", "/wiki/File:X.jpg"):
        assert from_href(href, SITE) is None, href


def test_a_colon_in_a_real_title_is_not_a_namespace() -> None:
    assert from_href("/wiki/Star_Trek:_The_Next_Generation", SITE) == (
        "Star_Trek:_The_Next_Generation"
    )


def test_a_red_link_href_is_not_an_article_link() -> None:
    """Both forms a wiki serves. The second differs from an ordinary link only
    by its query string, so a pattern that ignores queries would follow it."""
    for href in (
        "/w/index.php?title=X&action=edit&redlink=1",
        "/wiki/X?action=edit&redlink=1",
        "//en.wikipedia.org/wiki/X?action=edit&redlink=1",
    ):
        assert from_href(href, SITE) is None, href


def test_url_and_display_forms_round_trip() -> None:
    assert to_url("Walton_Cardiff", SITE) == "https://en.wikipedia.org/wiki/Walton_Cardiff"
    assert to_name("Walton_Cardiff") == "Walton Cardiff"
    assert canonical(to_name("Walton_Cardiff")) == "Walton_Cardiff"


def test_the_url_host_is_not_assumed() -> None:
    """The same title names different articles on different wikis."""
    assert to_url("Bristol", "simple.wikipedia.org").startswith(
        "https://simple.wikipedia.org/"
    )
