"""call_history 의미 보강 카탈로그 — 경로→(페이지/영역/액션) 라벨 + 요청본문 화이트리스트.

목적(GTM식 로깅): 변경요청을 "어느 화면(page)·세부영역(section)에서 / 누가(actor·미들웨어가 부착) /
대상(target)을 / 어떻게(changes 값) 했다"로 사람이 읽게 남긴다.

안전(PII):
  - fields = 값까지 로깅할 비민감 화이트리스트(본문키→한글라벨). 여기 없는 본문키는 로깅 안 함.
  - masked = 민감필드(전화/이메일/생년월일/비밀번호/인증코드/자격증명): 존재 여부만 라벨로 남기고 값은 '***'.
  - 미등록 경로는 무보강(미들웨어는 method+path 만 기록).

확장: 새 엔드포인트는 _CATALOG_RAW 에 항목만 추가하면 됨(미들웨어 코드 변경 없음).
경로 템플릿의 {name} 은 path 파라미터로 자동 추출되어 target 에 담긴다.
"""
import re
from typing import Any, Optional, Tuple

_REDACTED = "***"


def _compile(tpl: str) -> "re.Pattern":
    """'/nurses/{nurse_id}' → ^/nurses/(?P<nurse_id>[^/]+)$ (경로 파라미터=named group)."""
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", tpl)
    return re.compile("^" + pattern + "$")


