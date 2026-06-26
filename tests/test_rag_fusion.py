"""RAG 融合策略测试：weighted（默认）与 RRF（新增）。

不依赖真实 embedding 或 Chroma；直接对 CodeRAGPipeline 的融合函数喂构造好的
RetrievalResult，校验：
- weighted 模式得分由原公式给出
- rrf 模式得分等于 1/(k+rank) 之和（容许极小类型加成）
"""

import pytest

from rag.models import CodeChunk, RetrievalResult


def _result(idx: int, semantic: float = 0.0, keyword: float = 0.0):
    chunk = CodeChunk(
        file_path=f"file_{idx}.py",
        chunk_type="function",
        name=f"fn_{idx}",
        content="def x(): pass",
        start_line=idx,
        end_line=idx + 1,
        signature="def x()",
        language="python",
    )
    return RetrievalResult(
        chunk=chunk,
        score=0.0,
        semantic_score=semantic,
        keyword_score=keyword,
    )


@pytest.fixture
def fuser():
    """构造 CodeRAGPipeline 的最小壳子，不触发 Chroma/embedding 初始化。"""
    from rag.pipeline import CodeRAGPipeline

    class _Stub(CodeRAGPipeline):
        def __init__(self):  # type: ignore[no-untyped-def]
            pass

    return _Stub()


def test_weighted_fuse_matches_original_formula(fuser):
    sem = [_result(1, semantic=0.8), _result(2, semantic=0.6)]
    kw = [_result(1, keyword=0.5), _result(3, keyword=0.4)]

    out = fuser._weighted_fuse(sem, kw)
    by_name = {r.chunk.name: r for r in out}

    # fn_1 双重命中：semantic=0.8, keyword=0.5, function type_boost=0.08, dual=0.1
    fn1 = by_name["fn_1"]
    assert fn1.score == pytest.approx(0.8 * 0.5 + 0.5 * 0.3 + 0.08 + 0.1, abs=1e-6)
    assert fn1.source == "hybrid"

    # fn_3 仅关键词：semantic=0, keyword=0.4, function type_boost=0.08, dual=0
    fn3 = by_name["fn_3"]
    assert fn3.score == pytest.approx(0.0 * 0.5 + 0.4 * 0.3 + 0.08 + 0.0, abs=1e-6)


def test_rrf_fuse_uses_rank_reciprocal(fuser, monkeypatch):
    # 锁定 k=60，避免设置文件干扰
    monkeypatch.setattr("settings.get", lambda key, default=None: 60 if "rrf_k" in key else default)

    sem = [_result(1, semantic=0.9), _result(2, semantic=0.5), _result(3, semantic=0.3)]
    kw = [_result(2, keyword=0.7), _result(1, keyword=0.4)]

    out = fuser._rrf_fuse(sem, kw)
    by_name = {r.chunk.name: r for r in out}

    # fn_1：semantic rank=0 → 1/61，keyword rank=1 → 1/62，+ function type_boost 0.005 + dual 0.005
    expected_fn1 = 1 / 61 + 1 / 62 + 0.005 + 0.005
    assert by_name["fn_1"].score == pytest.approx(expected_fn1, rel=1e-6)
    assert by_name["fn_1"].source == "hybrid"

    # fn_2：semantic rank=1 → 1/62，keyword rank=0 → 1/61，+ type 0.005 + dual 0.005
    expected_fn2 = 1 / 62 + 1 / 61 + 0.005 + 0.005
    assert by_name["fn_2"].score == pytest.approx(expected_fn2, rel=1e-6)

    # fn_3：仅 semantic rank=2 → 1/63，type 0.005，no dual
    expected_fn3 = 1 / 63 + 0.005
    assert by_name["fn_3"].score == pytest.approx(expected_fn3, rel=1e-6)


def test_hybrid_search_respects_fusion_config(monkeypatch, fuser):
    """rag.retrieval.fusion 配置应正确切换融合分支。"""
    sem = [_result(1, semantic=0.9)]
    kw = [_result(2, keyword=0.5)]

    weighted_calls = []
    rrf_calls = []

    def fake_weighted(s, k):
        weighted_calls.append(1)
        return list(s) + list(k)

    def fake_rrf(s, k):
        rrf_calls.append(1)
        return list(s) + list(k)

    fuser._weighted_fuse = fake_weighted  # type: ignore[assignment]
    fuser._rrf_fuse = fake_rrf  # type: ignore[assignment]
    fuser._semantic_search_many = lambda *a, **kw: sem  # type: ignore[assignment]
    fuser._keyword_search_many = lambda *a, **kw: kw_list  # type: ignore[assignment]
    fuser._deduplicate_by_file = lambda results, max_per_file: results  # type: ignore[assignment]

    kw_list = kw

    monkeypatch.setattr("settings.get", lambda key, default=None: "weighted" if "fusion" in key else default)
    fuser._hybrid_search("q", top_k=5)
    assert weighted_calls and not rrf_calls

    weighted_calls.clear()
    monkeypatch.setattr("settings.get", lambda key, default=None: "rrf" if "fusion" in key else default)
    fuser._hybrid_search("q", top_k=5)
    assert rrf_calls and not weighted_calls
