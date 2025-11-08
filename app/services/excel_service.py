"""
엑셀 파일 처리 서비스
간호사 정보 엑셀 업로드/다운로드 관련 기능 제공
"""
import pandas as pd
import uuid
import tempfile
import os
import re
from typing import List, Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from datetime import datetime

from db.models import Nurse as NurseModel, Group as GroupModel, Office as OfficeModel
from schemas.auth_schema import User as UserSchema
from db.client2 import msdb_manager
from datalayer.member import Member


def create_nurse_template() -> str:
    """간호사 정보 엑셀 템플릿 생성"""
    
    # 템플릿 데이터 구조
    template_data = {
        '병동명': ['ICU', 'ICU', '응급실', '(입력 가이드)', ''],
        '식별코드': ['UUID자동생성', 'UUID자동생성', 'UUID자동생성', '(UUID는 자동생성됨)', ''],
        '계정 ID': ['nurse001', 'nurse002', 'nurse003', '(영문숫자조합)', ''],
        '이름': ['김간호', '이수간', '박일반', '(한글이름)', ''],
        '경력': [5, 10, 3, '(1이상정수)', ''],
        '직군': ['간호사', '간호사', '간호사', '(간호사)', ''],
        '직책': ['주임', '수간호사', '일반', '(주임/수간호사/일반)', ''],
        '수간호사여부': ['N', 'Y', 'N', '(Y/N)', '']
    }
    
    # DataFrame 생성
    df = pd.DataFrame(template_data)
    
    # 임시 파일 생성
    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
        template_path = tmp_file.name
    
    # 엑셀 파일로 저장
    with pd.ExcelWriter(template_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='간호사정보', index=False)
        
        # 워크시트 스타일링
        worksheet = writer.sheets['간호사정보']
        
        # 헤더 스타일 적용
        for col in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=1, column=col)
            cell.font = cell.font.copy(bold=True)
            cell.fill = cell.fill.copy(fgColor="CCCCCC")
        
        # 가이드 행 스타일 적용 (4번째 행)
        for col in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=4, column=col)
            cell.font = cell.font.copy(italic=True, color="666666")
    
    return template_path