# body_shape: 'dict'(객체) | 'list_root'(본문 자체 리스트) | 'list_in_key:<키>' | 'none'(본문없음) | 'multipart'
_CATALOG_RAW = [
    # ────────── 근무자관리 · 간호사/팀/등급 (nurses/teams/grade/weekly-off) ──────────
    {"m": "PATCH", "p": "/nurses/{nurse_id}", "page": "근무자관리", "section": "간호사 상세/사이드프로필",
     "action": "간호사 정보 수정", "shape": "dict",
     "fields": {"name": "이름", "experience": "경력", "role": "역할", "level_": "직책", "grade": "등급",
                "team_id": "팀", "gender": "성별", "joining_date": "입사일", "resignation_date": "퇴사일",
                "resignation_reason": "퇴사사유", "nurse_memo": "메모", "is_head_nurse": "수간호사여부",
                "preceptor_id": "프리셉터", "exclusion_partner_id": "상호배제파트너", "fixed_shift": "고정근무",
                "weekly_off_enabled": "주휴사용", "weekly_off_weekday": "주휴요일", "weekly_off_type": "주휴유형",
                "is_weekend_off": "주말휴무", "allowed_shifts": "허용근무형", "work_shifts": "근무가능형태",
                "enable_nurse_pair_preference": "페어선호", "enable_aide": "AIDE사용",
                "wanted_max_requests": "원티드요청상한"},
     "masked": {"phone_number": "연락처", "email": "이메일", "birth_date": "생년월일"}},
    {"m": "DELETE", "p": "/nurses/{nurse_id}", "page": "근무자관리", "section": "간호사 삭제(휴지통)",
     "action": "간호사 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/nurses/bulk-update", "page": "근무자관리", "section": "간호사 일괄편집(그리드)",
     "action": "간호사 정보 일괄 수정", "shape": "list_root", "item_id": "nurse_id",
     "fields": {"grade": "등급", "team_id": "팀", "role": "역할", "level_": "직책", "experience": "경력",
                "allowed_shifts": "허용근무형", "fixed_shift": "고정근무", "is_weekend_off": "주말휴무",
                "sequence": "순서", "active": "활성", "is_head_nurse": "수간호사여부",
                "exclusion_partner_id": "상호배제파트너", "resignation_date": "퇴사일"},
     "masked": {"phone_number": "연락처", "email": "이메일", "birth_date": "생년월일"}},
    {"m": "PUT", "p": "/nurses/monthly-limits", "page": "근무자관리", "section": "월근무한도(나이트개수)",
     "action": "간호사 월 근무한도 저장", "shape": "list_in_key:limits", "item_id": "nurse_id",
     "fields": {"n_max": "나이트최대", "n_exact": "나이트고정", "n_min": "나이트최소", "d_max": "주간최대",
                "d_exact": "주간고정", "d_min": "주간최소", "e_max": "저녁최대", "e_exact": "저녁고정",
                "e_min": "저녁최소", "o_max": "오프최대", "o_exact": "오프고정", "o_min": "오프최소"},
     "masked": {}},
    {"m": "POST", "p": "/nurses/monthly-limits/night-bulk", "page": "근무자관리",
     "section": "월근무한도/나이트 일괄적용", "action": "나이트 개수 병동 일괄 적용", "shape": "dict",
     "fields": {"group_id": "병동", "year": "연도", "month": "월", "kind": "적용유형", "value": "나이트개수"},
     "masked": {}},
    {"m": "POST", "p": "/nurses/sequence/save", "page": "근무자관리", "section": "간호사 순서·활성",
     "action": "간호사 순서·활성 단건 변경", "shape": "dict",
     "fields": {"nurse_id": "간호사", "new_sequence": "새순서", "active": "활성상태"}, "masked": {}},
    {"m": "POST", "p": "/nurses/sequence/reorder", "page": "근무자관리", "section": "간호사 순서 일괄재정렬",
     "action": "간호사 순서 일괄 재정렬", "shape": "dict",
     "fields": {"active_order": "활성순서", "inactive_order": "비활성순서"}, "masked": {}},
    {"m": "POST", "p": "/nurses/add-to-group", "page": "근무자관리", "section": "근무자 추가(멤버 등록)",
     "action": "선택 멤버 병동 근무자로 추가", "shape": "dict",
     "fields": {"nurse_ids": "대상간호사목록", "group_id": "병동"}, "masked": {}},
    {"m": "POST", "p": "/nurses/upload2-validate", "page": "근무자관리", "section": "엑셀 업로드/검증",
     "action": "간호사 엑셀 업로드 검증", "shape": "multipart", "fields": {}, "masked": {}, "readonly": True},
    {"m": "POST", "p": "/nurses/upload2-confirm", "page": "근무자관리", "section": "엑셀 업로드/저장",
     "action": "간호사 엑셀 업로드 저장", "shape": "list_in_key:rows",
     "fields": {"group_id": "병동"}, "masked": {"rows": "업로드행(PII포함)"}},
    {"m": "POST", "p": "/nurses/validate-excel", "page": "근무자관리", "section": "엑셀 업로드(레거시)/검증",
     "action": "엑셀 데이터 검증", "shape": "list_in_key:data", "fields": {}, "masked": {"data": "행(PII)"},
     "readonly": True},
    {"m": "POST", "p": "/nurses/confirm-upload", "page": "근무자관리", "section": "엑셀 업로드(레거시)/저장",
     "action": "엑셀 데이터 저장", "shape": "list_in_key:data",
     "fields": {"new_groups_to_create": "신규병동"}, "masked": {"data": "행(PII)"}},
    {"m": "POST", "p": "/nurses/integrated-register", "page": "근무자관리", "section": "직접입력 통합등록",
     "action": "신규 직원 계정+근무자 통합 등록", "shape": "list_in_key:members",
     "fields": {"group_id": "병동"}, "masked": {"members": "직원정보(PII)"}},
    {"m": "PATCH", "p": "/nurses/personnel-basic-info", "page": "마이페이지", "section": "기본정보 수정",
     "action": "마이페이지 기본정보 수정", "shape": "dict",
     "fields": {"experience": "총경력"}, "masked": {"email": "이메일"}},
    {"m": "POST", "p": "/nurses/profile-image", "page": "마이페이지", "section": "프로필 이미지",
     "action": "프로필 이미지 업로드", "shape": "multipart", "fields": {}, "masked": {}},
    {"m": "DELETE", "p": "/nurses/profile-image", "page": "마이페이지", "section": "프로필 이미지",
     "action": "프로필 이미지 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "PUT", "p": "/nurses/change-password", "page": "마이페이지", "section": "비밀번호 변경",
     "action": "비밀번호 변경", "shape": "dict", "fields": {},
     "masked": {"current_password": "현재PW", "new_password": "새PW", "confirm_password": "새PW확인",
                "verification_code": "인증번호"}},
    {"m": "POST", "p": "/nurses/change-phone/send-code", "page": "마이페이지", "section": "휴대폰 변경/인증발송",
     "action": "휴대폰 변경 인증번호 발송", "shape": "dict", "fields": {}, "masked": {"new_phone_number": "새휴대폰"}},
    {"m": "PUT", "p": "/nurses/change-phone/verify", "page": "마이페이지", "section": "휴대폰 변경/검증",
     "action": "휴대폰 번호 변경 확정", "shape": "dict", "fields": {},
     "masked": {"new_phone_number": "새휴대폰", "verification_code": "인증번호"}},
    {"m": "PATCH", "p": "/nurses/assignments/{assignment_id}", "page": "근무자관리", "section": "배정 수정",
     "action": "배정 수정", "shape": "dict",
     "fields": {"status": "상태", "reason": "사유", "start_date": "시작일", "end_date": "종료일",
                "expected_end_date": "예상종료", "target_group_id": "대상병동", "target_team_id": "대상팀",
                "target_grade": "대상등급"}, "masked": {}},
    {"m": "PUT", "p": "/nurses/assignments/{assignment_id}", "page": "근무자관리", "section": "배정 수정",
     "action": "배정 수정(deprecated)", "shape": "dict",
     "fields": {"status": "상태", "reason": "사유", "start_date": "시작일", "end_date": "종료일",
                "target_group_id": "대상병동", "target_team_id": "대상팀"}, "masked": {}},
    {"m": "DELETE", "p": "/nurses/assignments/{assignment_id}", "page": "근무자관리", "section": "배정 취소",
     "action": "배정 취소", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/nurses/assignments", "page": "근무자관리", "section": "배정 등록(deprecated)",
     "action": "배정 등록(파견/이동/휴직/퇴사/프리셉티)", "shape": "dict",
     "fields": {"nurse_id": "대상", "reason": "사유", "target_group_id": "대상병동", "start_date": "시작일",
                "expected_end_date": "예상종료", "target_team_id": "대상팀", "target_grade": "대상등급"},
     "masked": {}},
    {"m": "POST", "p": "/nurses/assignments/preview", "page": "근무자관리", "section": "배정 영향 미리보기",
     "action": "배정 전 영향 분석", "shape": "dict", "fields": {"nurse_id": "대상", "reason": "사유"},
     "masked": {}, "readonly": True},
    {"m": "POST", "p": "/nurse-period/change", "page": "근무자관리", "section": "속성 시점변경",
     "action": "간호사 속성 시점 변경", "shape": "dict",
     "fields": {"attribute": "속성", "nurse_id": "대상", "valid_from": "발효일", "value": "변경값"}, "masked": {}},
    {"m": "POST", "p": "/nurse-period/backfill", "page": "근무자관리", "section": "속성이력 백필",
     "action": "속성 period 백필(운영)", "shape": "dict", "fields": {"group_id": "병동"}, "masked": {}},
    {"m": "POST", "p": "/nurse-period/roll", "page": "근무자관리", "section": "속성 캐시 롤",
     "action": "속성 캐시 투영(cron)", "shape": "dict", "fields": {"group_id": "병동", "as_of": "기준일"}, "masked": {}},
    {"m": "PUT", "p": "/teams", "page": "근무자관리", "section": "팀설정(생성/이름/편성)",
     "action": "팀 일괄 동기화", "shape": "dict",
     "fields": {"teams": "팀편성", "delete_team_ids": "삭제팀", "year": "발효연", "month": "발효월"}, "masked": {}},
    {"m": "POST", "p": "/teams/classify/apply", "page": "근무자관리", "section": "팀 자동분류 적용",
     "action": "팀 자동분류 적용", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "group_id": "병동", "note": "메모"}, "masked": {}},
    {"m": "POST", "p": "/teams/classify/preview", "page": "근무자관리", "section": "팀 자동분류 미리보기",
     "action": "팀 자동분류 미리보기", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {},
     "readonly": True},
    {"m": "POST", "p": "/teams/redistribute/apply", "page": "근무자관리", "section": "병동 간 재분배 적용",
     "action": "병동 간 재분배 적용", "shape": "dict",
     "fields": {"group_ids": "병동목록", "year": "연도", "month": "월", "note": "메모"}, "masked": {}},
    {"m": "POST", "p": "/teams/redistribute/preview", "page": "근무자관리", "section": "병동 간 재분배 미리보기",
     "action": "병동 간 재분배 미리보기", "shape": "dict", "fields": {"group_ids": "병동목록"}, "masked": {},
     "readonly": True},
    {"m": "POST", "p": "/grade/config", "page": "근무자관리", "section": "등급설정(그룹 등급 제약)",
     "action": "그룹 등급 설정 저장", "shape": "dict",
     "fields": {"null_grade_policy": "NULL등급정책", "use_dynamic_scaling": "동적축소", "allow_soft_fallback": "soft완화",
                "constraints": "등급최소인원", "constraints_max": "등급최대인원", "grade_names": "등급이름",
                "use_mid": "MID사용", "group_id": "병동"}, "masked": {}},
    {"m": "PUT", "p": "/weekly-off/settings", "page": "설정", "section": "주휴설정(그룹 정책)",
     "action": "주휴 그룹 정책 저장", "shape": "dict",
     "fields": {"activate": "활성화", "use_variable_cycle": "변동주기", "cycle_type": "주기유형",
                "cycle_start_date": "주기시작", "cycle_interval": "주기간격", "shift_variation": "변동값",
                "group_id": "병동"}, "masked": {}},
    {"m": "PUT", "p": "/weekly-off/nurses", "page": "설정", "section": "주휴설정(간호사별)",
     "action": "간호사별 주휴 설정 저장", "shape": "list_in_key:items", "item_id": "nurse_id",
     "fields": {"weekly_off_enabled": "주휴사용", "weekly_off_weekday": "주휴요일"}, "masked": {}},

    # ────────── 근무표 · 시프트 · 생성 · 선호 (roster/roster_create/shifts/daily-shift/preferences) ──────────
    {"m": "POST", "p": "/roster/config/save", "page": "근무표만들기", "section": "생성 설정 저장",
     "action": "근무표 생성 설정(프리셋) 저장", "shape": "dict",
     "fields": {"config_id": "프리셋ID", "config_name": "프리셋이름", "config_version": "버전",
                "day_req": "D요구", "eve_req": "E요구", "nig_req": "N요구", "max_nig_per_month": "월나이트상한",
                "max_conseq_work": "최대연속근무", "off_days": "OFF수", "use_mid": "MID사용",
                "not_one_night": "1N금지", "off_first": "OFF우선", "team_balance_enable": "팀밸런스"}, "masked": {}},
    {"m": "DELETE", "p": "/roster/config/{config_id}", "page": "근무표만들기", "section": "설정 프리셋 관리",
     "action": "저장 설정 프리셋 미노출", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/roster_create/async", "page": "근무표만들기", "section": "생성 실행",
     "action": "근무표 생성 요청(비동기)", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "group_id": "병동", "config_id": "설정ID",
                "grade_strategy": "직급전략", "distribution_mode": "분배모드", "advanced_inference": "고급추론",
                "not_one_night": "1N금지", "use_fixed_wanted": "확정원티드"}, "masked": {}},
    {"m": "POST", "p": "/roster_create/generate", "page": "근무표만들기", "section": "생성 실행",
     "action": "근무표 생성(동기)", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "group_id": "병동", "config_id": "설정ID",
                "grade_strategy": "직급전략"}, "masked": {}},
    {"m": "POST", "p": "/roster_create/hold_generate", "page": "근무표만들기", "section": "생성 실행",
     "action": "고정 셀 반영 근무표 생성", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "config_id": "설정ID"}, "masked": {}},
    {"m": "POST", "p": "/roster_create/apply-resolution", "page": "근무표만들기", "section": "재생성(제약완화)",
     "action": "infeasibility 해결 적용 후 재생성", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "apply": "설정delta", "option_id": "옵션", "persist": "영구반영"},
     "masked": {}},
    {"m": "POST", "p": "/roster/request", "page": "근무표만들기", "section": "생성 요청",
     "action": "수간호사 근무표 생성 요청 기록", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "group_id": "병동", "config_id": "설정ID"}, "masked": {}},
    {"m": "POST", "p": "/roster/publish", "page": "근무표보기", "section": "발행",
     "action": "근무표 발행(마감)", "shape": "dict",
     "fields": {"schedule_id": "스케줄", "issue_comment": "발행코멘트"}, "masked": {}},
    {"m": "POST", "p": "/roster/unpublish", "page": "근무표보기", "section": "발행",
     "action": "근무표 발행 취소(마감 철회)", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/roster/save", "page": "근무표보기", "section": "편집 저장",
     "action": "근무표 셀 수동편집 저장", "shape": "dict",
     "fields": {"year": "연도", "month": "월", "schedule_id": "스케줄", "memo": "메모"}, "masked": {}},
    {"m": "DELETE", "p": "/roster/{schedule_id}", "page": "근무표보기", "section": "버전 관리",
     "action": "근무표(버전) 삭제·숨김", "shape": "none", "fields": {}, "masked": {}},
    {"m": "PATCH", "p": "/roster/{schedule_id}/name", "page": "근무표보기", "section": "버전 관리",
     "action": "근무표 버전 이름 변경", "shape": "dict", "fields": {"name": "새이름"}, "masked": {}},
    {"m": "POST", "p": "/roster/copy/{source_schedule_id}", "page": "근무표보기", "section": "버전 관리",
     "action": "근무표 새 버전으로 복사", "shape": "dict", "fields": {"new_name": "새이름"}, "masked": {}},
    {"m": "POST", "p": "/roster/create-empty", "page": "근무표보기", "section": "버전 관리",
     "action": "빈 근무표 신규 버전 생성", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/roster/create-with-weekly-off", "page": "근무표보기", "section": "버전 관리",
     "action": "주휴 포함 빈 근무표 생성", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/roster/validate", "page": "근무표보기", "section": "검증",
     "action": "근무표 제약 위반 검증", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {},
     "readonly": True},
    {"m": "POST", "p": "/roster/replacement/recommend", "page": "근무표보기", "section": "대체 추천",
     "action": "대체·교체 간호사 추천", "shape": "dict",
     "fields": {"schedule_id": "스케줄", "mode": "모드", "target_nurse_id": "결원간호사", "top_k": "추천수"},
     "masked": {}, "readonly": True},
    {"m": "POST", "p": "/roster/shares/schedules/{schedule_id}", "page": "근무표보기", "section": "공유",
     "action": "근무표 공유 링크 생성", "shape": "dict",
     "fields": {"title": "제목", "expires_in_days": "만료일수"}, "masked": {}},
    {"m": "POST", "p": "/roster/shares/schedules/{schedule_id}/capture", "page": "근무표보기", "section": "공유",
     "action": "캡처 이미지 공유 링크 생성", "shape": "dict",
     "fields": {"title": "제목", "expires_in_days": "만료일수"}, "masked": {}},
    {"m": "POST", "p": "/roster/shares/schedules/{schedule_id}/auto", "page": "근무표보기", "section": "공유",
     "action": "자동 이미지 공유 링크 생성", "shape": "dict", "fields": {"title": "제목"}, "masked": {}},
    {"m": "POST", "p": "/roster/shares/schedules/{schedule_id}/upload", "page": "근무표보기", "section": "공유",
     "action": "이미지 업로드 후 공유 링크 생성", "shape": "multipart", "fields": {"title": "제목"}, "masked": {}},
    {"m": "DELETE", "p": "/roster/shares/{share_token}", "page": "근무표보기", "section": "공유",
     "action": "근무표 공유 링크 해제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/groups/{group_id}/roster/precheck", "page": "근무표만들기", "section": "사전 검사",
     "action": "생성 전 프리체크", "shape": "dict", "fields": {"num_days": "월일수"}, "masked": {}, "readonly": True},
    {"m": "POST", "p": "/shifts/add", "page": "설정(근무코드)", "section": "근무코드 CRUD",
     "action": "근무코드 추가", "shape": "dict",
     "fields": {"shift_id": "근무코드", "name": "코드명", "color": "색상", "type": "유형",
                "default_shift": "기본매핑", "start_time": "시작", "end_time": "종료"}, "masked": {}},
    {"m": "POST", "p": "/shifts/update", "page": "설정(근무코드)", "section": "근무코드 CRUD",
     "action": "근무코드 수정", "shape": "dict",
     "fields": {"id": "PK", "shift_id": "근무코드", "name": "코드명", "color": "색상", "type": "유형",
                "default_shift": "기본매핑"}, "masked": {}},
    {"m": "POST", "p": "/shifts/remove", "page": "설정(근무코드)", "section": "근무코드 CRUD",
     "action": "근무코드 삭제", "shape": "dict", "fields": {"shift_id": "근무코드"}, "masked": {}},
    {"m": "POST", "p": "/shifts/move", "page": "설정(근무코드)", "section": "근무코드 CRUD",
     "action": "근무코드 순서 변경", "shape": "dict", "fields": {"shift_id": "근무코드", "new_sequence": "새순서"},
     "masked": {}},
    {"m": "POST", "p": "/shifts/import-to-group", "page": "설정(근무코드)", "section": "근무코드 가져오기",
     "action": "타 병동 근무코드 가져오기", "shape": "dict", "fields": {"shift_ids": "코드목록", "group_id": "병동"},
     "masked": {}},
    {"m": "POST", "p": "/shifts/upload-validate", "page": "설정(근무코드)", "section": "엑셀 일괄",
     "action": "근무코드 엑셀 업로드 검증", "shape": "multipart", "fields": {}, "masked": {}, "readonly": True},
    {"m": "POST", "p": "/shifts/upload-confirm", "page": "설정(근무코드)", "section": "엑셀 일괄",
     "action": "근무코드 엑셀 확정 저장", "shape": "list_in_key:rows", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/shift-manage/save", "page": "설정(근무코드)", "section": "시프트 관리",
     "action": "시프트 관리 슬롯 저장", "shape": "dict", "fields": {"class_name": "클래스"}, "masked": {}},
    {"m": "PUT", "p": "/daily-shift", "page": "근무표만들기", "section": "일자별 근무인원",
     "action": "월 일자별 근무인원 저장", "shape": "dict",
     "fields": {"group_id": "병동", "year": "연도", "month": "월", "max_enabled": "상한사용",
                "apply_globally": "템플릿동기화"}, "masked": {}},
    {"m": "POST", "p": "/constraint_impact/preview_adjustments", "page": "근무표만들기", "section": "제약 조정",
     "action": "제약 조정 미리보기", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {},
     "readonly": True},
    {"m": "POST", "p": "/preferences", "page": "희망근무", "section": "선호 입력",
     "action": "희망근무 초안 임시저장", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {}},
    {"m": "POST", "p": "/preferences/submit", "page": "희망근무", "section": "선호 제출",
     "action": "희망근무 최종 제출", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {}},
    {"m": "POST", "p": "/preferences/submit/empty", "page": "희망근무", "section": "선호 제출",
     "action": "빈 희망근무 제출", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {}},
    {"m": "POST", "p": "/preferences/retract", "page": "희망근무", "section": "선호 제출",
     "action": "희망근무 제출 철회", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {}},

    # ────────── 원티드 · 멤버 · 설정 · 알림 · 기타 (wanted/member/setting/groups/auth/push/message/contact/sticker) ──────────
    {"m": "POST", "p": "/wanted/request", "page": "원티드", "section": "작성 요청",
     "action": "원티드 작성기간 오픈", "shape": "dict", "fields": {"year": "연도", "month": "월", "exp_date": "마감일"},
     "masked": {}},
    {"m": "PATCH", "p": "/wanted/close", "page": "원티드", "section": "마감", "action": "원티드 작성 마감",
     "shape": "none", "fields": {}, "masked": {}},
    {"m": "PATCH", "p": "/wanted/deadline", "page": "원티드", "section": "마감일 변경",
     "action": "원티드 마감일 변경", "shape": "dict", "fields": {"year": "연도", "month": "월", "exp_date": "마감일"},
     "masked": {}},
    {"m": "POST", "p": "/wanted/invoke", "page": "원티드", "section": "AI 반영",
     "action": "자연어 원티드 요청 분석·반영", "shape": "dict", "fields": {"year": "연도", "month": "월"}, "masked": {}},
    {"m": "POST", "p": "/wanted/config", "page": "원티드", "section": "일자별 제한 설정",
     "action": "원티드 제한 설정 저장", "shape": "list_root",
     "fields": {"target_date": "대상일자", "max_requests": "최대요청", "shift_type": "근무타입"}, "masked": {}},
    {"m": "DELETE", "p": "/wanted/config", "page": "원티드", "section": "일자별 제한 삭제",
     "action": "원티드 제한 설정 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "DELETE", "p": "/wanted/config/toggle", "page": "원티드", "section": "제한 월 일괄삭제",
     "action": "해당 월 원티드 설정 전체 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/wanted/adjustment", "page": "원티드", "section": "확정 원티드 저장",
     "action": "확정 원티드(조정판) 저장", "shape": "list_in_key:entries", "item_id": "nurse_id",
     "fields": {"shift_date": "근무일", "shift_id": "근무코드", "is_applied": "적용여부", "reason": "사유"},
     "masked": {}},
    {"m": "PATCH", "p": "/wanted/adjustment/entry/{entry_id}/toggle", "page": "원티드",
     "section": "확정 원티드 토글", "action": "확정 원티드 항목 적용 토글", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/wanted/adjustment/{year}/{month}/reset", "page": "원티드", "section": "확정 원티드 재설정",
     "action": "해당 월 확정 원티드 삭제·복원", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/wanted/delete-excess-off/{nurse_id}", "page": "원티드", "section": "초과 OFF 삭제",
     "action": "간호사 초과 OFF 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/wanted/validate-limits", "page": "원티드", "section": "제한 검증",
     "action": "원티드 요청 제한 검증", "shape": "none", "fields": {}, "masked": {}, "readonly": True},
    {"m": "POST", "p": "/groups", "page": "설정", "section": "병동(그룹) 생성", "action": "병동(그룹) 생성",
     "shape": "dict", "fields": {"group_name": "병동명"}, "masked": {}},
    {"m": "PATCH", "p": "/groups/{group_id}", "page": "설정", "section": "병동(그룹) 이름 수정",
     "action": "병동(그룹) 이름 수정", "shape": "dict", "fields": {"group_name": "병동명"}, "masked": {}},
    {"m": "PUT", "p": "/groups/hn-admin", "page": "설정", "section": "그룹 관리자 지정/해제",
     "action": "그룹 관리자 권한 지정/해제", "shape": "dict", "fields": {"nurse_id": "대상", "group_ids": "관리그룹"},
     "masked": {}},
    {"m": "POST", "p": "/auth/login", "page": "인증", "section": "로그인", "action": "로그인",
     "shape": "dict", "fields": {}, "masked": {"username": "계정ID", "password": "비밀번호"}},
    {"m": "POST", "p": "/auth/logout", "page": "인증", "section": "로그아웃", "action": "로그아웃",
     "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/auth/switch-group", "page": "설정", "section": "그룹 전환", "action": "관리 그룹 전환",
     "shape": "dict", "fields": {"target_group_id": "대상그룹"}, "masked": {}},
    {"m": "POST", "p": "/auth/find_id", "page": "인증", "section": "아이디 찾기", "action": "아이디 찾기",
     "shape": "dict", "fields": {"auth_method": "인증방식"},
     "masked": {"EmployeeName": "이름", "DateOfBirth": "생년월일", "PortableTel": "휴대폰", "Email": "이메일"}},
    {"m": "POST", "p": "/auth/find_pw", "page": "인증", "section": "비밀번호 찾기", "action": "임시 비밀번호 발급",
     "shape": "dict", "fields": {"auth_method": "인증방식"},
     "masked": {"memberID": "회원ID", "EmployeeName": "이름", "receivenum": "휴대폰", "email": "이메일"}},
    {"m": "POST", "p": "/token/", "page": "인증", "section": "토큰 발급", "action": "머신 SSO 토큰 발급",
     "shape": "dict", "fields": {}, "masked": {"clientId": "클라이언트ID", "clientSecret": "시크릿"}},
    {"m": "POST", "p": "/token/login", "page": "인증", "section": "토큰 SSO 로그인", "action": "토큰 SSO 로그인",
     "shape": "dict", "fields": {}, "masked": {"token": "토큰", "MemberID": "회원ID"}},
    {"m": "PATCH", "p": "/push/read", "page": "알림", "section": "읽음처리", "action": "코드 조건 알림 읽음처리",
     "shape": "none", "fields": {}, "masked": {}},
    {"m": "PATCH", "p": "/push/read/one", "page": "알림", "section": "읽음처리", "action": "알림 1건 읽음처리",
     "shape": "dict", "fields": {"fk_idx": "알림인덱스"}, "masked": {}},
    {"m": "PATCH", "p": "/push/read/all", "page": "알림", "section": "읽음처리", "action": "알림 전체 읽음처리",
     "shape": "none", "fields": {}, "masked": {}},
    {"m": "PATCH", "p": "/push/setting", "page": "마이페이지", "section": "푸시 수신설정", "action": "푸시 수신 변경",
     "shape": "dict", "fields": {"push_yn": "수신여부"}, "masked": {}},
    {"m": "POST", "p": "/message/write", "page": "마이페이지", "section": "쪽지 전송", "action": "쪽지 전송",
     "shape": "dict", "fields": {"receiver_nurse_ids": "수신자"}, "masked": {"message": "쪽지내용"}},
    {"m": "DELETE", "p": "/message/delete/{message_id}", "page": "마이페이지", "section": "쪽지 삭제",
     "action": "쪽지 삭제", "shape": "none", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/member/edit", "page": "마이페이지", "section": "회원정보 수정", "action": "회원정보 수정",
     "shape": "dict",
     "fields": {"name": "이름", "gender": "성별", "JoinDate": "입사일", "zipcode": "우편번호",
                "mb_partName": "부서명", "OfficialTitleName": "직위명", "career": "경력", "duty": "직무",
                "is_head_nurse": "수간호사여부", "nightkeep": "나이트킵"},
     "masked": {"CurMemberPass": "현재PW", "MemberPass": "새PW", "PortableTel": "휴대폰", "Tel": "전화",
                "DateOfBirth": "생년월일", "Email": "이메일", "Address1": "주소", "Address2": "주소2"}},
    {"m": "POST", "p": "/setting/member_upload", "page": "설정", "section": "회원 엑셀 일괄등록",
     "action": "회원 엑셀 업로드·등록", "shape": "multipart", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/setting/division_upload", "page": "설정", "section": "부서 엑셀 일괄등록",
     "action": "부서 엑셀 업로드·등록", "shape": "multipart", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/setting/position_upload", "page": "설정", "section": "직위 엑셀 일괄등록",
     "action": "직위 엑셀 업로드·등록", "shape": "multipart", "fields": {}, "masked": {}},
    {"m": "POST", "p": "/contact/write", "page": "고객센터", "section": "문의 작성", "action": "고객문의 등록",
     "shape": "multipart", "fields": {"username": "작성자", "title": "제목", "contents": "내용"},
     "masked": {"PortableTel": "연락처", "Email": "이메일"}},
    {"m": "POST", "p": "/sticker/insert", "page": "홈", "section": "스티커 저장", "action": "근무표 스티커 저장",
     "shape": "dict", "fields": {"stcker_date": "스티커월"}, "masked": {}},
]

