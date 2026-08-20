"""Attribution rules — the correctness heart of the LinkedIn lane.

Ports the lead_tool regression tests, including the generalized-owner case
that locks the fix for the original hardcoded-name bug.
"""
from linkedin_lane import attribute_and_cap, is_junk_text


def _rec(pid, handle, text="A real post with enough content to pass junk.",
         interaction="", author=None):
    return {"id": pid, "url": f"https://linkedin.com/posts/{handle}_{pid}",
            "owner_handle": handle, "interaction": interaction,
            "author": author, "text": text}


def test_authored_iff_handle_matches_owner():
    records = [
        _rec("1", "jane-doe"),
        _rec("2", "someone-else", author="someone-else"),
    ]
    out = attribute_and_cap(records, "jane-doe", "Jane Doe")
    assert len(out["authored"]) == 1
    assert out["authored"][0]["post_id"] == "1"
    assert len(out["liked"]) == 1
    assert out["liked"][0]["original_author"] == "someone-else"


def test_shared_by_owner_is_authored_for_any_owner_name():
    # The original bug: "shared by" was hardcoded to one founder's name.
    records = [_rec("1", "other-handle", interaction="Shared by Karim Ben")]
    out = attribute_and_cap(records, "karim-ben", "Karim Ben")
    assert len(out["authored"]) == 1


def test_shared_by_someone_else_is_liked():
    records = [_rec("1", "other-handle", interaction="Shared by Someone Else",
                    author="other-handle")]
    out = attribute_and_cap(records, "karim-ben", "Karim Ben")
    assert len(out["authored"]) == 0
    assert len(out["liked"]) == 1


def test_authored_cap_keeps_bio_post_past_recency():
    records = [_rec(str(i), "o") for i in range(12)]
    records.append(_rec("bio", "o", text="Here's who I am — my founder story, twelve years in."))
    out = attribute_and_cap(records, "o", None, authored_keep=10)
    ids = [p["post_id"] for p in out["authored"]]
    assert "bio" in ids            # kept past the cap
    assert out["bio_kept"] is True
    assert len(ids) == 11          # 10 recent + the bio post


def test_dedup_keeps_longest_text():
    records = [
        _rec("1", "o", text="short but valid text"),
        _rec("1", "o", text="a much longer version of the exact same post body text"),
    ]
    out = attribute_and_cap(records, "o", None)
    assert len(out["authored"]) == 1
    assert "much longer" in out["authored"][0]["text"]


def test_junk_posts_are_dropped():
    records = [
        _rec("1", "o", text="Sign in to view — join LinkedIn today"),
        _rec("2", "o", text="https://example.com/image.png"),
        _rec("3", "o", text="2025-07-01T10:00:00"),
        _rec("4", "o", text="tiny"),
        _rec("5", "o"),
    ]
    out = attribute_and_cap(records, "o", None)
    assert [p["post_id"] for p in out["authored"]] == ["5"]
    assert out["dropped_junk"] == 4


def test_is_junk_text_accepts_real_content():
    assert not is_junk_text("We just shipped our MVP built with Cursor in a weekend!")
    assert is_junk_text(None)
    assert is_junk_text("   ")
