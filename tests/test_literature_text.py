"""Literature module: text normalisation, title/author/year matching and quote-span support (GOAL §5 L4.3).

Every check here fails closed: a normalisation may only erase differences a reader cannot see (Unicode
compatibility forms, invisible characters, whitespace, letter case). It may never erase a difference that changes
meaning (a minus sign, a negation, a sentence boundary, an inserted or elided word)."""

from __future__ import annotations

import pytest

from custom_harness.literature.normalize import (
    authors_match,
    family_name,
    quote_support,
    title_key,
    titles_match,
)

ABSTRACT = ("We study sparse gradient routing for tabular ensembles. On twelve benchmark datasets the method "
            "improves accuracy by 1.8 points over gradient boosting. It does not improve calibration, and "
            "training cost rises by 40%. The effect on the smallest datasets was −0.5 points.")


# ------------------------------------------------------------------ titles
def test_title_normalisation_ignores_case_punctuation_markup_and_spacing():
    assert titles_match("Sparse Gradient Routing for Tabular Ensembles",
                        "sparse  gradient routing for <i>tabular</i> ensembles.")
    assert titles_match("BERT: Pre-training of X", "BERT - Pre-training of X")


def test_title_with_one_word_changed_does_not_match():
    assert not titles_match("Sparse Gradient Routing for Tabular Ensembles",
                            "Dense Gradient Routing for Tabular Ensembles")
    assert not titles_match("Sparse Gradient Routing for Tabular Ensembles",
                            "Sparse Gradient Routing for Tabular Ensembles Revisited")


def test_title_with_cyrillic_homoglyph_does_not_match():
    # the 'a' in "Tabular" is CYRILLIC SMALL LETTER A (U+0430): looks identical, is a different string
    assert not titles_match("Sparse Gradient Routing for Tabulаr Ensembles",
                            "Sparse Gradient Routing for Tabular Ensembles")


def test_title_with_zero_width_characters_matches_visible_text():
    assert titles_match("Sparse Gradi​ent Routing for Tabular Ensembles",
                        "Sparse Gradient Routing for Tabular Ensembles")


def test_empty_title_never_matches():
    assert title_key("") == ()
    assert not titles_match("", "")
    assert not titles_match("...", "!!!")


# ------------------------------------------------------------------ authors
def test_family_name_forms():
    assert family_name("Okafor, Mira") == "okafor"
    assert family_name("Mira Okafor") == "okafor"
    assert family_name("M. Okafor") == "okafor"
    assert family_name("Tomás Lindqvist") == "lindqvist"
    assert family_name("Ana van der Berg") == "berg"
    assert family_name("José Núñez") == "nunez"
    assert family_name("") == ""


def test_authors_match_order_sensitive_prefix_with_et_al():
    record = ["Mira Okafor", "Tomas Lindqvist", "Wen Zhao"]
    assert authors_match(["Okafor, M.", "Lindqvist, T.", "Zhao, W."], record)
    assert authors_match(["Okafor", "et al."], record)
    assert authors_match(["M. Okafor", "T. Lindqvist", "et al"], record)


def test_authors_mismatch_cases():
    record = ["Mira Okafor", "Tomas Lindqvist", "Wen Zhao"]
    assert not authors_match(["Lindqvist", "Okafor"], record)          # order swapped
    assert not authors_match(["Okafor", "Smith"], record)              # invented co-author
    assert not authors_match(["Okafor", "Lindqvist", "Zhao", "Kim"], record)  # more authors than the paper
    assert not authors_match([], record)                               # nothing claimed is not a match
    assert not authors_match(["et al."], record)
    assert not authors_match(["Okafor"], [])


# ------------------------------------------------------------------ quote-span support
def test_exact_quote_is_supported():
    status, _ = quote_support("the method improves accuracy by 1.8 points over gradient boosting", [ABSTRACT])
    assert status == "supported"


def test_quote_tolerates_case_curly_quotes_whitespace_and_trailing_period():
    status, _ = quote_support("  The   method improves accuracy by 1.8 points over gradient boosting.  ", [ABSTRACT])
    assert status == "supported"
    status, _ = quote_support("“It does not improve calibration, and training cost rises by 40%”", [ABSTRACT])
    assert status == "supported"