_CATALOG = [{**e, "_re": _compile(e["p"])} for e in _CATALOG_RAW]


def match(method: str, path: str) -> Optional[Tuple[dict, dict]]:
    """(entry, path_params) 반환 · 미등록이면 None. 미들웨어가 본문 tee 게이트로도 사용."""
    for e in _CATALOG:
        if e["m"] == method:
            mm = e["_re"].match(path)
            if mm:
                return e, mm.groupdict()
    return None


def _short(v: Any) -> Any:
    if isinstance(v, str):
        return v[:200]
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float) or v is None:
        return v
    if isinstance(v, list):
        return [_short(x) for x in v[:20]]
    if isinstance(v, dict):
        return {k: _short(x) for k, x in list(v.items())[:20]}
    return str(v)[:200]


def _dict_changes(entry: dict, obj: dict) -> list:
    out = []
    for k, label in entry.get("fields", {}).items():
        if isinstance(obj, dict) and k in obj:
            out.append({"field": k, "label": label, "value": _short(obj[k])})
    for k, label in entry.get("masked", {}).items():
        if isinstance(obj, dict) and k in obj and obj[k] not in (None, ""):
            out.append({"field": k, "label": label, "value": _REDACTED})
    return out


def _list_changes(entry: dict, items: list) -> list:
    id_key = entry.get("item_id")
    fields = entry.get("fields", {})
    out = []
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        per = {label: _short(it[k]) for k, label in fields.items() if k in it and it[k] is not None}
        if per:
            row = {"id": it.get(id_key)} if id_key else {}
            row["set"] = per
            out.append(row)
    return out