def create_nurse_template2() -> str:
    """엑셀 템플릿2: 계정ID/이름 두 컬럼만 포함."""
    template_data = {
        '사번(필수)': ['1001', '1002', '1003', '(영문숫자조합)'],
        '계정 ID(필수)': ['nurse001', 'nurse002', 'nurse003', '(영문숫자조합)'],
        '직원명(필수)': ['김수간', '이간호', '최간호', '(한글이름)'],
        '직무(필수)': ['HN', 'AN', 'RN', ('직무코드')],
        '경력(필수)': [25, 15, 1, ('경력년수')],
        '수간호사여부(필수)': ['Y', 'N', 'N', ('수간호사여부 정보')],
        '입사일(선택)': ['2025-01-03', '', '', ('입사일 정보')],
        '적용해제일(선택)': ['2025-01-25', '', '', ('근무 표 적용해제일 정보')],

    }
    df = pd.DataFrame(template_data)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp_file:
        template_path = tmp_file.name
    with pd.ExcelWriter(template_path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='업로드2', index=False)
        ws = writer.sheets['업로드2']
        for col in range(1, len(df.columns) + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = cell.font.copy(bold=True)
    return template_path



def get_or_create_group(group_name: str, user: UserSchema, db: Session) -> Tuple[Optional[str], bool, List[str]]:
    """
    병동명을 기반으로 group_id를 찾거나 새로 생성
    Returns: (group_id, is_new_group, warnings)
    """
    warnings = []
    
    # 현재 사용자의 office_id로 해당 office의 모든 그룹 조회
    existing_group = db.query(GroupModel).filter(
        GroupModel.office_id == user.office_id,
        GroupModel.group_name == group_name
    ).first()
    
    if existing_group:
        warnings.append(f"기존 '{group_name}' 그룹에 추가됩니다.")
        return existing_group.group_id, False, warnings
    
    # 기존에 없는 그룹명인 경우 새 그룹 ID 생성
    new_group_id = generate_new_group_id(user.office_id, db)
    warnings.append(f"새로운 그룹 '{group_name}'이 생성됩니다.")
    return new_group_id, True, warnings


def generate_new_group_id(office_id: str, db: Session) -> str:
    """office_id + 001, 002, 003... 형태로 새 그룹 ID 생성"""
    
    # 해당 office의 기존 그룹들 중 최대 번호 찾기
    existing_groups = db.query(GroupModel).filter(
        GroupModel.office_id == office_id,
        GroupModel.group_id.like(f"{office_id}%")
    ).all()
    
    max_number = 0
    for group in existing_groups:
        try:
            # office_id 뒤의 숫자 부분 추출
            number_part = group.group_id.replace(office_id, "")
            if number_part.isdigit():
                max_number = max(max_number, int(number_part))
        except:
            continue
    
    # 다음 번호로 새 그룹 ID 생성
    new_number = max_number + 1
    return f"{office_id}{new_number:03d}"


def create_new_group(group_name: str, group_id: str, user: UserSchema, db: Session) -> str:
    """새로운 그룹 생성"""
    
    new_group = GroupModel(
        group_id=group_id,
        office_id=user.office_id,
        group_name=group_name
    )
    
    db.add(new_group)
    db.flush()  # DB에 즉시 반영하되 커밋은 나중에
    
    return group_id


def get_next_sequence(group_id: str, active_status: int, db: Session) -> int:
    """해당 그룹의 특정 active 상태에서 다음 sequence 번호 반환"""
    
    max_sequence = db.query(func.max(NurseModel.sequence)).filter(
        NurseModel.group_id == group_id,
        NurseModel.active == active_status
    ).scalar()
    
    return (max_sequence or 0) + 1


def process_excel_upload(file_path: str, user: UserSchema, db: Session) -> Dict[str, Any]:
    """엑셀 파일 업로드 처리 및 검증"""
    
    try:
        # 엑셀 파일 읽기
        df = pd.read_excel(file_path, sheet_name=0)
        
        # 빈 행 및 가이드 행 제거
        df = df.dropna(how='all')  # 모든 컬럼이 비어있는 행 제거
        df = df[~df.iloc[:, 0].astype(str).str.contains('입력 가이드', na=False)]  # 가이드 행 제거
        
        # 최대 행 수 검증
        if len(df) > 1000:
            raise ValueError("최대 1000행까지만 업로드 가능합니다.")
        # 컬럼 매핑
        column_mapping = {
            '병동명': 'group_name',
            '식별코드': 'nurse_id', 
            '계정 ID': 'account_id',
            '이름': 'name',
            '경력': 'experience',
            '직군': 'role',
            '직책': 'level_',
            '수간호사여부': 'is_head_nurse'
        }
        # 컬럼명 유연 매핑 (유사한 이름 인식)
        flexible_mapping = {}
        for excel_col in df.columns:
            excel_col_clean = str(excel_col).strip()
            for standard_col, db_field in column_mapping.items():
                if (excel_col_clean == standard_col or 
                    excel_col_clean in ['병동', '부서'] and standard_col == '병동명' or
                    excel_col_clean in ['ID', '아이디'] and standard_col == '계정 ID' or
                    excel_col_clean in ['성명', '간호사명'] and standard_col == '이름' or
                    excel_col_clean in ['년차', '경력년수'] and standard_col == '경력' or
                    excel_col_clean in ['수간호사', '헤드너스'] and standard_col == '수간호사여부'):
                    flexible_mapping[excel_col] = db_field
                    break
        # 필수 컬럼 확인
        required_fields = ['group_name', 'account_id', 'name', 'experience', 'role', 'level_', 'is_head_nurse']
        missing_fields = [field for field in required_fields if field not in flexible_mapping.values()]
        if missing_fields:
            missing_korean = []
            field_korean_map = {
                'group_name': '병동명',
                'account_id': '계정 ID', 
                'name': '이름',
                'experience': '경력',
                'role': '직군',
                'level_': '직책',
                'is_head_nurse': '수간호사여부'
            }
            for field in missing_fields:
                missing_korean.append(field_korean_map.get(field, field))
            raise ValueError(f"필수 컬럼이 누락되었습니다: {', '.join(missing_korean)}")
            
        # 병동명별 그룹 정보 수집
        # 동일한 병동명이 여러 병원(office)에서 존재할 수 있으므로, 엑셀에 오피스/지점 컬럼이 존재하는 경우
        # 먼저 현재 사용자 office_id와 일치하는 행으로 필터링한다.
        office_col = None
        for c in ['office_id', 'Office ID', '오피스ID', '병원ID', '병원코드', '기관ID', '지점ID']:
            if c in df.columns:
                office_col = c
                break
        if office_col:
            df_filtered = df[df[office_col].astype(str).str.strip() == str(user.office_id)]
        else:
            df_filtered = df

        unique_groups = df_filtered[get_excel_column_by_field('group_name', flexible_mapping)].dropna().unique()
        group_info = {}
        new_groups_needed = []
        
        for group_name in unique_groups:
            group_name = str(group_name).strip()
            if not group_name:
                continue
                
            group_id, is_new, warnings = get_or_create_group(group_name, user, db)
            group_info[group_name] = {
                'group_id': group_id,
                'is_new': is_new,
                'warnings': warnings
            }
            
            if is_new:
                new_groups_needed.append(group_name)
        
        # 그룹별 sequence 카운터 초기화 (활성 상태 기준)
        group_sequence_counters = {}
        for group_name, info in group_info.items():
            group_id = info['group_id']
            if info['is_new']:
                # 새 그룹인 경우 1부터 시작
                group_sequence_counters[group_id] = 1
            else:
                # 기존 그룹인 경우 활성 상태(active=1)의 다음 sequence 가져오기
                group_sequence_counters[group_id] = get_next_sequence(group_id, 1, db)
        
        # 데이터 변환
        processed_data = []
        validation_results = []
        
        for idx, row in df.iterrows():
            try:
                # 병동명 처리
                group_name = str(row[get_excel_column_by_field('group_name', flexible_mapping)]).strip()
                group_data = group_info.get(group_name, {})
                group_id = group_data.get('group_id')
                
                # sequence 할당
                sequence = group_sequence_counters.get(group_id, 0)
                group_sequence_counters[group_id] = sequence + 1
                
                # 기본 데이터 변환 (nurses.office_id 함께 저장)
                nurse_data = {
                    # 'group_name': group_name,
                    'group_id': group_id,
                    'office_id': user.office_id,
                    'nurse_id': str(uuid.uuid4()) if pd.isna(row.get('식별코드')) or str(row.get('식별코드')).strip() == 'UUID자동생성' else str(row.get('식별코드')),
                    'account_id': str(row[get_excel_column_by_field('account_id', flexible_mapping)]).strip(),
                    'name': str(row[get_excel_column_by_field('name', flexible_mapping)]).strip(),
                    'experience': int(float(row[get_excel_column_by_field('experience', flexible_mapping)])),
                    'role': str(row[get_excel_column_by_field('role', flexible_mapping)]).strip(),
                    'level_': str(row[get_excel_column_by_field('level_', flexible_mapping)]).strip(),
                    'is_head_nurse': parse_boolean(row[get_excel_column_by_field('is_head_nurse', flexible_mapping)]),
                    'is_night_nurse': False,  # 기본값
                    'personal_off_adjustment': 0,  # 기본값
                    'preceptor_id': None,  # 기본값
                    'joining_date': None,  # 기본값
                    'resignation_date': None,  # 기본값
                    'sequence': sequence,
                    'active': 1  # 엑셀 업로드는 기본적으로 활성 상태
                }
                
                # 개별 행 검증
                row_validation = validate_single_row(group_name, nurse_data, user, db)
                
                # 그룹 상태 정보 추가
                row_validation['warnings'].extend(group_data.get('warnings', []))
                row_validation['is_new_group'] = group_data.get('is_new', False)
                row_validation['group_name'] = group_name
                
                processed_data.append(nurse_data)
                validation_results.append(row_validation)
                
            except Exception as e:
                # 행별 오류 처리
                error_data = {
                    'row_index': idx + 2,  # 엑셀 행 번호 (헤더 포함)
                    'error': str(e),
                    'is_valid': False,
                    'errors': [str(e)],
                    'warnings': [],
                    'is_new_group': False
                }
                validation_results.append(error_data)
                processed_data.append(None)
        
        # 전체 검증 결과 요약
        valid_count = sum(1 for result in validation_results if result.get('is_valid', False))
        error_count = len(validation_results) - valid_count
        overwrite_count = sum(1 for result in validation_results if result.get('is_overwrite', False))
        new_group_count = len(new_groups_needed)
        
        return {
            'success': True,
            'data': processed_data,
            'validation_results': validation_results,
            'new_groups_needed': new_groups_needed,
            'summary': {
                'total': len(validation_results),
                'valid': valid_count,
                'error': error_count,
                'overwrite': overwrite_count,
                'new_groups': new_group_count
            }
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'data': [],
            'validation_results': [],
            'new_groups_needed': [],
            'summary': {'total': 0, 'valid': 0, 'error': 0, 'overwrite': 0, 'new_groups': 0}
        }


# def process_excel_upload2(file_path: str, user: UserSchema, db: Session, target_group_id: Optional[str] = None) -> Dict[str, Any]:
#     """
#     엑셀 업로드2 처리 함수.

#     요약: 한 번의 MSSQL 조회로 해당 오피스의 허용 계정 목록을 가져온 뒤,
#     엑셀의 각 `account_id`를 검증하고, 통과 시 `nurses`에 등록/업데이트합니다.

#     인자
#     - file_path: 업로드된 엑셀 임시 경로
#     - user: 현재 사용자
#     - db: 세션
#     - target_group_id: ADM이 선택한 대상 그룹 ID (없으면 user.group_id 사용)

#     반환
#     - { success: int, errors: [{row, reason}], rows: [{account_id, name}] }

#     예시: 허용 문자 제약 정규식은 r'^[A-Za-z0-9]+$' (예: 'nurse01' OK, 'nurse_01' X)
#     """
#     try:
#         df = pd.read_excel(file_path, sheet_name=0)
#         df = df.dropna(how='all')
#         if len(df) > 2000:
#             raise ValueError("최대 2000행까지만 업로드 가능합니다.")
#         # 컬럼 추출(유연 매핑)
#         def find_col(candidates: list[str]) -> str:
#             for c in df.columns:
#                 cc = str(c).strip()
#                 if cc in candidates:
#                     return c
#             raise ValueError(f"필수 컬럼 누락: {candidates}")
#         def find_col_optional(candidates: list[str]) -> Optional[str]:
#             for c in df.columns:
#                 cc = str(c).strip()
#                 if cc in candidates:
#                     return c
#             return None

#         # 신규 템플릿2 컬럼 매핑
#         col_empnum = find_col(['사번(필수)','사번','EmpNum','emp_num'])
#         col_acc = find_col(['계정 ID(필수)','계정 ID','ID','아이디','account_id'])
#         col_name = find_col(['직원명(필수)','이름','성명','name'])
#         col_role = find_col(['직무(필수)','직무','role'])
#         col_exp = find_col(['경력(필수)','경력','experience'])
#         col_head = find_col(['수간호사여부(필수)','수간호사여부','is_head_nurse'])
#         col_join = find_col_optional(['입사일(선택)','입사일','joining_date'])
#         col_resi = find_col_optional(['적용해제일(선택)','적용해제일','퇴사일','resignation_date'])

#         office_id = user.office_id
#         print('office_id', office_id)
#         print('target_group_id', target_group_id)
#         # 대상 그룹 결정: 파라미터 우선, 없으면 현재 사용자 그룹
#         group_id_for_insert = (target_group_id or getattr(user, 'group_id', None))
#         if not group_id_for_insert:
#             return {"success": 0, "errors": [{"row": 0, "reason": "group_id가 필요합니다. ADM은 선택한 병동의 group_id를 쿼리로 전달하세요."}], "rows": []}

#         # 1) 허용 계정 목록을 한 번에 조회(성능 개선)
#         rows = msdb_manager.fetch_all(Member.member_accounts_by_office(), params=(str(office_id),))
#         # account_id → (name, EmpAuthGbn)
#         allowed: dict[str, tuple[str, str | None]] = {}
#         for r in rows or []:
#             acc = str(r.get('account_id', '')).strip()
#             nm = str(r.get('name', '')).strip()
#             auth = r.get('EmpAuthGbn')
#             if acc:
#                 allowed[acc] = (nm, auth)
#         success = 0
#         errors: list[dict] = []
#         rows_out: list[dict] = []

#         # 2) 행별 검증 및 누적 등록
#         for i, row in df.iterrows():
#             ridx = int(i) + 2  # 헤더 포함 행 번호
#             emp_num = str(row.get(col_empnum, '')).strip()
#             account_id = str(row.get(col_acc, '')).strip()
#             # 이름은 엑셀 값 우선, 없으면 DB조회값 사용
#             name = str(row.get(col_name, '')).strip()
#             # nurse_id = str(row.get(col_nurse_id, '')).strip()
#             role = str(row.get(col_role, '')).strip()
#             # 경험치는 숫자 변환
#             try:
#                 experience_val = int(float(str(row.get(col_exp, '')).strip()))
#             except Exception:
#                 experience_val = None
#             head_raw = str(row.get(col_head, '')).strip().upper()
#             head_bool = True if head_raw in ['Y','YES','1','TRUE','T'] else False
#             # 날짜 파싱
#             def parse_dt(v):
#                 try:
#                     if pd.isna(v) or str(v).strip() == '':
#                         return None
#                     return pd.to_datetime(v, errors='coerce').to_pydatetime()
#                 except Exception:
#                     return None
#             joining_dt = parse_dt(row.get(col_join)) if col_join else None
#             resignation_dt = parse_dt(row.get(col_resi)) if col_resi else None
#             if not account_id:
#                 errors.append({"row": ridx, "reason": "계정 ID 누락"})
#                 continue
#             # 공백 제외 모든 문자 허용(최대 50자) → DB 컬럼 VARCHAR(50) 기준
#             if not re.match(r'^\S{1,50}$', account_id):
#                 errors.append({"row": ridx, "reason": "계정 ID 형식 오류: 공백 제외 최대 50자까지 허용됩니다."})
#                 continue
#             # 허용 계정 존재 검사 (사전 조회 결과 사용)
#             if account_id not in allowed:
#                 errors.append({"row": ridx, "reason": f"허용되지 않은 계정 또는 오피스 불일치: office_id={office_id}, account_id={account_id}"})
#                 continue
#             db_name, db_auth = allowed[account_id]
#             final_name = name or db_name
#             if not final_name:
#                 errors.append({"row": ridx, "reason": "이름을 확인할 수 없습니다. 엑셀 또는 원장(member)에서 이름이 필요합니다."})
#                 continue

#             # 이미 존재하는 nurse 여부 확인 (account_id 기준)
#             existing = db.query(NurseModel).filter(NurseModel.account_id == account_id).first()
#             if existing:
#                 # 최소 업데이트: 이름만 동기화
#                 if final_name and existing.name != final_name:
#                     existing.name = final_name
#                 # 확장 필드 업데이트
#                 # emp_num/role 기본값 처리
#                 setattr(existing, 'emp_num', (emp_num if emp_num is not None else ''))
#                 existing.role = (role if role is not None else '')
#                 if experience_val is not None:
#                     existing.experience = experience_val
#                 existing.is_head_nurse = head_bool
#                 # office_id는 ADM의 office로 통일
#                 try:
#                     existing.office_id = user.office_id
#                 except Exception:
#                     pass
#                 if joining_dt:
#                     existing.joining_date = joining_dt
#                 if resignation_dt:
#                     existing.resignation_date = resignation_dt
#                 # 그룹 이동은 업로드2의 범위를 넘으므로 변경하지 않음
#             else:
#                 # 신규 생성: 최소 필수값 채우기
#                 print('col_nurse_id', col_nurse_id)
#                 print('uuid.uuid4()', str(row.get(col_nurse_id, uuid.uuid4())).strip())
#                 seq_next = get_next_sequence(group_id_for_insert, 1, db)
#                 new_nurse = NurseModel(
#                     # nurse_id=str(uuid.uuid4()),
#                     nurse_id=str(row.get(col_nurse_id, uuid.uuid4())).strip(),
#                     group_id=group_id_for_insert,
#                     office_id=user.office_id,
#                     emp_num=(emp_num if emp_num is not None else ''),
#                     account_id=account_id,
#                     name=final_name or account_id,
#                     experience=(experience_val if experience_val is not None else 1),
#                     role=(role if role is not None else ''),
#                     level_='일반',
#                     is_head_nurse=head_bool,
#                     emp_auth_gbn=db_auth,
#                     is_night_nurse=0,
#                     personal_off_adjustment=0,
#                     preceptor_id=None,
#                     joining_date=joining_dt,
#                     resignation_date=resignation_dt,
#                     sequence=seq_next,
#                     active=1,
#                 )
#                 db.add(new_nurse)

#             success += 1
#             rows_out.append({"account_id": account_id, "name": final_name or db_name})

#         # 3) 저장
#         try:
#             db.commit()
#         except Exception as e:
#             db.rollback()
#             errors.append({"row": 0, "reason": f"DB 커밋 실패: {str(e)}"})

#         return {"success": success, "errors": errors, "rows": rows_out}
#     except Exception as e:
#         return {"success": 0, "errors": [{"row": 0, "reason": str(e)}], "rows": []}


def upload2_validate(file_path: str, user: UserSchema, db: Session) -> Dict[str, Any]:
    """업로드2: 파일을 검증만 수행하고, 정규화된 행과 오류를 반환한다.

    - 행별 오류: 포맷/타입/허용 계정/필수값 등
    - 전역 오류: 수간호사 최소 1명, DB 중복 계정 등
    """
    try:
        df = pd.read_excel(file_path, sheet_name=0)
        df = df.dropna(how='all')
        if len(df) > 2000:
            raise ValueError("최대 2000행까지만 업로드 가능합니다.")

        def find_col(candidates: list[str]) -> str:
            for c in df.columns:
                cc = str(c).strip()
                if cc in candidates:
                    return c
            raise ValueError(f"필수 컬럼 누락: {candidates}")
        def find_col_optional(candidates: list[str]) -> Optional[str]:
            for c in df.columns:
                cc = str(c).strip()
                if cc in candidates:
                    return c
            return None

        col_empnum = find_col(['사번(필수)','사번','EmpNum','emp_num'])
        col_acc = find_col(['계정 ID(필수)','계정 ID','ID','아이디','account_id'])
        col_name = find_col(['직원명(필수)','이름','성명','name'])
        col_role = find_col(['직무(필수)','직무','role'])
        col_exp = find_col(['경력(필수)','경력','experience'])
        col_head = find_col(['수간호사여부(필수)','수간호사여부','is_head_nurse'])
        col_join = find_col_optional(['입사일(선택)','입사일','joining_date'])
        col_resi = find_col_optional(['적용해제일(선택)','적용해제일','퇴사일','resignation_date'])
        
        office_id = user.office_id
        rows_allowed = msdb_manager.fetch_all(Member.member_accounts_by_office(), params=(str(office_id),))
        allowed: dict[str, tuple[str, str | None]] = {}
        for r in rows_allowed or []:
            acc = str(r.get('account_id', '')).strip()
            nm = str(r.get('name', '')).strip()
            auth = r.get('EmpAuthGbn')
            nurse_id = str(r.get('nurse_id', uuid.uuid4())).strip()
            if acc:
                allowed[acc] = (nm, auth, nurse_id)
        normalized: list[dict] = []
        errors: list[dict] = []
        head_count = 0
        acc_in_file: set[str] = set()

        def parse_dt(v):
            try:
                if pd.isna(v) or str(v).strip() == '':
                    return None
                return pd.to_datetime(v, errors='coerce').to_pydatetime()
            except Exception:
                return None

        for i, row in df.iterrows():
            ridx = int(i) + 2
            row_errs: list[str] = []
            emp_num_val = row.get(col_empnum)
            emp_num = '' if pd.isna(emp_num_val) else str(emp_num_val).strip()
            account_id = str(row.get(col_acc, '')).strip()
            name = str(row.get(col_name, '')).strip()
            role_val = row.get(col_role)
            role = 'RN' if pd.isna(role_val) or not str(role_val).strip() else str(role_val).strip()

            # experience: 비어있으면 None 허용, 값이 있으면 숫자만 허용
            exp_val = None
            raw_exp_val = row.get(col_exp, 1)
            # print('raw_exp_val', raw_exp_val)
            import math
            # NaN/None/빈문자/문자 'nan' 전부 필터링
            if raw_exp_val not in ['', None] and not (isinstance(raw_exp_val, float) and math.isnan(raw_exp_val)) and str(raw_exp_val).lower() != 'nan':
                try:
                    exp_val = int(float(str(raw_exp_val).strip()))
                except Exception:
                    # print('raw_exp_val', raw_exp_val)
                    row_errs.append("경력은 숫자여야 합니다. 예: 1, 3, 10")

            head_raw = str(row.get(col_head, '')).strip().upper()
            is_head = True if head_raw in ['Y','YES','1','TRUE','T'] else False
            if is_head:
                head_count += 1

            joining_raw = row.get(col_join) if col_join else None
            resignation_raw = row.get(col_resi) if col_resi else None
            # 날짜: None 허용, 숫자 허용, 문자열인 경우에만 YYYY-MM-DD 형식 검증
            joining_dt = parse_dt(joining_raw) if col_join else None
            if isinstance(joining_raw, str) and joining_raw.strip() and not re.match(r'^\d{4}-\d{2}-\d{2}$', joining_raw.strip()):
                row_errs.append("입사일 형식이 올바르지 않습니다. 예: 2025-10-10")
            resignation_dt = parse_dt(resignation_raw) if col_resi else None
            if isinstance(resignation_raw, str) and resignation_raw.strip() and not re.match(r'^\d{4}-\d{2}-\d{2}$', resignation_raw.strip()):
                row_errs.append("적용해제일 형식이 올바르지 않습니다. 예: 2025-10-10")

            if not account_id:
                row_errs.append("계정 ID 누락")
            elif not re.match(r'^\S{1,50}$', account_id):
                row_errs.append("계정 ID 형식 오류: 공백 제외 최대 50자까지 허용됩니다.")
            elif account_id not in allowed:
                row_errs.append(f"허용되지 않은 계정 또는 오피스 불일치: office_id={office_id}, account_id={account_id}")

            if not name:
                # 원장에서 이름 보강
                name = (allowed.get(account_id, ("", None))[0] if account_id in allowed else "")
                if not name:
                    row_errs.append("직원명 누락")

            if not role:
                row_errs.append("직무 누락")

            if account_id in acc_in_file:
                row_errs.append("엑셀 내 중복 계정 ID")
            else:
                acc_in_file.add(account_id)

            normalized.append({
                'row': ridx,
                'emp_num': emp_num or None,
                'account_id': account_id,
                'name': name,
                'role': role,
                'experience': exp_val,
                'is_head_nurse': is_head,
                'joining_date': joining_dt.isoformat() if joining_dt else None,
                'resignation_date': resignation_dt.isoformat() if resignation_dt else None,
                'nurse_id': allowed[account_id][2],
            })
            if row_errs:
                errors.append({'row': ridx, 'reason': '; '.join(row_errs)})

        # 전역 검증: 수간호사 최소 1명
        if head_count == 0:
            errors.append({'row': 0, 'reason': '수간호사는 최소 1명 이상이어야 합니다.'})

        # 전역 검증: DB 중복 계정
        if acc_in_file:
            existing = db.query(NurseModel.account_id).filter(NurseModel.account_id.in_(list(acc_in_file))).all()
            if existing:
                for (acc,) in existing:
                    errors.append({'row': 0, 'reason': f'이미 존재하는 계정 ID: {acc}'})

        return {
            'success': 0 if errors else len(normalized),
            'errors': errors,
            'rows': normalized,
            'summary': {
                'total': len(normalized),
                'head_nurses': head_count,
                'error_count': len(errors),
            }
        }
    except Exception as e:
        return {"success": 0, "errors": [{"row": 0, "reason": str(e)}], "rows": [], 'summary': {'total': 0, 'head_nurses': 0, 'error_count': 1}}


def upload2_confirm(rows: List[Dict[str, Any]], user: UserSchema, db: Session, target_group_id: str) -> Dict[str, Any]:
    """업로드2: 검증된 행을 저장한다. 오류 포함 행은 건너뜀."""
    try:
        if not target_group_id:
            return {"success": 0, "errors": [{"row": 0, "reason": "group_id가 필요합니다."}]}
        print('[/upload2_confirm] target_group_id', target_group_id)
        print('[/upload2_confirm] rows', rows)
        saved = 0
        updated = 0
        for item in rows:
            if not item or item.get('error'):
                continue
            
            account_id = item.get('account_id')
            name = item.get('name')
            role = item.get('role', 'RN')
            exp_val = item.get('experience', 1)
            nurse_id = item.get('nurse_id').strip()
            is_head = bool(item.get('is_head_nurse', False))
            emp_num = item.get('emp_num', '')
            jd = item.get('joining_date')
            rd = item.get('resignation_date')
            joining_dt = pd.to_datetime(jd).to_pydatetime() if jd else None
            resignation_dt = pd.to_datetime(rd).to_pydatetime() if rd else None

            existing = db.query(NurseModel).filter(NurseModel.account_id == account_id).first()
            print(1)
            if existing:
                if name and existing.name != name:
                    existing.name = name
                setattr(existing, 'emp_num', (emp_num if emp_num is not None else ''))
                existing.role = (role if role is not None else '')
                if isinstance(exp_val, int):
                    existing.experience = exp_val
                existing.is_head_nurse = is_head
                try:
                    existing.office_id = user.office_id
                except Exception:
                    pass
                if joining_dt:
                    existing.joining_date = joining_dt
                if resignation_dt:
                    existing.resignation_date = resignation_dt
                updated += 1
                continue
            print(2)
            seq_next = get_next_sequence(target_group_id, 1, db)
            print(3)
            new_nurse = NurseModel(
                nurse_id= nurse_id,
                group_id=target_group_id,
                office_id=user.office_id,
                emp_num=(emp_num if emp_num is not None else ''),
                account_id=account_id,
                name=name or account_id,
                experience=(exp_val if isinstance(exp_val, int) else 1),
                role=(role if role is not None else ''),
                level_='일반',
                is_head_nurse=is_head,
                is_night_nurse=0,
                personal_off_adjustment=0,
                preceptor_id=None,
                joining_date=joining_dt,
                resignation_date=resignation_dt,
                sequence=seq_next,
                active=1,
            )
            print(4)
            import pprint
            pprint.pprint(new_nurse.__dict__)
            db.add(new_nurse)
            saved += 1

        print(5)
        db.commit()
        print(6)
        return {"success": saved + updated, "saved": saved, "updated": updated, "errors": []}
    except Exception as e:
        print(7)
        print('error', e)
        db.rollback()
        print(8)
        return {"success": 0, "errors": [{"row": 0, "reason": f"저장 실패: {str(e)}"}]}


def get_excel_column_by_field(field: str, mapping: Dict[str, str]) -> str:
    """필드명으로 엑셀 컬럼명 찾기"""
    for excel_col, db_field in mapping.items():
        if db_field == field:
            return excel_col
    raise ValueError(f"필드 {field}에 해당하는 엑셀 컬럼을 찾을 수 없습니다.")


def parse_boolean(value: Any) -> bool:
    """다양한 형태의 불린 값 파싱"""
    if pd.isna(value):
        return False
    
    str_value = str(value).strip().upper()
    return str_value in ['Y', 'YES', 'TRUE', '1', 'T', '참', '예']


def validate_single_row(group_name: str, nurse_data: Dict[str, Any], user: UserSchema, db: Session) -> Dict[str, Any]:
    """개별 행 데이터 검증"""
    
    errors = []
    warnings = []
    is_overwrite = False
    
    # # 병동명 검증
    # if not nurse_data.get('group_name'):
    #     errors.append("병동명은 필수입니다.")
    # elif not nurse_data.get('group_id'):
    #     errors.append("유효하지 않은 병동명입니다.")
    
    # 필수 필드 검증
    if not nurse_data.get('account_id'):
        errors.append("계정 ID는 필수입니다.")
    elif not re.match(r'^\S{1,50}$', nurse_data['account_id']):
        errors.append("계정 ID 형식 오류: 공백 제외 최대 50자까지 허용됩니다.")
    
    if not nurse_data.get('name'):
        errors.append("이름은 필수입니다.")
    
    if nurse_data.get('experience', 0) < 1:
        errors.append("경력은 1년 이상이어야 합니다.")
    
    if not nurse_data.get('role'):
        errors.append("직군은 필수입니다.")
    
    if not nurse_data.get('level_'):
        errors.append("직책은 필수입니다.")
    
    # 중복 검사
    if nurse_data.get('group_id'):
        existing_nurse = db.query(NurseModel).filter(
            NurseModel.nurse_id == nurse_data['nurse_id']
        ).first()
        
        if existing_nurse:
            is_overwrite = True
            warnings.append(f"기존 간호사 '{existing_nurse.name}' 데이터를 덮어씁니다.")
        
        # 계정 ID 중복 검사 (다른 nurse_id와)
        existing_account = db.query(NurseModel).filter(
            NurseModel.account_id == nurse_data['account_id'],
            NurseModel.nurse_id != nurse_data['nurse_id']
        ).first()
        
        if existing_account:
            errors.append(f"계정 ID '{nurse_data['account_id']}'는 이미 사용 중입니다.")
    
    return {
        'is_valid': len(errors) == 0,
        'is_overwrite': is_overwrite,
        'errors': errors,
        'warnings': warnings,
        'nurse_data': nurse_data,
        'group_name': group_name
    }


def validate_excel_data(data: List[Dict[str, Any]], user: UserSchema, db: Session) -> Dict[str, Any]:
    """엑셀 데이터 전체 유효성 검증"""
    
    validation_results = []
    
    for nurse_data in data:
        if nurse_data is None:
            validation_results.append({'is_valid': False, 'errors': ['데이터 파싱 오류']})
            continue
            
        result = validate_single_row(nurse_data, user, db)
        validation_results.append(result)
    
    # 수간호사 최소 1명 검증
    head_nurses = [
        result for result in validation_results 
        if result.get('is_valid') and result.get('nurse_data', {}).get('is_head_nurse')
    ]
    
    if len(head_nurses) == 0:
        # 기존 수간호사가 있는지 확인
        existing_head_nurses = db.query(NurseModel).filter(
            NurseModel.group_id == user.group_id,
            NurseModel.is_head_nurse == True
        ).count()
        
        if existing_head_nurses == 0:
            return {
                'success': False,
                'error': '최소 1명의 수간호사가 필요합니다.',
                'validation_results': validation_results
            }
    
    valid_count = sum(1 for result in validation_results if result.get('is_valid', False))
    error_count = len(validation_results) - valid_count
    overwrite_count = sum(1 for result in validation_results if result.get('is_overwrite', False))
    
    return {
        'success': True,
        'validation_results': validation_results,
        'summary': {
            'total': len(validation_results),
            'valid': valid_count,
            'error': error_count,
            'overwrite': overwrite_count
        }
    }


def save_excel_data(data: List[Dict[str, Any]], user: UserSchema, db: Session) -> Dict[str, Any]:
    """검증된 데이터 DB 저장"""
    
    try:
        saved_count = 0
        updated_count = 0
        for nurse_data in data:
            if not nurse_data:
                continue
                
            # 기존 데이터 확인
            existing_nurse = db.query(NurseModel).filter(
                NurseModel.nurse_id == nurse_data['nurse_id']
            ).first()
            
            if existing_nurse:
                # 업데이트
                for key, value in nurse_data.items():
                    if hasattr(existing_nurse, key):
                        setattr(existing_nurse, key, value)
                existing_nurse.updated_at = datetime.now()
                updated_count += 1
            else:
                # 신규 생성
                new_nurse = NurseModel(**nurse_data)
                db.add(new_nurse)
                saved_count += 1
        
        db.commit()
        
        return {
            'success': True,
            'message': f'저장 완료: 신규 {saved_count}건, 업데이트 {updated_count}건',
            'saved_count': saved_count,
            'updated_count': updated_count
        }
        
    except Exception as e:
        db.rollback()
        return {
            'success': False,
            'error': f'저장 실패: {str(e)}'
        } 


def create_groups_and_save_data(data: List[Dict[str, Any]], new_groups_to_create: List[str], user: UserSchema, db: Session) -> Dict[str, Any]:
    """새 그룹 생성 후 데이터 저장"""
    
    try:
        # 새 그룹 생성
        created_groups = {}
        for group_name in new_groups_to_create:
            # 새 그룹 ID 생성
            new_group_id = generate_new_group_id(user.office_id, db)
            # print('new_group_id', new_group_id)
            # 그룹 생성
            create_new_group(group_name, new_group_id, user, db)
            created_groups[group_name] = new_group_id

        # 데이터의 group_id 업데이트 (이미 생성된 그룹 ID 사용)
        for nurse_data in data:
            if nurse_data:
                group_name = nurse_data.get('group_name')
                if group_name in created_groups:
                    nurse_data['group_id'] = created_groups[group_name]
        
        # 데이터 저장
        result = save_excel_data(data, user, db)
        if result['success']:
            result['created_groups'] = created_groups
            result['message'] = f"{result['message']} (새 병동 {len(created_groups)}개 생성)"
        
        return result
        
    except Exception as e:
        db.rollback()
        return {
            'success': False,
            'error': f'그룹 생성 및 저장 실패: {str(e)}'
        } 


def export_schedule_excel_bytes(schedule_id: str, current_user, db, target_group_id: str) -> bytes:
    """지정된 schedule_id의 근무표를 엑셀(xlsx) 바이트로 생성하여 반환합니다.
    
    요약: schedules, schedule_entries, nurses, shifts, shift_manage 정보를 조회하여
    example.html과 유사한 표 형태(상단 제목, 일자 헤더, 간호사별 행, D/E/N/O 합계, 일일 현황)를 구성합니다.
    
    Args:
        schedule_id: 스케줄 식별자
        current_user: 요청 사용자(권한/그룹 필터용)
        db: SQLAlchemy 세션
    
    Returns:
        bytes: 생성된 엑셀 파일의 바이트 데이터
    
    예시:
        - 2025년 9월 → "2025년 9월 근무표" 제목 표시.
        - 일수 30일이면 1~30 열 생성.
    """
    from io import BytesIO
    from datetime import date
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from db.models import Schedule, ScheduleEntry, Nurse, Shift, RosterConfig, ShiftManage

    # ───────── 1) 데이터 로드 ─────────
    schedule = db.query(Schedule).filter(
        Schedule.schedule_id == schedule_id,
        Schedule.group_id == target_group_id,
        Schedule.dropped == False
    ).first()
    if not schedule:
        raise ValueError("스케줄을 찾을 수 없습니다.")

    year, month = schedule.year, schedule.month

    # 간호사 목록(경력 desc, nurse_id asc)
    nurses = db.query(Nurse.nurse_id, Nurse.name, Nurse.experience, Nurse.sequence).filter(
        Nurse.group_id == target_group_id
    ).order_by(Nurse.sequence.asc(), Nurse.nurse_id.asc()).all()

    # 엔트리
    entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()

    # 시프트(색상 등은 엑셀에서는 텍스트 표기 중심으로 사용)
    shifts_db = db.query(Shift).filter(Shift.group_id == target_group_id).all()
    # known_shift_ids = {s.shift_id for s in shifts_db}

    # # config_version → ShiftManage 별칭 맵 작성
    # if schedule.config_id:
    #     rc = db.query(RosterConfig).filter(RosterConfig.config_id == schedule.config_id).first()
    # else:
    #     rc = db.query(RosterConfig).filter(RosterConfig.group_id == current_user.group_id).order_by(RosterConfig.created_at.desc()).first()
    # config_version = rc.config_version if rc else None

    alias_map: dict[str, str] = {}
    # if config_version:
    sm_rows = db.query(ShiftManage).filter(
        ShiftManage.office_id == current_user.office_id,
        ShiftManage.group_id == target_group_id,
        # ShiftManage.config_version == config_version
    ).all()
    for row in sm_rows:
        if not row.main_code:
            continue
        base = row.main_code.upper()
        alias_map[base] = base
        if row.codes:
            for c in row.codes:
                alias_map[str(c).upper()] = base
    alias_map.setdefault('OFF', 'O'); alias_map.setdefault('O', 'O')

    def to_base(code: str) -> str:
        if not code:
            return '-'
        u = code.upper()
        if u in alias_map:
            return alias_map[u]
        # 휴리스틱 (미등록 코드)
        if u.startswith('D'):
            return 'D'
        if u.startswith('E'):
            return 'E'
        if u.startswith('N'):
            return 'N'
        if u in ('O', 'OFF'):
            return 'O'
        return u

    # 일 수
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]

    # nurse별 일자→shift 매핑
    by_nurse: dict[str, dict[int, str]] = {}
    for e in entries:
        by_nurse.setdefault(e.nurse_id, {})[e.work_date.day] = e.shift_id
    # ───────── 2) 워크북/시트 ─────────
    wb = Workbook()
    ws = wb.active
    ws.title = "근무표"

    # 스타일
    center = Alignment(horizontal="center", vertical="center")
    header_font = Font(bold=True, size=12)
    title_font = Font(bold=True, size=20)
    thin = Side(style="thin", color="000000")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    gray_fill = PatternFill("solid", fgColor="DEE2E6")

    # ───────── 3) 제목 영역 ─────────
    title = f"{year}년 {month}월 근무표"
    # 열 구성: [번호, 구분, 이름] + days + [spacer] + [D,E,N,O]
    static_cols = 4
    spacer_cols = 1
    summary_cols = 4
    total_cols = static_cols + days_in_month + spacer_cols + summary_cols
    # 제목을 스케줄 전체 폭(1..total_cols)으로 병합
    ws.merge_cells(start_row=2, start_column=1, end_row=3, end_column=total_cols)
    ws.cell(row=2, column=1, value=title).font = title_font
    ws.cell(row=2, column=1).alignment = center

    # ───────── 4) 헤더 행 ─────────
    header_row = 7
    ws.cell(row=header_row, column=1, value="번호").font = header_font
    ws.cell(row=header_row, column=2, value="구분").font = header_font
    ws.cell(row=header_row, column=3, value="경력").font = header_font
    ws.cell(row=header_row, column=4, value="이름").font = header_font
    for c in range(1, static_cols + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.alignment = center; cell.border = border_all; cell.fill = gray_fill

    for d in range(1, days_in_month + 1):
        col = static_cols + d
        cell = ws.cell(row=header_row, column=col, value=d)
        cell.font = header_font; cell.alignment = center; cell.border = border_all; cell.fill = gray_fill

    tail_labels = ['D', 'E', 'N', 'O']
    tail_start_col = static_cols + days_in_month + 1 + spacer_cols
    for i, lab in enumerate(tail_labels):
        col = tail_start_col + i
        cell = ws.cell(row=header_row, column=col, value=lab)
        cell.font = header_font; cell.alignment = center; cell.border = border_all; cell.fill = gray_fill

    # 열 너비 조정
    ws.column_dimensions['A'].width = 5
    ws.column_dimensions['B'].width = 6
    ws.column_dimensions['C'].width = 6
    ws.column_dimensions['D'].width = 12
    for d in range(1, days_in_month + 1):
        ws.column_dimensions[get_column_letter(static_cols + d)].width = 4
    # spacer 열 너비
    ws.column_dimensions[get_column_letter(static_cols + days_in_month + 1)].width = 3
    for i in range(summary_cols):
        ws.column_dimensions[get_column_letter(tail_start_col + i)].width = 5

    # ───────── 5) 본문: 간호사별 스케줄 ─────────
    start_row = header_row + 1
    daily_counts = {d: {'D': 0, 'E': 0, 'N': 0, 'O': 0} for d in range(1, days_in_month + 1)}

    for idx, n in enumerate(nurses, start=1):
        r = start_row + idx - 1
        # 번호/구분(간단 분류: HN/RN/AN 추정 불가 → RN로 표기)/이름
        ws.cell(row=r, column=1, value=idx)
        ws.cell(row=r, column=2, value="RN")
        ws.cell(row=r, column=3, value=n.experience)
        ws.cell(row=r, column=4, value=n.name)
        for c in range(1, static_cols + 1):
            cell = ws.cell(row=r, column=c)
            cell.alignment = center; cell.border = border_all

        # 일자별
        row_counts = {'D': 0, 'E': 0, 'N': 0, 'O': 0}
        schedule_map = by_nurse.get(n.nurse_id, {})
        for d in range(1, days_in_month + 1):
            shift_code = schedule_map.get(d, '-')
            cell = ws.cell(row=r, column=static_cols + d, value=shift_code)
            cell.alignment = center; cell.border = border_all
            base = to_base(shift_code)
            if base in row_counts:
                row_counts[base] += 1
                daily_counts[d][base] += 1

        # 합계(D/E/N/O)
        for i, lab in enumerate(tail_labels):
            col = tail_start_col + i
            cell = ws.cell(row=r, column=col, value=row_counts.get(lab, 0))
            cell.alignment = center; cell.border = border_all

    last_row = start_row + len(nurses) - 1

    # ───────── 6) 일일 근무 현황(풋터) ─────────
    footer_start = last_row + 2
    ws.cell(row=footer_start, column=2, value="일일 근무 현황").font = header_font

    def write_footer_row(label: str, values: list[int], row_idx: int):
        ws.cell(row=row_idx, column=3, value=label).font = header_font
        for c in range(1, static_cols):
            cell = ws.cell(row=row_idx, column=c)
            cell.border = border_all
        # days
        for d in range(1, days_in_month + 1):
            val = values[d - 1]
            cell = ws.cell(row=row_idx, column=static_cols + d, value=val)
            cell.alignment = center; cell.border = border_all
        # spacer + tail 4칸 비움(border 유지)
        for i in range(spacer_cols + summary_cols):
            col = static_cols + days_in_month + 1 + i
            cell = ws.cell(row=row_idx, column=col)
            cell.border = border_all

    d_vals = [daily_counts[d]['D'] for d in range(1, days_in_month + 1)]
    e_vals = [daily_counts[d]['E'] for d in range(1, days_in_month + 1)]
    n_vals = [daily_counts[d]['N'] for d in range(1, days_in_month + 1)]
    o_vals = [daily_counts[d]['O'] for d in range(1, days_in_month + 1)]

    write_footer_row('D', d_vals, footer_start + 1)
    write_footer_row('E', e_vals, footer_start + 2)
    write_footer_row('N', n_vals, footer_start + 3)
    write_footer_row('O', o_vals, footer_start + 4)

    # 테두리/정렬 마감 및 첫 행 스타일 약간 보정
    for row in ws.iter_rows(min_row=header_row, max_row=footer_start + 4, min_col=1, max_col=total_cols):
        for cell in row:
            if cell.value is None:
                continue
            # 이미 지정된 것 외 공통 보더
            if cell.border is None or cell.border.left.style is None:
                cell.border = border_all

    # 바이트로 저장
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.read() 


def export_members_excel_bytes(office_id: str) -> bytes:
    """ADM용 멤버 목록 엑셀 생성.

    - 입력: office_id
    - 컬럼: 대분류, 중분류, 소분류, 부서명, 사번, 직원명, 계정 ID, 직무, 경력, 수간호사여부
    - 반환: 생성된 xlsx 바이트
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

    rows = msdb_manager.fetch_all(Member.member_export_by_office(), params=(str(office_id),)) or []

    headers = [
        ("big_kind_name", "대분류"),
        ("middle_kind_name", "중분류"),
        ("small_kind_name", "소분류"),
        ("mb_part_name", "부서명"),
        ("OfficeEmpNum", "사번"),
        ("MemberID", "계정 ID"),
        ("EmployeeName", "직원명"),
        ("duty", "직무"),
        ("career", "경력"),
        ("headnurse", "수간호사여부"),
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "구성원"

    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")
    header_font = Font(bold=True)
    gray = PatternFill("solid", fgColor="DEE2E6")
    thin = Side(style="thin", color="000000")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)

    # 안내 문구 (맨 윗줄, 노란색 배경)
    guide_text = '사번 컬럼부터 우측으로 필요한 정보를 복사해 템플릿에 그룹(병동)별로 추가하여 업로드 하시면 됩니다'
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    gcell = ws.cell(row=1, column=1, value=guide_text)
    gcell.alignment = left
    gcell.fill = PatternFill("solid", fgColor="FFF59D")  # 연노랑
    gcell.border = border_all

    # 헤더 (2행부터)
    for i, (_, label) in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=i, value=label)
        cell.font = header_font
        cell.alignment = center
        cell.fill = gray
        cell.border = border_all

    # 데이터 (3행부터)
    for r_idx, row in enumerate(rows, start=3):
        for c_idx, (key, _) in enumerate(headers, start=1):
            # pyodbc.Row 또는 dict 형태 지원
            try:
                val = row[key]
            except Exception:
                val = row.get(key) if hasattr(row, 'get') else None
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.alignment = left
            cell.border = border_all

    # 약간의 폭 조정
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + col)].width = 16

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.read()