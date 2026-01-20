# app/services/rag_service.py (최종 버전 - description 없음)

from sentence_transformers import SentenceTransformer
import faiss
import numpy as np
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from db.models import Shift


_model: SentenceTransformer | None = None
_index: faiss.Index | None = None
_metadata: List[Dict] = []


def get_or_build_shift_index(db: Session, group_id: str, force_rebuild: bool = False) -> Tuple[faiss.Index | None, List[Dict]]:
    global _model, _index, _metadata

    if _index is not None and not force_rebuild:
        print("기존 Shift 인덱스 사용")
        return _index, _metadata

    if _model is None:
        try:
            _model = SentenceTransformer('jhgan/ko-sroberta-multitask')
            print("SentenceTransformer 모델 로드 완료")
        except Exception as e:
            print(f"모델 로드 실패: {e}")
            raise

    shifts = db.query(Shift).filter(
        Shift.group_id == group_id,
        Shift.show_in_preference == True
    ).all()

    if not shifts:
        print(f"group_id={group_id}에 preference shift 없음")
        return None, []

    texts = []
    _metadata = []
    for s in shifts:
        # description 없음 → name과 shift_id만 사용 + 변형
        text = f"{s.name} {s.shift_id} {s.name.replace(' ', '')} {s.name.lower()}"
        texts.append(text)
        _metadata.append({
            "code": s.shift_id,
            "name": s.name
        })

    embeddings = _model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
    dim = embeddings.shape[1]

    _index = faiss.IndexFlatIP(dim)
    _index.add(embeddings.astype(np.float32))

    print(f"Shift 인덱스 빌드 완료: {len(shifts)}개 항목, 차원={dim}")
    return _index, _metadata


def search_best_shift(
    query_text: str,
    top_k: int = 3,
    min_score: float = 0.35
) -> Optional[Tuple[str, float, Dict]]:
    global _index, _metadata, _model
    if _index is None or _model is None:
        print("인덱스가 아직 빌드되지 않음")
        return None

    q_emb = _model.encode([query_text], normalize_embeddings=True)
    distances, indices = _index.search(q_emb.astype(np.float32), top_k)

    if len(indices[0]) == 0:
        return None

    best_idx = indices[0][0]
    best_score = distances[0][0]
    
    print(f"[RAG DEBUG] 쿼리: '{query_text}' → top score: {best_score:.3f}")

    if best_score < min_score:
        print(f"[RAG] 유사도 낮음: {best_score:.3f} < {min_score} (쿼리: '{query_text}')")
        return None

    best_meta = _metadata[best_idx]
    print(f"Best match: {best_meta['name']} ({best_meta['code']}) score={best_score:.3f}")

    return best_meta["code"], best_score, best_meta