def _extract_items(entry: dict, body_obj: Any) -> Optional[list]:
    shape = entry.get("shape", "dict")
    if shape == "list_root":
        return body_obj if isinstance(body_obj, list) else []
    if shape.startswith("list_in_key:"):
        key = shape.split(":", 1)[1]
        v = body_obj.get(key) if isinstance(body_obj, dict) else None
        return v if isinstance(v, list) else []
    return None


def enrich(entry: dict, path_params: dict, body_obj: Any) -> dict:
    """매칭 항목 + 파싱된 본문 → 로그 보강 필드(page/section/action/target/changes/summary)."""
    info = {"page": entry["page"], "section": entry["section"], "action": entry["action"]}
    if path_params:
        info["target"] = path_params
    items = _extract_items(entry, body_obj)
    if items is not None:
        info["count"] = len(items)
        info["changes"] = _list_changes(entry, items)
    else:
        info["changes"] = _dict_changes(entry, body_obj if isinstance(body_obj, dict) else {})
    info["summary"] = _summary(entry, path_params, info.get("changes") or [], info.get("count"))
    return info


def _summary(entry: dict, path_params: dict, changes: list, count: Optional[int]) -> str:
    head = f"{entry['page']} · {entry['action']}"
    if path_params:
        head += "(" + ",".join(f"{k}={v}" for k, v in path_params.items()) + ")"
    if count is not None:
        body = f" — 대상 {count}건"
        if changes:
            first = changes[0]
            body += f" 예: {first.get('id')}→{first.get('set')}"
    elif changes:
        body = " — " + ", ".join(f"{c['label']}={c['value']}" for c in changes)
    else:
        body = ""
    return (head + body)[:800]
