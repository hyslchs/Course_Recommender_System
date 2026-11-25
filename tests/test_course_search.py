import pytest

from fju_outline.course_search import (
    build_course_search_counts_payload,
    build_partitions,
    compare_course_sources,
    validate_course_search_counts_payload,
)


def test_build_partitions_matches_the_two_search_screens():
    partitions = build_partitions(hy=115, ht=1, lcid=1028, sco_typ=100)

    assert len(partitions) == 20
    assert [p.division for p in partitions[:4]] == ["D", "C", "G", "T"]
    assert all(p.base_params["Ht"] == 1 for p in partitions)
    assert all(p.params_for_page(2, 100)["pageNumber"] == 2 for p in partitions[:4])
    assert all(p.params_for_page(2, 100)["PageNumber"] == 2 for p in partitions[4:])


def test_comparison_uses_course_id_and_primary_course_code():
    basic = [
        {"jonCouSn": 1, "jAvaNO": "D010000001", "avaNO": "D010000001"},
        {"jonCouSn": 2, "jAvaNO": "D010000002", "avaNO": "G010000002"},
    ]
    general = [basic[0]]
    outlines = [
        {
            "source": {"jon_cou_sn": 1},
            "course": {
                "jon_cou_sn": 1,
                "joint_ava_no": "D010000001",
                "ava_no": "D010000001",
            },
        },
        {
            "source": {"jon_cou_sn": 3},
            "course": {
                "jon_cou_sn": 3,
                "joint_ava_no": "D010000003",
                "ava_no": "D010000003",
            },
        },
    ]

    result = compare_course_sources(
        outline_records=outlines,
        basic_rows=basic,
        general_rows=general,
    )

    basic_outline = result["comparisons"]["basic_vs_outline"]
    assert basic_outline["id"]["intersection"] == 1
    assert basic_outline["id"]["only_left"] == ["2"]
    assert basic_outline["id"]["only_right"] == ["3"]
    assert basic_outline["code_primary"]["intersection"] == 1
    assert result["comparisons"]["general_vs_basic"]["id"]["intersection"] == 1
    assert result["basic"]["unique_codes"] == 2
    assert result["basic"]["unique_code_variants"] == 3


def test_course_search_counts_are_joined_by_embedding_ids_only_and_keep_zero():
    payload = build_course_search_counts_payload(
        [
            {"jonCouSn": 1, "totNum": 55, "outNum": 0},
            {"jonCouSn": 999, "totNum": 12, "outNum": 3},
        ],
        [1, 2],
        metadata={"hy": 115, "ht": 1, "lcid": 1028, "sco_typ": 100},
    )

    rows = validate_course_search_counts_payload(payload, ["1", "2"])
    assert rows == {
        "1": {"selectable_count": 55, "external_count": 0},
        "2": {"selectable_count": None, "external_count": None},
    }
    assert payload["matched_course_count"] == 1


def test_course_search_counts_reject_duplicate_basic_ids():
    with pytest.raises(ValueError, match="duplicate course ID"):
        build_course_search_counts_payload(
            [
                {"jonCouSn": 1, "totNum": 1, "outNum": 0},
                {"jonCouSn": 1, "totNum": 2, "outNum": 0},
            ],
            [1],
        )


def test_course_search_counts_reject_ids_outside_embedding_catalog():
    payload = build_course_search_counts_payload(
        [{"jonCouSn": 1, "totNum": 1, "outNum": 0}],
        [1],
    )
    payload["courses"]["2"] = {"selectable_count": 1, "external_count": 0}
    with pytest.raises(ValueError, match="exactly match"):
        validate_course_search_counts_payload(payload, [1])