def test_quote_found_in_any_supplied_text():
    status, _ = quote_support("training cost rises by 40%", ["unrelated text", ABSTRACT])
    assert status == "supported"


def test_fabricated_quote_is_unsupported():
    status, reason = quote_support("the method improves calibration by 1.8 points", [ABSTRACT])
    assert status == "unsupported"
    assert "not found" in reason


def test_quote_with_dropped_negation_is_unsupported():
    status, _ = quote_support("It does improve calibration, and training cost rises by 40%", [ABSTRACT])
    assert status == "unsupported"


def test_quote_stitched_from_two_sentences_is_unsupported():
    # first half of sentence 2 + second half of sentence 3, joined seamlessly
    status, _ = quote_support("the method improves accuracy by 1.8 points and training cost rises by 40%", [ABSTRACT])
    assert status == "unsupported"


def test_quote_that_erases_a_sentence_boundary_is_unsupported():
    status, _ = quote_support("over gradient boosting it does not improve calibration", [ABSTRACT])
    assert status == "unsupported"


def test_quote_that_drops_a_minus_sign_is_unsupported():
    status, _ = quote_support("The effect on the smallest datasets was 0.5 points", [ABSTRACT])
    assert status == "unsupported"
    status, _ = quote_support("The effect on the smallest datasets was -0.5 points", [ABSTRACT])
    assert status == "supported"  # ASCII hyphen-minus is the same sign as U+2212 MINUS SIGN


def test_quote_matching_only_inside_a_longer_word_is_unsupported():
    q = "oves accuracy by 1.8 points over gradient boost"
    assert q in ABSTRACT  # a raw substring, but it starts and ends inside words
    status, _ = quote_support(q, [ABSTRACT])
    assert status == "unsupported"


def test_elided_quote_is_unsupported():
    for q in ("the method improves accuracy ... over gradient boosting",
              "the method improves accuracy … over gradient boosting",
              "the method improves accuracy [...] over gradient boosting"):
        status, reason = quote_support(q, [ABSTRACT])
        assert status == "unsupported", q
        assert "elided" in reason


def test_quote_with_inserted_bracketed_words_is_unsupported():
    status, _ = quote_support("the method [strongly] improves accuracy by 1.8 points", [ABSTRACT])
    assert status == "unsupported"


def test_homoglyph_quote_is_unsupported():
    # "improves" with a Cyrillic 'о' (U+043E)
    status, reason = quote_support("the method imprоves accuracy by 1.8 points over gradient boosting", [ABSTRACT])
    assert status == "unsupported"
    assert "mixed-script" in reason


def test_bidi_and_tag_characters_make_a_quote_unsupported():
    for q in ("the method improves accuracy by 1.8 points ‮over gradient boosting",
              "the method improves accuracy by 1.8 points\U000e0020over gradient boosting"):
        status, reason = quote_support(q, [ABSTRACT])
        assert status == "unsupported", repr(q)
        assert "control" in reason


def test_zero_width_and_compatibility_forms_are_harmless():
    # zero-width space inside a word and full-width letters: the visible text is the abstract's text
    status, _ = quote_support("the method impro​ves accuracy by 1.8 points over gradient ｂoosting", [ABSTRACT])
    assert status == "supported"


def test_too_short_quote_is_not_evidence():
    status, reason = quote_support("improves accuracy", [ABSTRACT])
    assert status == "unsupported"
    assert "too short" in reason


def test_no_text_means_unchecked_not_supported():
    status, _ = quote_support("the method improves accuracy by 1.8 points", [])
    assert status == "unchecked"
    status, _ = quote_support("the method improves accuracy by 1.8 points", [None, ""])
    assert status == "unchecked"


@pytest.mark.parametrize("bad", ["", "   ", "...", "“”"])
def test_empty_quote_is_unsupported(bad):
    status, _ = quote_support(bad, [ABSTRACT])
    assert status == "unsupported"
