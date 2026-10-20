from agent.docs import DocIndex, tokenize


def test_tokenize_mixes_hangul_bigrams_and_words():
    assert tokenize("KEIT 과제번호 2024") == ["keit", "2024", "과제", "제번", "번호"]
    assert tokenize("돈") == ["돈"]


def test_finds_source_passages():
    ix = DocIndex()
    cases = [
        ("주관부처 코드 1450000 폐지", "폐지된"),
        ("해양수산 연구비 데이터 2023년까지만 있는 이유", "IRIS"),
        ("개발시작일 개발종료일 원래 값", "총개발기간"),
    ]
    for query, needle in cases:
        hits = ix.search(query, k=4)
        assert any(needle in c.text for _, c in hits), query
